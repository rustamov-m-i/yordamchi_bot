"""Claude API wrapper. Builds context, calls Anthropic, parses JSON envelope response."""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import AsyncIterator, Optional, Tuple

from anthropic import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)

import config
import database
import redaction

logger = logging.getLogger(__name__)

_client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=60.0, max_retries=2)


# ──────────────────────── CIRCUIT BREAKER ────────────────────────
# Tracks recent Anthropic failures. When >= _CIRCUIT_THRESHOLD consecutive
# failures occur within _CIRCUIT_WINDOW seconds, the circuit "opens" and we
# short-circuit for _CIRCUIT_COOLDOWN seconds (returning a degraded fallback
# instead of hammering a down API). After cooldown, the next call is allowed
# ("half-open"): success closes the circuit; failure re-opens it.
_CIRCUIT_THRESHOLD = 5            # consecutive failures to trip
_CIRCUIT_WINDOW = 60              # seconds within which threshold counts
_CIRCUIT_COOLDOWN = 120           # seconds the circuit stays open
import time as _time

_circuit_failures: list[float] = []  # timestamps of recent failures
_circuit_open_until: float = 0.0     # epoch seconds; 0 = closed


def _circuit_is_open() -> bool:
    return _time.time() < _circuit_open_until


def _circuit_record_failure() -> None:
    now = _time.time()
    _circuit_failures.append(now)
    # Keep only failures within the window
    cutoff = now - _CIRCUIT_WINDOW
    while _circuit_failures and _circuit_failures[0] < cutoff:
        _circuit_failures.pop(0)
    if len(_circuit_failures) >= _CIRCUIT_THRESHOLD:
        global _circuit_open_until
        _circuit_open_until = now + _CIRCUIT_COOLDOWN
        logger.error(
            "Anthropic circuit OPEN — %d failures in %ds, cooling down for %ds",
            len(_circuit_failures), _CIRCUIT_WINDOW, _CIRCUIT_COOLDOWN,
        )
        _circuit_failures.clear()


def _circuit_record_success() -> None:
    """Successful call closes the circuit and resets the failure counter."""
    global _circuit_open_until
    _circuit_open_until = 0.0
    _circuit_failures.clear()


# Internal-directive keywords that signal complex reasoning and benefit from Opus.
# Anything not on this list (and not explicitly forced via complexity="fast"|"complex")
# uses the default CLAUDE_MODEL (Sonnet).
#
# NOTE: check_followups was deliberately removed. It is a routine 4×/day state scan
# with tiny output (~115 tokens) but ran on Opus and drove ~70% of total LLM spend.
# Sonnet handles it at ~1/5 the cost with no quality loss, and two of its three
# checks are already covered by cheaper jobs (_post_meeting_followup_sweep and the
# nightly Haiku _proactive_dependency_check). Don't re-add without a cost review.
_COMPLEX_DIRECTIVE_KEYWORDS = ("executive_plan", "risk_analysis")


_MAX_HISTORY_TOKENS_APPROX = 2000  # ~4 chars/token heuristic

# Output token ceiling. A single message may ask to create many tasks at once
# (e.g. a pasted list of 14). At ~120 tokens per task object, the old 1500 cap
# truncated the JSON mid-array — the response failed to parse and the user saw
# "Texnik xato". 8000 fits ~40 task objects with headroom and is within both
# Sonnet 4.6 and Opus 4.8 default output limits. Cost is unaffected (billing is
# per token GENERATED, not per ceiling), so a short reply still bills tiny.
_MAX_OUTPUT_TOKENS = 8000


def _budget_history(history: list[dict], max_tokens: int = _MAX_HISTORY_TOKENS_APPROX) -> list[dict]:
    """Trim conversation history to fit within an approximate token budget.
    Walks newest-first so the most-recent context is preserved when older
    turns get dropped. Uses a coarse `len(content) // 4` estimate — not
    perfect but enough to prevent surprise context-window overruns when the
    user pastes long meeting notes."""
    if not history:
        return history
    out: list[dict] = []
    total = 0
    for msg in reversed(history):
        content = msg.get("content") or ""
        approx = max(1, len(content) // 4)
        if total + approx > max_tokens and out:
            break
        out.insert(0, msg)
        total += approx
    return out


def _pick_model(complexity: Optional[str], internal_directive: Optional[str]) -> str:
    """Route to the cheapest model that can do the job well.

    Explicit `complexity` (fast/default/complex) wins. Otherwise auto-detect
    from internal_directive keywords. Defaults to CLAUDE_MODEL (Sonnet) so
    behaviour is unchanged for the common case."""
    if complexity == "fast":
        return config.CLAUDE_MODEL_FAST
    if complexity == "complex":
        return config.CLAUDE_MODEL_COMPLEX
    if complexity == "default":
        return config.CLAUDE_MODEL
    if internal_directive and any(k in internal_directive for k in _COMPLEX_DIRECTIVE_KEYWORDS):
        return config.CLAUDE_MODEL_COMPLEX
    return config.CLAUDE_MODEL


async def _build_state_block() -> str:
    """Dynamic, DB-backed snapshot appended after the cached system prompt.

    Covers ALL four sections (tasks, meetings, reminders, notes) so Claude can
    answer questions about them from REAL data instead of guessing. It is a
    capped snapshot, not the full database — when the principal wants a complete
    list, Claude must emit a show_* action (see output contract) rather than
    enumerate from here. Claude must NEVER invent items beyond what's listed."""
    now = datetime.now(database.TZ)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    active_tasks = await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=200)
    overdue = await database.list_overdue_tasks()
    today_tasks = await database.list_today_tasks()
    done_today = await database.list_tasks_done_today()
    today_meetings = await database.list_today_meetings()
    week_meetings = [
        m for m in await database.list_meetings_in_window(
            today_start.isoformat(), (today_start + timedelta(days=7)).isoformat())
        if not m.get("completed_at")
    ]
    reminders = await database.list_reminders(status_in=["scheduled"], limit=20)
    notes_inbox = await database.count_notes_in_status("inbox")
    recent_notes = await database.list_notes(status="inbox", limit=8)
    contacts = await database.list_contacts()
    corrections = await database.list_recent_corrections(limit=5)

    def task_line(t: dict) -> str:
        deadline = t.get("deadline") or "no deadline"
        # assignee drives the /plan YUK BALANSI (load/bottleneck) view; "—" = the
        # principal's own task. Never omit it — the planner counts per-owner load.
        assignee = (t.get("assignee") or "").strip() or "—"
        # category shown so Claude REUSES existing category names (consistency)
        # instead of inventing near-duplicates on each new task.
        category = (t.get("category") or "").strip() or "—"
        return (f"  - [{t['priority']}] {t['title']} "
                f"(deadline: {deadline}, status: {t['status']}, assignee: {assignee}, "
                f"category: {category}, id: {t['id']})")

    def meeting_line(m: dict) -> str:
        return f"  - {m['datetime_start']} — {m['title']} (participants: {', '.join(m.get('participants', []))}, id: {m['id']})"

    def reminder_line(r: dict) -> str:
        return f"  - {r.get('remind_at')} — {r.get('title')} (id: {r.get('id')})"

    def note_line(n: dict) -> str:
        return f"  - {n.get('title') or (n.get('content') or '')[:50]} (id: {n.get('id')})"

    def contact_line(c: dict) -> str:
        return f"  - {c['name']} (role: {c.get('role') or 'unknown'}, formality: {c.get('formality_level', 3)})"

    def correction_line(c: dict) -> str:
        return f"  - {c.get('correction', '')[:120]} (reason: {c.get('reason', '')[:80]})"

    blocked = sum(1 for t in active_tasks if t.get("status") == "blocked")

    # Per-assignee load — PRECOMPUTED so /plan's YUK BALANSI uses exact counts
    # instead of the model re-counting task lines (which slips, e.g. 7 vs 8) and
    # undermines a feature whose whole point is "who is overloaded". "—" = the
    # principal's own / unassigned tasks. "soon" = P0 OR due by end of tomorrow.
    soon_cutoff = today_start + timedelta(days=2)
    load: dict[str, dict] = {}
    for t in active_tasks:
        who = (t.get("assignee") or "").strip() or "—"
        entry = load.setdefault(who, {"total": 0, "soon": 0})
        entry["total"] += 1
        is_soon = t.get("priority") == "P0"
        dl = t.get("deadline")
        if dl and not is_soon:
            try:
                is_soon = datetime.fromisoformat(dl) < soon_cutoff
            except (ValueError, TypeError):
                pass
        if is_soon:
            entry["soon"] += 1

    def load_line(item) -> str:
        who, e = item
        return f"  - {who}: {e['total']} active ({e['soon']} urgent/soon)"

    lines = [
        "# CURRENT PRINCIPAL STATE (real DB snapshot — do NOT invent anything beyond this)",
        "",
        f"current_datetime: {now.isoformat()}",
        f"current_weekday: {now.strftime('%A')}",
        "",
        "## COUNTS",
        f"tasks: active={len(active_tasks)}, overdue={len(overdue)}, due_today={len(today_tasks)}, "
        f"blocked={blocked}, done_today={len(done_today)}",
        f"meetings: today={len(today_meetings)}, this_week={len(week_meetings)}",
        f"reminders_scheduled: {len(reminders)}",
        f"notes_inbox: {notes_inbox}",
        "",
        f"## LOAD BY ASSIGNEE ({len(load)} owners — EXACT counts for /plan YUK BALANSI; '—' = principal's own)",
        *(load_line(it) for it in sorted(load.items(), key=lambda kv: -kv[1]["total"])),
        "  (none)" if not load else "",
        f"## ACTIVE TASKS ({len(active_tasks)} total, showing up to 25)",
        *(task_line(t) for t in active_tasks[:25]),
        "  (none)" if not active_tasks else "",
        f"## OVERDUE TASKS ({len(overdue)})",
        *(task_line(t) for t in overdue[:10]),
        "  (none)" if not overdue else "",
        f"## MEETINGS — today + this week ({len(week_meetings)})",
        *(meeting_line(m) for m in week_meetings[:12]),
        "  (none)" if not week_meetings else "",
        f"## SCHEDULED REMINDERS ({len(reminders)})",
        *(reminder_line(r) for r in reminders[:12]),
        "  (none)" if not reminders else "",
        f"## NOTES INBOX ({notes_inbox} unprocessed, showing up to 8)",
        *(note_line(n) for n in recent_notes[:8]),
        "  (none)" if not recent_notes else "",
        f"## CONTACTS ({len(contacts)})",
        *(contact_line(c) for c in contacts[:15]),
        "  (none)" if not contacts else "",
        f"## STYLE CORRECTIONS ({len(corrections)})",
        *(correction_line(c) for c in corrections),
        "  (none)" if not corrections else "",
    ]
    return "\n".join(lines)


_USER_MESSAGE_KEY_RE = re.compile(r'"user_message"\s*:\s*"')


def _extract_partial_user_message(buf: str) -> Optional[str]:
    """Best-effort extraction of the `user_message` value from a partial JSON
    buffer produced by Anthropic's streaming response. Returns None if the
    key hasn't appeared yet; otherwise returns whatever text has accumulated
    inside the string so far (possibly empty, possibly mid-word).

    Handles the common JSON escape sequences. If the buffer ends mid-escape
    (last char is '\\\\'), the escape is held back for the next call."""
    m = _USER_MESSAGE_KEY_RE.search(buf)
    if not m:
        return None
    out: list[str] = []
    i = m.end()
    n = len(buf)
    while i < n:
        c = buf[i]
        if c == "\\":
            if i + 1 >= n:
                break  # incomplete escape — wait for more data
            nxt = buf[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
            out.append(mapping.get(nxt, nxt))
            i += 2
            continue
        if c == '"':
            break  # end of user_message string
        out.append(c)
        i += 1
    return "".join(out)


def _extract_json(text: str) -> Optional[dict]:
    """Robust JSON extraction. Strips code fences and finds the first valid JSON object."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        logger.exception("Failed to parse JSON from Claude response: %s", text[:500])
        return None


def _fallback(user_message: str) -> dict:
    return {
        "intent": "none",
        "actions": [],
        "user_message": user_message,
        "buttons": [],
        "needs_clarification": False,
        "clarification_question": None,
    }


_FALLBACK_RESPONSE = _fallback("Texnik xato yuz berdi. Iltimos, qaytadan urinib ko'ring.")


def _classify_anthropic_error(e: Exception) -> Tuple[str, str]:
    """Map an Anthropic SDK exception to (audit_label, O'zbek user message).
    Mirrors the inline handling in process_message; used by process_document so
    the document path reports the same root causes (rate limit, credit, etc.)."""
    if isinstance(e, RateLimitError):
        return "rate_limit", "Juda ko'p so'rov. Bir-ikki daqiqadan keyin qayta urinib ko'ring."
    if isinstance(e, AuthenticationError):
        return "auth", "Claude kalitida muammo. Administrator bilan bog'laning."
    if isinstance(e, BadRequestError):
        msg = str(e).lower()
        if "credit" in msg or "balance" in msg or "billing" in msg:
            return ("credit_low",
                    "Claude balansi tugadi. Hisobni to'ldiring: "
                    "https://console.anthropic.com/settings/billing")
        return "bad_request", "Texnik xato (bad request). Iltimos, qaytadan urinib ko'ring."
    if isinstance(e, APITimeoutError):
        return "timeout", "Javob kech keldi. Qaytadan urinib ko'ring."
    if isinstance(e, APIConnectionError):
        return "connection", "Tarmoqqa ulanib bo'lmadi. Bir ozdan keyin qaytadan urinib ko'ring."
    if isinstance(e, APIStatusError):
        code = getattr(e, "status_code", None)
        if code == 529:
            return f"status_{code}", "Claude vaqtincha band. Bir ozdan keyin qaytadan urinib ko'ring."
        return f"status_{code}", "Texnik xato yuz berdi. Iltimos, qaytadan urinib ko'ring."
    return "api_error", "Texnik xato yuz berdi. Iltimos, qaytadan urinib ko'ring."


def _envelope_from_raw(raw: str) -> Optional[dict]:
    """Turn Claude's raw output into a response envelope.

    Normally Claude returns the JSON envelope. But for trivial conversational
    inputs ("Salom", "ha", "rahmat") it sometimes replies in PLAIN TEXT. Rather
    than surfacing a "Texnik xato", treat such a reply as a normal user_message
    so the user just sees Claude's answer. Returns None only when the output is
    genuinely unusable (empty, or a broken JSON attempt) — the caller then uses
    _FALLBACK_RESPONSE.
    """
    parsed = _extract_json(raw)
    if parsed:
        return parsed
    text = (raw or "").strip()
    if text and "{" not in text and "}" not in text:
        # Pure prose — Claude chatted instead of emitting JSON. Show it verbatim.
        return _fallback(text)
    return None


async def process_message(
    user_text: str,
    internal_directive: Optional[str] = None,
    complexity: Optional[str] = None,
) -> dict:
    """Send user input to Claude, return parsed JSON envelope.

    internal_directive: when invoked by scheduler (briefings, follow-ups) instead of by the user,
                        pass a system-style directive like "[INTERNAL] generate_morning_briefing".
    complexity: optional override for the model router — "fast" (Haiku),
                "default" (Sonnet), or "complex" (Opus). When omitted, the
                router auto-picks based on internal_directive keywords.
    """

    if _circuit_is_open():
        logger.warning("Anthropic circuit open — short-circuiting (cooldown remaining: %.0fs)",
                        _circuit_open_until - _time.time())
        return _fallback(
            "Claude vaqtinchalik mavjud emas. Bir necha daqiqadan keyin qayta urinib ko'ring. "
            "Bot boshqa funksiyalari (ro'yxatlar, qidiruv) ishlayapti."
        )

    model = _pick_model(complexity, internal_directive)
    state_block = await _build_state_block()

    # Conversation history is included ONLY for the interactive user path.
    # Internal directives (briefings, summaries, proactive checks) must rely
    # SOLELY on the structured state block — otherwise Claude treats things the
    # user merely MENTIONED in chat (but never saved) as real tasks/meetings and
    # fabricates them into briefings (reported bug: phantom "overdue tasks").
    if internal_directive:
        messages = []
    else:
        history = await database.recent_messages(limit=10)
        history = _budget_history(history)
        # Drop any empty-content rows — the Anthropic API rejects empty messages
        # ("messages.N: ... must have non-empty content"), which would 400 the
        # whole call. Defense-in-depth alongside not persisting internal directives.
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if (m.get("content") or "").strip()]

    # Redact PII for BOTH user messages and internal directives. Internal directives
    # are generated server-side but may interpolate state (task titles, meeting
    # agendas) that could contain redactable values (card numbers, IBANs) the
    # principal entered earlier.
    if internal_directive:
        outgoing_content, redacted_count = redaction.redact(internal_directive)
        purpose = "internal:" + internal_directive[:40]
    else:
        outgoing_content, redacted_count = redaction.redact(user_text)
        purpose = "user_message"
    messages.append({"role": "user", "content": outgoing_content})

    input_hash = redaction.hash_input(outgoing_content)
    input_chars = len(outgoing_content)

    try:
        max_tokens = _MAX_OUTPUT_TOKENS
        response = await _client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": config.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": state_block,
                },
            ],
            messages=messages,
        )
    except RateLimitError:
        logger.warning("Anthropic rate limited (model=%s)", model)
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error="rate_limit")
        _circuit_record_failure()
        return _fallback("Juda ko'p so'rov. Bir-ikki daqiqadan keyin qayta urinib ko'ring.")
    except AuthenticationError:
        logger.exception("Anthropic auth failed — check ANTHROPIC_API_KEY")
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error="auth")
        _circuit_record_failure()
        return _fallback("Claude kalit kalitida muammo. Administrator bilan bog'laning.")
    except BadRequestError as e:
        msg = str(e).lower()
        err_label = "credit_low" if ("credit" in msg or "balance" in msg or "billing" in msg) else "bad_request"
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error=err_label)
        # credit_low is "permanent" until billing is fixed — keep circuit closed
        # so we don't waste calls on a known-broken state during the cooldown.
        _circuit_record_failure()
        if err_label == "credit_low":
            return _fallback("Claude balansi tugadi. Hisobni to'ldiring: https://console.anthropic.com/settings/billing")
        logger.exception("Anthropic bad request")
        return _fallback("Texnik xato (bad request). Iltimos, qaytadan urinib ko'ring.")
    except APITimeoutError:
        logger.warning("Anthropic timeout (model=%s)", model)
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error="timeout")
        _circuit_record_failure()
        return _fallback("Javob kech keldi. Qaytadan urinib ko'ring.")
    except APIConnectionError:
        logger.warning("Anthropic connection error")
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error="connection")
        _circuit_record_failure()
        return _fallback("Tarmoqqa ulanib bo'lmadi. Bir ozdan keyin qaytadan urinib ko'ring.")
    except APIStatusError as e:
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error=f"status_{e.status_code}")
        _circuit_record_failure()
        if getattr(e, "status_code", None) == 529:
            return _fallback("Claude vaqtincha band. Bir ozdan keyin qaytadan urinib ko'ring.")
        logger.exception("Anthropic API status error: %s", e.status_code)
        return _FALLBACK_RESPONSE
    except APIError:
        logger.exception("Anthropic API error")
        await database.log_llm_call("anthropic", model, purpose, input_hash, input_chars, None, None, redacted_terms_count=redacted_count, error="api_error")
        _circuit_record_failure()
        return _FALLBACK_RESPONSE

    # Successful call — reset the circuit so any cooldown clears immediately.
    _circuit_record_success()
    raw = response.content[0].text if response.content else ""

    # Audit log — success path. Log the model that was actually used (after
    # router decision), not config.CLAUDE_MODEL, so cost analytics stay accurate.
    usage = getattr(response, "usage", None)
    in_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
    cost = redaction.estimate_cost(model, in_tokens, out_tokens, cache_read, cache_creation)
    await database.log_llm_call(
        "anthropic", model, purpose, input_hash, input_chars,
        in_tokens, out_tokens, cache_read, cache_creation,
        redacted_terms_count=redacted_count, estimated_cost_usd=cost,
    )

    parsed = _envelope_from_raw(raw)
    if not parsed:
        logger.error("Claude returned unusable output: %s", raw[:500])
        return _FALLBACK_RESPONSE

    parsed.setdefault("intent", "none")
    parsed.setdefault("actions", [])
    parsed.setdefault("user_message", "")
    parsed.setdefault("buttons", [])
    parsed.setdefault("needs_clarification", False)
    parsed.setdefault("clarification_question", None)

    if not internal_directive:
        await database.append_message("user", user_text)
        await database.append_message("assistant", parsed.get("user_message") or "✅")
        await database.trim_history(keep=30)

    return parsed


async def process_document(
    instruction: str,
    content_blocks: list,
    complexity: Optional[str] = None,
    file_label: Optional[str] = None,
) -> dict:
    """Analyse an uploaded document/image and return the standard JSON envelope.

    `content_blocks` are pre-built Anthropic content blocks (text/image/document)
    from document_service — prepended to the user turn, followed by `instruction`.
    Because the return shape matches process_message, handlers can route any
    create_task / create_reminder actions through the normal confirm pipeline.

    Not streamed: analysis isn't latency-critical and handlers shows a working
    indicator. A short marker plus the summary are written to conversation history
    so the principal can ask follow-up questions about the document.
    """
    if _circuit_is_open():
        logger.warning("Anthropic circuit open — short-circuiting document analysis "
                        "(cooldown remaining: %.0fs)", _circuit_open_until - _time.time())
        return _fallback(
            "Claude vaqtinchalik mavjud emas. Bir necha daqiqadan keyin qayta urinib ko'ring. "
            "Bot boshqa funksiyalari (ro'yxatlar, qidiruv) ishlayapti."
        )

    model = _pick_model(complexity, None)
    state_block = await _build_state_block()

    # The instruction is small and server-shaped, but redact it anyway — the
    # principal's caption may quote a card number / IBAN. (Text extracted from
    # the file is already redacted upstream in document_service.)
    instruction_red, redacted_count = redaction.redact(instruction or "")
    content = list(content_blocks) + [{"type": "text", "text": instruction_red}]

    input_hash = redaction.hash_input(instruction_red)
    # Only the textual portion is "chars"; binary image/PDF blocks aren't counted.
    input_chars = sum(len(b.get("text", "")) for b in content if b.get("type") == "text")
    purpose = "document"

    try:
        response = await _client.messages.create(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": config.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": state_block},
            ],
            messages=[{"role": "user", "content": content}],
        )
    except (RateLimitError, AuthenticationError, BadRequestError, APITimeoutError,
            APIConnectionError, APIStatusError, APIError) as e:
        label, msg = _classify_anthropic_error(e)
        if label not in ("rate_limit", "credit_low"):
            logger.exception("Anthropic document call failed (%s, model=%s)", label, model)
        await database.log_llm_call(
            "anthropic", model, purpose, input_hash, input_chars, None, None,
            redacted_terms_count=redacted_count, error=label,
        )
        _circuit_record_failure()
        return _fallback(msg)

    _circuit_record_success()
    raw = response.content[0].text if response.content else ""

    usage = getattr(response, "usage", None)
    in_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    out_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
    cache_creation = getattr(usage, "cache_creation_input_tokens", 0) if usage else 0
    cost = redaction.estimate_cost(model, in_tokens, out_tokens, cache_read, cache_creation)
    await database.log_llm_call(
        "anthropic", model, purpose, input_hash, input_chars,
        in_tokens, out_tokens, cache_read, cache_creation,
        redacted_terms_count=redacted_count, estimated_cost_usd=cost,
    )

    parsed = _envelope_from_raw(raw)
    if not parsed:
        logger.error("Claude (document) returned unusable output: %s", raw[:500])
        return _FALLBACK_RESPONSE

    parsed.setdefault("intent", "none")
    parsed.setdefault("actions", [])
    parsed.setdefault("user_message", "")
    parsed.setdefault("buttons", [])
    parsed.setdefault("needs_clarification", False)
    parsed.setdefault("clarification_question", None)

    # Persist a SHORT marker (not the file contents) + the summary so the
    # principal can follow up ("3-banddagi muddat qachon?") with context.
    try:
        marker = f"[Hujjat yuborildi: {file_label or 'fayl'}] {(instruction or '').strip()[:200]}"
        await database.append_message("user", marker.strip())
        await database.append_message("assistant", parsed.get("user_message") or "✅")
        await database.trim_history(keep=30)
    except Exception:
        logger.debug("Could not persist document turn to history")

    return parsed


# ──────────────────────── STREAMING (user-facing path only) ────────────────────────


async def process_message_stream(
    user_text: str,
    complexity: Optional[str] = None,
    internal_directive: Optional[str] = None,
) -> AsyncIterator[Tuple[str, object]]:
    """Streaming variant of process_message for the interactive user path.

    Yields tuples (kind, payload):
        ("partial", str)   — current best-effort user_message text. Callers
                              should treat each yield as the FULL accumulated
                              text so far (not a delta) and replace/edit any
                              progress message with it.
        ("complete", dict) — the final, fully-parsed JSON envelope. Same shape
                              as process_message() returns.

    `internal_directive` streams a server-generated directive (e.g. /plan's
    executive_plan) the SAME way — there IS a user waiting, and a long Opus
    plan that appears 33s later with zero feedback reads as "broken". Streaming
    shows the plan building live. As in process_message, an internal directive
    skips conversation history (the directive carries its own context via the
    state block) to avoid hallucinating chat-only items.

    Falls back to the non-streaming path on any unrecoverable error (yields
    a single ("complete", fallback_dict))."""

    model = _pick_model(complexity, internal_directive)
    state_block = await _build_state_block()
    if internal_directive:
        messages = []
        outgoing_content, redacted_count = redaction.redact(internal_directive)
        purpose = "internal_stream:" + internal_directive[:40]
    else:
        history = await database.recent_messages(limit=10)
        history = _budget_history(history)
        # Drop any empty-content rows — the Anthropic API rejects empty messages
        # ("messages.N: ... must have non-empty content"), which would 400 the
        # whole call. Defense-in-depth alongside not persisting internal directives.
        messages = [{"role": m["role"], "content": m["content"]}
                    for m in history if (m.get("content") or "").strip()]
        outgoing_content, redacted_count = redaction.redact(user_text)
        purpose = "user_message_stream"
    messages.append({"role": "user", "content": outgoing_content})

    input_hash = redaction.hash_input(outgoing_content)
    input_chars = len(outgoing_content)

    buf = ""
    last_emitted = ""
    final_usage = None
    try:
        async with _client.messages.stream(
            model=model,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": config.SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": state_block},
            ],
            messages=messages,
        ) as stream:
            async for chunk in stream.text_stream:
                if not chunk:
                    continue
                buf += chunk
                partial = _extract_partial_user_message(buf)
                if partial is None or partial == last_emitted:
                    continue
                last_emitted = partial
                yield ("partial", partial)
            final_message = await stream.get_final_message()
            final_usage = getattr(final_message, "usage", None)
    except (RateLimitError, AuthenticationError, BadRequestError,
             APITimeoutError, APIConnectionError, APIStatusError, APIError) as e:
        logger.warning("Streaming Claude failed (%s) — falling back to non-streaming", type(e).__name__)
        fallback = await process_message(user_text, complexity=complexity, internal_directive=internal_directive)
        yield ("complete", fallback)
        return

    raw = buf
    # Audit log mirrors process_message (success path) so cost analytics still work.
    in_tokens = getattr(final_usage, "input_tokens", 0) if final_usage else 0
    out_tokens = getattr(final_usage, "output_tokens", 0) if final_usage else 0
    cache_read = getattr(final_usage, "cache_read_input_tokens", 0) if final_usage else 0
    cache_creation = getattr(final_usage, "cache_creation_input_tokens", 0) if final_usage else 0
    cost = redaction.estimate_cost(model, in_tokens, out_tokens, cache_read, cache_creation)
    await database.log_llm_call(
        "anthropic", model, purpose, input_hash, input_chars,
        in_tokens, out_tokens, cache_read, cache_creation,
        redacted_terms_count=redacted_count, estimated_cost_usd=cost,
    )

    parsed = _envelope_from_raw(raw)
    if not parsed:
        logger.error("Claude (stream) returned unusable output: %s", raw[:500])
        yield ("complete", _FALLBACK_RESPONSE)
        return

    parsed.setdefault("intent", "none")
    parsed.setdefault("actions", [])
    parsed.setdefault("user_message", "")
    parsed.setdefault("buttons", [])
    parsed.setdefault("needs_clarification", False)
    parsed.setdefault("clarification_question", None)

    # Never persist internal directives (plans/briefings) — their user_text is ""
    # which would poison history with an empty message and 400 the next API call.
    if not internal_directive:
        await database.append_message("user", user_text)
        await database.append_message("assistant", parsed.get("user_message") or "✅")
        await database.trim_history(keep=30)

    yield ("complete", parsed)
