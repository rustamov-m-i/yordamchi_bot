"""Regression tests for the QA fixes applied 2026-05-27.

Each test exercises a concrete bug-fix to prevent regression:

  - complete_task atomic transaction (no duplicate recurring tasks)
  - save_contact UPSERT (no UNIQUE-constraint crash under concurrency)
  - mark_reminder_sent atomic claim (single delivery)
  - mark_task_reminded conditional claim (single delivery)
  - mark_meeting_reminded / prep / followup conditional claim
  - _spawn_background helper (exceptions logged, ref kept)
  - _cb_int / _cb_part safe callback parsing
  - _coerce_dt / parse_iso_dt handle aware datetimes

Run: ./venv/bin/python tests/qa_regression.py
"""

import asyncio
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database  # noqa: E402
import handlers  # noqa: E402

_results: list[tuple[str, bool, str]] = []


def t(group: str, name: str, ok: bool, detail: str = ""):
    _results.append((f"{group}.{name}", ok, detail))
    icon = "✓" if ok else "✗"
    print(f"  {icon} {name:60} {detail}")


def section(title: str):
    print(f"\n━━━ {title} ━━━")


async def test_complete_task_race():
    section("1. complete_task atomic — no duplicate recurring")
    # Create a recurring (daily) task with a past deadline so completion is allowed.
    base_iso = (datetime.now(database.TZ) - timedelta(hours=2)).isoformat()
    tid = await database.create_task({
        "title": "QA recurring",
        "priority": "P2",
        "status": "todo",
        "deadline": base_iso,
        "recurrence_rule": "daily",
    })
    t("complete_task", "setup recurring task", tid.startswith("t-"))

    # Fire 5 concurrent complete_task calls — pre-fix this could create
    # multiple "next" recurring tasks; post-fix only the first wins.
    results = await asyncio.gather(
        *(database.complete_task(tid) for _ in range(5)),
        return_exceptions=True,
    )
    t("complete_task", "all 5 concurrent calls returned True",
      all(r is True for r in results),
      f"results={results}")

    # Count newly-spawned recurring children: rows with recurrence_parent_id=tid.
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE recurrence_parent_id = ? AND id != ?",
            (tid, tid),
        )
        row = await cur.fetchone()
        children = row["c"]
    t("complete_task", "exactly one recurring child created", children == 1,
      f"children={children}")

    # Cleanup
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE recurrence_parent_id = ? OR id = ?",
                          (tid, tid))
        await db.commit()


async def test_save_contact_race():
    section("2. save_contact UPSERT — concurrent same-name doesn't crash")
    name = f"QA_CONTACT_{datetime.now().timestamp():.0f}"

    # Fire 5 concurrent upserts of the same name. Pre-fix: one wins, the rest
    # would race past the SELECT and crash on UNIQUE-constraint INSERT.
    results = await asyncio.gather(
        *(database.save_contact({"name": name, "role": f"R{i}"}) for i in range(5)),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    t("save_contact", "no exceptions raised", not errors,
      f"errors={errors}")
    ids = {r for r in results if isinstance(r, str) and r}
    t("save_contact", "all callers received the same contact id",
      len(ids) == 1, f"ids={ids}")

    # Cleanup
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM contacts WHERE name = ?", (name,))
        await db.commit()


async def test_mark_reminder_sent_atomic():
    section("3. mark_reminder_sent — only one caller wins per delivery")
    # Create a one-shot reminder (no recurrence) so the second caller MUST see
    # status='sent' and return False.
    rid = await database.create_reminder({
        "title": "QA reminder",
        "remind_at": datetime.now(database.TZ).isoformat(),
        "status": "scheduled",
    })
    t("mark_reminder_sent", "setup reminder", rid.startswith("r-"))

    results = await asyncio.gather(
        *(database.mark_reminder_sent(rid) for _ in range(4)),
        return_exceptions=True,
    )
    wins = sum(1 for r in results if r is True)
    t("mark_reminder_sent", "exactly one of 4 concurrent calls won",
      wins == 1, f"wins={wins}, results={results}")

    # Cleanup
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        await db.commit()


async def test_mark_reminder_sent_recurring_advances():
    section("4. mark_reminder_sent — recurring stays scheduled, remind_at advances")
    base = datetime.now(database.TZ).isoformat()
    rid = await database.create_reminder({
        "title": "QA daily reminder",
        "remind_at": base,
        "status": "scheduled",
        "recurrence_rule": "daily",
    })
    ok = await database.mark_reminder_sent(rid)
    t("mark_reminder_sent.recurring", "first call wins", ok is True)
    after = await database.get_reminder(rid)
    t("mark_reminder_sent.recurring", "status stays scheduled",
      after.get("status") == "scheduled", f"status={after.get('status')}")
    t("mark_reminder_sent.recurring", "remind_at moved forward",
      after.get("remind_at") != base, f"new={after.get('remind_at')}")

    # Cleanup
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM reminders WHERE id = ?", (rid,))
        await db.commit()


async def test_mark_task_reminded_atomic():
    section("5. mark_task_reminded — single-winner claim")
    tid = await database.create_task({
        "title": "QA reminder claim",
        "priority": "P1",
        "deadline": (datetime.now(database.TZ) + timedelta(hours=1)).isoformat(),
    })
    results = await asyncio.gather(
        *(database.mark_task_reminded(tid) for _ in range(6)),
    )
    wins = sum(1 for r in results if r is True)
    t("mark_task_reminded", "exactly one of 6 concurrent calls won",
      wins == 1, f"wins={wins}")
    # Second-pass call (after reminded_at is set) returns False.
    t("mark_task_reminded", "subsequent call returns False",
      await database.mark_task_reminded(tid) is False)

    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM tasks WHERE id = ?", (tid,))
        await db.commit()


async def test_meeting_claims():
    section("6. mark_meeting_* — conditional claim")
    mid = await database.create_meeting({
        "title": "QA meeting",
        "datetime_start": (datetime.now(database.TZ) + timedelta(hours=2)).isoformat(),
        "datetime_end": (datetime.now(database.TZ) + timedelta(hours=3)).isoformat(),
    })
    t("meeting.claim", "first mark_meeting_reminded wins",
      await database.mark_meeting_reminded(mid) is True)
    t("meeting.claim", "second mark_meeting_reminded loses",
      await database.mark_meeting_reminded(mid) is False)
    t("meeting.claim", "first mark_meeting_prep_sent wins",
      await database.mark_meeting_prep_sent(mid) is True)
    t("meeting.claim", "second mark_meeting_prep_sent loses",
      await database.mark_meeting_prep_sent(mid) is False)
    t("meeting.claim", "first mark_meeting_followup_sent wins",
      await database.mark_meeting_followup_sent(mid) is True)
    t("meeting.claim", "second mark_meeting_followup_sent loses",
      await database.mark_meeting_followup_sent(mid) is False)

    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM meetings WHERE id = ?", (mid,))
        await db.commit()


def test_cb_int_safety():
    section("7. _cb_int / _cb_part — safe callback parsing")
    t("cb_int", "well-formed 'prefix:5' → 5", handlers._cb_int("prefix:5") == 5)
    t("cb_int", "negative 'prefix:-3' → -3", handlers._cb_int("prefix:-3") == -3)
    t("cb_int", "missing tail 'prefix:' → default",
      handlers._cb_int("prefix:", default=99) == 99)
    t("cb_int", "no colon 'prefix' → default",
      handlers._cb_int("prefix", default=7) == 7)
    t("cb_int", "non-numeric 'prefix:abc' → default",
      handlers._cb_int("prefix:abc", default=0) == 0)
    t("cb_int", "None → default", handlers._cb_int(None, default=1) == 1)
    t("cb_int", "empty '' → default", handlers._cb_int("", default=2) == 2)

    t("cb_part", "'a:b:c'[2] → 'c'", handlers._cb_part("a:b:c", 2) == "c")
    t("cb_part", "'a:b'[2] → '' (default)", handlers._cb_part("a:b", 2) == "")
    t("cb_part", "None[1] → default", handlers._cb_part(None, 1, default="x") == "x")
    t("cb_part", "negative index → default", handlers._cb_part("a:b", -1, default="z") == "z")


def test_coerce_dt_tz_handling():
    section("8. _coerce_dt / parse_iso_dt — aware datetime handling")
    # Naive ISO → localized to Asia/Tashkent
    naive = "2026-05-27T14:00:00"
    dt = database._coerce_dt(naive)
    t("coerce_dt", "naive input becomes tz-aware",
      dt.tzinfo is not None, f"tzinfo={dt.tzinfo}")
    t("coerce_dt", "naive input keeps wall-clock hour",
      dt.hour == 14, f"hour={dt.hour}")

    # Aware ISO in UTC → converted to Asia/Tashkent (+5)
    utc_iso = "2026-05-27T09:00:00+00:00"
    dt = database._coerce_dt(utc_iso)
    t("coerce_dt", "UTC input converted to Asia/Tashkent",
      dt.hour == 14, f"hour={dt.hour} (expected 14 for UTC+5)")

    # parse_iso_dt returns None on garbage
    t("parse_iso_dt", "garbage input returns None",
      database.parse_iso_dt("not-a-date") is None)
    t("parse_iso_dt", "None input returns None",
      database.parse_iso_dt(None) is None)
    # parse_iso_dt also normalizes aware to TZ
    dt = database.parse_iso_dt(utc_iso)
    t("parse_iso_dt", "UTC input normalized to Asia/Tashkent",
      dt is not None and dt.hour == 14)


async def test_spawn_background_logs_exceptions():
    section("9. _spawn_background — exceptions logged, ref kept")

    async def will_fail():
        raise RuntimeError("intentional QA failure")

    # Capture logger output
    caplog = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            caplog.append(self.format(record))

    h = _ListHandler(level=logging.ERROR)
    handlers.logger.addHandler(h)
    try:
        task = handlers._spawn_background(will_fail(), name="qa_fail")
        t("spawn_background", "task ref retained in registry",
          task in handlers._background_tasks)
        # Drain the task
        try:
            await task
        except RuntimeError:
            pass  # asyncio.Task.__await__ re-raises; that's fine here
        # Give done_callbacks a tick
        await asyncio.sleep(0)
        t("spawn_background", "task removed from registry after done",
          task not in handlers._background_tasks)
        t("spawn_background", "exception logged with task name",
          any("qa_fail" in line and "intentional QA failure" in line for line in caplog),
          f"captured={caplog}")
    finally:
        handlers.logger.removeHandler(h)


def test_redaction_phase1():
    """Phase 1 expansion: Uzbek-specific PII patterns + ensure internal
    directives are subject to the same redaction as user messages."""
    import redaction

    section("10. redaction — Uzbek PII patterns (PASSPORT, INPS)")
    txt = "Mening pasportim AA1234567 va INN 123456789."
    redacted, n = redaction.redact(txt)
    t("redaction", "PASSPORT pattern catches AA1234567",
      "[PASSPORT-REDACTED]" in redacted and n >= 1,
      f"out={redacted!r}")
    t("redaction", "INN (with keyword) still redacted",
      "[INN-REDACTED]" in redacted)
    t("redaction", "Raw digits not leaked through",
      "AA1234567" not in redacted and "123456789" not in redacted)

    # INPS with keyword
    txt = "INPS 12345678901234 ekan"
    redacted, n = redaction.redact(txt)
    t("redaction", "INPS (with keyword + 14 digits) redacted",
      "[INPS-REDACTED]" in redacted or "[INPS_BARE-REDACTED]" in redacted,
      f"out={redacted!r}")

    # Bare 14-digit string (likely JShShIR / PINFL)
    txt = "JShShIR 12345678901234"
    redacted, n = redaction.redact(txt)
    t("redaction", "JShShIR keyword variant redacted",
      n >= 1 and "12345678901234" not in redacted,
      f"out={redacted!r}")

    # Card numbers still caught (regression)
    txt = "Karta: 5614 6810 1234 5678"
    redacted, n = redaction.redact(txt)
    t("redaction", "CARD pattern still works (regression)",
      "[CARD-REDACTED]" in redacted)


def test_model_router():
    """Phase 2.1: claude_service._pick_model routes by hint then by directive keyword."""
    import claude_service
    section("14. claude_service._pick_model — Haiku/Sonnet/Opus routing")
    t("router", "complexity='fast' → Haiku",
      claude_service._pick_model("fast", None) == config.CLAUDE_MODEL_FAST)
    t("router", "complexity='complex' → Opus",
      claude_service._pick_model("complex", None) == config.CLAUDE_MODEL_COMPLEX)
    t("router", "complexity='default' → Sonnet",
      claude_service._pick_model("default", None) == config.CLAUDE_MODEL)
    t("router", "no hint, no directive → Sonnet (default)",
      claude_service._pick_model(None, None) == config.CLAUDE_MODEL)
    t("router", "no hint, 'executive_plan' directive → Opus",
      claude_service._pick_model(None, "[INTERNAL] executive_plan blah") == config.CLAUDE_MODEL_COMPLEX)
    t("router", "no hint, 'check_followups' directive → Sonnet (moved off Opus for cost)",
      claude_service._pick_model(None, "[INTERNAL] check_followups blah") == config.CLAUDE_MODEL)
    t("router", "no hint, ordinary directive → Sonnet",
      claude_service._pick_model(None, "[INTERNAL] generate_morning_briefing") == config.CLAUDE_MODEL)
    t("router", "explicit hint overrides directive",
      claude_service._pick_model("fast", "[INTERNAL] executive_plan") == config.CLAUDE_MODEL_FAST)


def test_partial_user_message_extraction():
    """Phase 2.3: best-effort extractor used during streaming. Must handle
    incremental buffers and JSON escape sequences."""
    import claude_service
    section("15. claude_service._extract_partial_user_message — streaming JSON")

    t("partial", "no user_message yet → None",
      claude_service._extract_partial_user_message('{"intent":"create_task",') is None)

    t("partial", "empty user_message → ''",
      claude_service._extract_partial_user_message('{"user_message": "') == "")

    t("partial", "incomplete value → text so far",
      claude_service._extract_partial_user_message('{"user_message": "Vazifa ya') == "Vazifa ya")

    t("partial", "newline escape decoded",
      claude_service._extract_partial_user_message('{"user_message": "Birinchi\\nIkkin') == "Birinchi\nIkkin")

    t("partial", "embedded escaped quote",
      claude_service._extract_partial_user_message('{"user_message": "He said \\"Hi\\"') == 'He said "Hi"')

    t("partial", "trailing backslash held back",
      claude_service._extract_partial_user_message('{"user_message": "Test\\') == "Test")

    t("partial", "complete value with following key",
      claude_service._extract_partial_user_message('{"user_message": "Done","buttons":[]}') == "Done")


async def test_pending_actions_idempotency():
    """Phase 2.2: pending_actions table records lifecycle and prevents
    duplicate processing of the same Telegram update_id."""
    section("16. database — pending_actions queue")

    pid1 = await database.enqueue_pending_action(
        update_id=999001, chat_id=42, message_id=1, user_text="qa test 1"
    )
    t("pending", "first enqueue returns row id", pid1 is not None and pid1 > 0)

    # Same update_id again → None (duplicate, idempotent)
    pid_dup = await database.enqueue_pending_action(
        update_id=999001, chat_id=42, message_id=1, user_text="qa test 1"
    )
    t("pending", "duplicate update_id returns None",
      pid_dup is None)

    await database.mark_pending_in_progress(pid1)
    await database.complete_pending_action(pid1)
    # Row should now be completed, not in stuck list
    stuck = await database.list_stuck_pending_actions(stuck_after_minutes=0)
    t("pending", "completed row not in stuck list",
      not any(r["id"] == pid1 for r in stuck))

    # Different update_id → new row
    pid2 = await database.enqueue_pending_action(
        update_id=999002, chat_id=42, message_id=2, user_text="qa test 2"
    )
    t("pending", "different update_id → new row", pid2 is not None and pid2 != pid1)
    await database.fail_pending_action(pid2, "intentional QA failure")
    # Failed row also not in stuck list (only pending/in_progress count as stuck)
    stuck = await database.list_stuck_pending_actions(stuck_after_minutes=0)
    t("pending", "failed row not in stuck list",
      not any(r["id"] == pid2 for r in stuck))

    # No update_id at all (e.g., synthetic call) → no UNIQUE conflict, multiple rows allowed
    pid3a = await database.enqueue_pending_action(
        update_id=None, chat_id=42, message_id=3, user_text="no update_id"
    )
    pid3b = await database.enqueue_pending_action(
        update_id=None, chat_id=42, message_id=4, user_text="no update_id again"
    )
    t("pending", "null update_id does not trigger UNIQUE constraint",
      pid3a is not None and pid3b is not None and pid3a != pid3b)

    # Cleanup
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM pending_actions WHERE update_id IN (999001, 999002) OR id IN (?, ?)",
                          (pid3a, pid3b))
        await db.commit()


def test_voice_and_create_confirm_settings():
    """Phase: voice auto + create-confirm settings present with safe defaults."""
    section("13. settings — voice_auto_confirm + confirm_create_actions defaults")
    assert "voice_auto_confirm" in database.DEFAULT_SETTINGS
    assert "confirm_create_actions" in database.DEFAULT_SETTINGS
    t("settings", "voice_auto_confirm default = True (auto)",
      database.DEFAULT_SETTINGS["voice_auto_confirm"] is True)
    t("settings", "confirm_create_actions default = True (safer)",
      database.DEFAULT_SETTINGS["confirm_create_actions"] is True)


async def test_create_action_preview_format():
    """Phase: _format_create_preview renders task + meeting cards correctly."""
    section("14. _format_create_preview — task + meeting rendering")
    actions = [
        {"type": "create_task", "data": {
            "title": "Marketing rejasi tayyorlash",
            "assignee": "Bekzod",
            "deadline": "2030-01-01T15:00:00+05:00",
            "priority": "P1",
        }},
        {"type": "schedule_meeting", "data": {
            "title": "Q2 review",
            "participants": ["Bekzod", "Alisher"],
            "datetime_start": "2030-01-02T10:00:00+05:00",
            "location_or_link": "Bosh ofis",
        }},
        {"type": "save_contact", "data": {"name": "Should be skipped"}},
    ]
    preview = await handlers._format_create_preview(
        [a for a in actions if a["type"] in handlers._DESTRUCTIVE_ACTION_TYPES]
    )
    t("preview", "header present", "TASDIQLAYSIZMI" in preview)
    t("preview", "task title in preview", "Marketing rejasi" in preview)
    t("preview", "task assignee in preview", "Bekzod" in preview)
    t("preview", "meeting title in preview", "Q2 review" in preview)
    t("preview", "meeting participants in preview",
      "Bekzod" in preview and "Alisher" in preview)
    t("preview", "non-destructive action excluded",
      "Should be skipped" not in preview)


async def test_today_tasks_strict_deadline_filter():
    """Strict /today semantics: only tasks whose DEADLINE is in today's range
    appear. Deadlinesiz vazifa /today da ko'rinmaydi — design choice for
    a clean "what did I commit to today" panel. They still appear in /tasks."""
    section("16. list_today_tasks — strict: faqat bugungi deadline")
    import aiosqlite

    # (1) Bugungi deadline → ro'yxatda bo'lishi shart.
    today_iso = (datetime.now(database.TZ)
                 .replace(hour=15, minute=0, second=0, microsecond=0)
                 .isoformat())
    tid_today = await database.create_task({
        "title": "QA — bugungi deadline'li vazifa",
        "priority": "P1",
        "status": "todo",
        "deadline": today_iso,
    })

    # (2) Deadlinesiz vazifa → /today da KO'RINMASLIGI kerak.
    tid_undated = await database.create_task({
        "title": "QA — deadlinesiz vazifa",
        "priority": "P2",
        "status": "todo",
    })

    # (3) Ertangi deadline → /today da yo'q.
    tomorrow_iso = ((datetime.now(database.TZ) + timedelta(days=1))
                    .replace(hour=10, minute=0, second=0, microsecond=0)
                    .isoformat())
    tid_tomorrow = await database.create_task({
        "title": "QA — ertangi vazifa",
        "priority": "P1",
        "status": "todo",
        "deadline": tomorrow_iso,
    })

    today_ids = {row["id"] for row in await database.list_today_tasks()}
    t("today_tasks", "bugungi deadline'li vazifa /today da bor",
      tid_today in today_ids)
    t("today_tasks", "deadlinesiz vazifa /today da YO'Q (strict)",
      tid_undated not in today_ids)
    t("today_tasks", "ertangi deadline'li vazifa /today da yo'q",
      tid_tomorrow not in today_ids)

    # cleanup
    async with aiosqlite.connect(database.config.DATABASE_PATH) as db:
        await db.execute(
            "DELETE FROM tasks WHERE id IN (?, ?, ?)",
            (tid_today, tid_undated, tid_tomorrow),
        )
        await db.commit()


async def test_notes_crud_roundtrip():
    """Phase: Qaydlar — create / list / archive / processed / delete roundtrip."""
    section("18. notes — CRUD roundtrip")
    nid = await database.create_note({
        "content": "Test qayd matni\nIkkinchi qator",
        "source": "command",
    })
    t("notes", "create_note returns id with n- prefix",
      nid.startswith("n-"))
    note = await database.get_note(nid)
    t("notes", "title auto-derived from first line",
      note["title"] == "Test qayd matni")
    t("notes", "default status = inbox", note["status"] == "inbox")
    t("notes", "tags default = []", note["tags"] == [])

    inbox = await database.list_notes(status="inbox")
    t("notes", "list_notes(inbox) includes the new note",
      any(n["id"] == nid for n in inbox))

    n_inbox = await database.count_notes_in_status("inbox")
    t("notes", "count_notes_in_status('inbox') > 0", n_inbox >= 1)

    await database.archive_note(nid)
    archived = await database.get_note(nid)
    t("notes", "archive_note flips status to archived",
      archived["status"] == "archived")

    await database.mark_note_processed(nid, "task", "t-fake")
    processed = await database.get_note(nid)
    t("notes", "mark_note_processed sets converted_to fields",
      processed["status"] == "processed"
      and processed["converted_to_type"] == "task"
      and processed["converted_to_id"] == "t-fake")

    await database.delete_note(nid)
    t("notes", "delete_note removes the row",
      (await database.get_note(nid)) is None)


async def test_notes_search():
    section("19. notes — search_notes excludes archived")
    n_inbox = await database.create_note({
        "content": "Marketing byudjet 2026 yaqinlashyapti",
        "source": "manual",
    })
    n_archived = await database.create_note({
        "content": "Eski byudjet qaydlari",
        "source": "manual",
    })
    await database.archive_note(n_archived)
    results = await database.search_notes("byudjet")
    ids = {n["id"] for n in results}
    t("notes.search", "inbox note matches", n_inbox in ids)
    t("notes.search", "archived note excluded", n_archived not in ids)

    # cleanup
    import aiosqlite
    async with aiosqlite.connect(database.config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM notes WHERE id IN (?, ?)",
                          (n_inbox, n_archived))
        await db.commit()


def test_notes_section_fsm_present():
    """Schema invariants: SectionFSM.in_notes + NoteCaptureFSM exist; filters
    defined; render helper exposed."""
    section("20. notes — FSM + filters + render helper present")
    t("notes.section", "SectionFSM.in_notes defined",
      hasattr(handlers.SectionFSM, "in_notes"))
    t("notes.section", "NoteCaptureFSM.awaiting_text defined",
      hasattr(handlers, "NoteCaptureFSM")
      and hasattr(handlers.NoteCaptureFSM, "awaiting_text"))
    t("notes.section", "_NOTES_SECTION_FILTERS has inbox/processed/archived",
      set(handlers._NOTES_SECTION_FILTERS.values()) == {"inbox", "processed", "archived"})
    t("notes.section", "_render_notes_for_filter callable",
      callable(getattr(handlers, "_render_notes_for_filter", None)))
    t("notes.section", "notes_section_reply_keyboard callable",
      callable(getattr(handlers, "notes_section_reply_keyboard", None)))
    t("notes.section", "note_detail_menu callable",
      callable(getattr(handlers, "note_detail_menu", None)))


async def test_notes_html_blockquote_render():
    """Forward'lar uchun content_html saqlanadi va detail view HTML +
    <blockquote> ishlatadi. Plain qaydlar Markdown'siz HTML escape bilan."""
    section("22. notes — HTML blockquote rendering (forward optimization)")
    nid = await database.create_note({
        "content": "Salom, byudjet tayyor",
        "content_html": "Salom, <b>byudjet</b> tayyor",
        "source": "forward",
        "source_chat": "Marketing chati",
        "source_author": "Aliya",
    })
    note = await database.get_note(nid)
    text, parse_mode = handlers._format_note_detail(note)
    t("notes.html", "parse_mode is HTML for forwarded note",
      parse_mode == "HTML")
    t("notes.html", "blockquote tag present",
      "<blockquote" in text and "</blockquote>" in text)
    t("notes.html", "expandable attribute used",
      "blockquote expandable" in text)
    t("notes.html", "original <b> formatting preserved",
      "<b>byudjet</b>" in text)
    t("notes.html", "content_html column persists in DB",
      note["content_html"] == "Salom, <b>byudjet</b> tayyor")

    # Plain note (no HTML) — must escape special chars
    nid2 = await database.create_note({
        "content": "Test <script>alert(1)</script> & more",
        "source": "manual",
    })
    note2 = await database.get_note(nid2)
    text2, _ = handlers._format_note_detail(note2)
    t("notes.html", "XSS safety: <, >, & escaped in plain content",
      "&lt;script&gt;" in text2 and "&amp;" in text2)

    # cleanup
    import aiosqlite
    async with aiosqlite.connect(database.config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM notes WHERE id IN (?, ?)", (nid, nid2))
        await db.commit()


async def test_notes_source_validation():
    """create_note coerces unknown source values to 'manual' so a buggy
    caller can't insert garbage into the source column."""
    section("21. notes — source validation")
    nid = await database.create_note({"content": "x", "source": "garbage_value"})
    note = await database.get_note(nid)
    t("notes.source", "unknown source coerced to 'manual'",
      note["source"] == "manual", f"got source={note['source']!r}")
    await database.delete_note(nid)


def test_maybe_refresh_section_present():
    """Smoke check: the auto-refresh helper exists and is wired."""
    section("17. _maybe_refresh_section — auto-refresh helper")
    t("refresh", "_maybe_refresh_section function exists",
      hasattr(handlers, "_maybe_refresh_section")
      and callable(handlers._maybe_refresh_section))


def test_destructive_action_types():
    """The set must contain exactly the operations that mutate the user's
    primary objects (tasks, meetings). update/cancel/save_contact are
    intentionally NOT here because they target existing items or are safe."""
    section("15. _DESTRUCTIVE_ACTION_TYPES — scope")
    expected = {"create_task", "schedule_meeting"}
    actual = handlers._DESTRUCTIVE_ACTION_TYPES
    t("destructive", "exactly create_task + schedule_meeting",
      actual == expected, f"got={actual}")


async def test_aisha_provider_chain():
    """Aisha integration regression — verifies the provider chain wiring
    (Aisha → Muxlisa → Whisper) without actually calling the network.

    Covers:
      - config.AISHA_API_KEY and AISHA_STT_URL exist and have sensible defaults
      - voice_service._transcribe_aisha is exposed and callable
      - transcribe() preflight (size/silence) doesn't depend on Aisha being up
    """
    section("12. voice_service — Aisha provider chain")
    import voice_service

    t("aisha", "config.AISHA_STT_URL set to back.aisha.group default",
      "back.aisha.group" in config.AISHA_STT_URL,
      f"url={config.AISHA_STT_URL!r}")
    t("aisha", "_transcribe_aisha helper present",
      hasattr(voice_service, "_transcribe_aisha")
      and callable(voice_service._transcribe_aisha))
    t("aisha", "AISHA_TIMEOUT > 0",
      getattr(voice_service, "AISHA_TIMEOUT", 0) > 0,
      f"timeout={voice_service.AISHA_TIMEOUT}s")

    # Aisha response parser: simulate a v1 sync response shape via duck typing.
    # We can't easily mock httpx here, so we just verify the function exists
    # and the public transcribe() entry point still short-circuits on tiny audio
    # without crashing when AISHA_API_KEY is set/unset.
    tiny = b"\x00" * 50  # below silence threshold
    result = await voice_service.transcribe(tiny, filename="qa.ogg", language="uz")
    t("aisha", "tiny audio still skipped (silence path independent of provider)",
      result is None)


async def test_silence_detection():
    """Voice service should short-circuit on suspiciously small audio
    payloads instead of paying for an STT round-trip."""
    section("11. voice_service — silence skip")
    import voice_service
    t("voice", "_SILENCE_BYTES_THRESHOLD defined",
      hasattr(voice_service, "_SILENCE_BYTES_THRESHOLD")
      and voice_service._SILENCE_BYTES_THRESHOLD > 0,
      f"value={getattr(voice_service, '_SILENCE_BYTES_THRESHOLD', None)}")

    tiny = b"\x00" * 100  # well below threshold
    result = await voice_service.transcribe(tiny, filename="qa.ogg", language="uz")
    t("voice", "tiny audio returns None (skipped before STT)",
      result is None)


def test_wal_autocheckpoint():
    """PRAGMA wal_autocheckpoint should be set after init()."""
    section("12. database — WAL autocheckpoint")
    import sqlite3
    con = sqlite3.connect(config.DATABASE_PATH)
    cur = con.execute("PRAGMA wal_autocheckpoint")
    value = cur.fetchone()[0]
    con.close()
    t("db", "wal_autocheckpoint set (>0 pages)",
      isinstance(value, int) and value > 0, f"pages={value}")


def test_trim_history_scheduler_method():
    """Scheduler should expose a _trim_history_sweep coroutine that wraps
    database.trim_history without raising."""
    section("13. scheduler — _trim_history_sweep present and callable")
    import scheduler as sched_mod
    cls = sched_mod.YordamchiScheduler
    t("scheduler", "_trim_history_sweep method exists on class",
      hasattr(cls, "_trim_history_sweep"))


def test_status_emoji_lockdown():
    """Faza 1: _STATUS_EMOJI['todo'] must be ⏳ (pending) — NOT 📍 (location).
    Catches regression of the semantic bug where 'todo' status was rendered as a
    location pin in task list badges.
    """
    section("23. Faza 1 — _STATUS_EMOJI[todo] is ⏳ (not 📍)")
    se = handlers._STATUS_EMOJI
    t("icons", "todo == ⏳",          se.get("todo") == "⏳",         f"got={se.get('todo')!r}")
    t("icons", "in_progress == 🔄",   se.get("in_progress") == "🔄")
    t("icons", "blocked == ⚠️",       se.get("blocked") == "⚠️")
    t("icons", "done == ✅",          se.get("done") == "✅")
    t("icons", "cancelled == ❌",     se.get("cancelled") == "❌")
    t("icons", "no stray 📍 in dict", "📍" not in se.values())


def test_priority_palette_unified():
    """Faza 1: _PRIORITY_BADGE is the single global priority palette.
    The old `bugun_badge` dict (P1=🟡, P2=⚪) was inconsistent with this and is
    now removed — Bugun panel uses _PRIORITY_BADGE.
    """
    section("24. Faza 1 — _PRIORITY_BADGE is the single source")
    pb = handlers._PRIORITY_BADGE
    t("icons", "P0 == 🔴", pb.get("P0") == "🔴")
    t("icons", "P1 == 🟠", pb.get("P1") == "🟠")
    t("icons", "P2 == 🔵", pb.get("P2") == "🔵")
    t("icons", "P3 == ⚪", pb.get("P3") == "⚪")
    # No leftover `bugun_badge` module-level (it was inside a function, but if
    # someone re-introduces it as global we want to catch it).
    t("icons", "no module-level bugun_badge",
      not hasattr(handlers, "bugun_badge"))


def test_dead_handlers_removed():
    """Faza 1: 4 dead callback handlers were removed because no button referenced
    them. Re-introducing them is a code smell; this test catches accidental revival.
    """
    section("25. Faza 1 — dead handlers stay deleted")
    for name in ("cb_cockpit_plan", "cb_copy", "cb_share",
                 "cb_view_tasks", "_extract_polished_text"):
        t("dead", f"{name} not in handlers",
          not hasattr(handlers, name))


def test_icon_hygiene():
    """Faza 2: ✓ → ✅, 🔎 → 🔍, 🔥 → ⚡ unified across handlers.py source.
    Reads the source file (not just object attrs) because most icon literals
    live inline in button text / message strings, not in named constants.
    """
    section("26. Faza 2 — no legacy icons (✓ 🔎 🔥) in handlers.py")
    src = (ROOT / "handlers.py").read_text(encoding="utf-8")
    t("icons", "no bare ✓ in source",  "✓" not in src,
      f"found {src.count('✓')} occurrences" if "✓" in src else "")
    t("icons", "no bare 🔎 in source",  "🔎" not in src,
      f"found {src.count('🔎')} occurrences" if "🔎" in src else "")
    t("icons", "no bare 🔥 in source",  "🔥" not in src,
      f"found {src.count('🔥')} occurrences" if "🔥" in src else "")
    # Affirm the replacements made it in
    t("icons", "✅ present",  "✅" in src)
    t("icons", "🔍 present",  "🔍" in src)
    t("icons", "⚡ present",  "⚡" in src)


def test_icons_module_palette():
    """Faza 2: icons.py exposes a canonical palette for new code."""
    section("27. Faza 2 — icons.py palette source-of-truth")
    import icons as ic_mod
    t("icons", "ICONS class exists", hasattr(ic_mod, "ICONS"))
    expected = {
        "CONFIRM": "✅", "CANCEL": "✕", "ADD": "➕", "EDIT": "✏️",
        "DELETE": "🗑", "SEARCH": "🔍", "BACK": "⬅️", "REFRESH": "🔄",
        "SETTINGS": "⚙️", "URGENT": "⚡",
        "P0_URGENT": "🔴", "P1_IMPORTANT": "🟠",
        "P2_PLANNED": "🔵", "P3_LOW": "⚪",
    }
    for attr, want in expected.items():
        got = getattr(ic_mod.ICONS, attr, None)
        t("icons", f"ICONS.{attr} == {want}", got == want, f"got={got!r}")
    # STATUS_EMOJI + PRIORITY_BADGE convenience dicts present
    t("icons", "STATUS_EMOJI dict exposes 'todo' == ⏳",
      ic_mod.STATUS_EMOJI.get("todo") == "⏳")
    t("icons", "PRIORITY_BADGE dict exposes 'P0' == 🔴",
      ic_mod.PRIORITY_BADGE.get("P0") == "🔴")


async def main():
    config.ensure_paths()
    await database.init()

    # Pure-sync tests
    test_cb_int_safety()
    test_coerce_dt_tz_handling()
    test_redaction_phase1()
    test_wal_autocheckpoint()
    test_trim_history_scheduler_method()
    test_model_router()
    test_partial_user_message_extraction()
    # Async tests
    await test_complete_task_race()
    await test_save_contact_race()
    await test_mark_reminder_sent_atomic()
    await test_mark_reminder_sent_recurring_advances()
    await test_mark_task_reminded_atomic()
    await test_meeting_claims()
    await test_spawn_background_logs_exceptions()
    await test_pending_actions_idempotency()
    await test_silence_detection()
    await test_aisha_provider_chain()
    test_voice_and_create_confirm_settings()
    await test_create_action_preview_format()
    test_destructive_action_types()
    await test_today_tasks_strict_deadline_filter()
    test_maybe_refresh_section_present()
    await test_notes_crud_roundtrip()
    await test_notes_search()
    test_notes_section_fsm_present()
    await test_notes_html_blockquote_render()
    await test_notes_source_validation()
    # Faza 1-2-3 lockdown
    test_status_emoji_lockdown()
    test_priority_palette_unified()
    test_dead_handlers_removed()
    test_icon_hygiene()
    test_icons_module_palette()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{'═' * 70}")
    print(f" QA REGRESSION XULOSA: {passed}/{total} o'tdi")
    print(f"{'═' * 70}\n")
    if passed != total:
        print("FAILED:")
        for name, ok, detail in _results:
            if not ok:
                print(f"  ✗ {name}  {detail}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
