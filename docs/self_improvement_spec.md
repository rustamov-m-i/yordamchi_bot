# Self-Improvement Subsystem — Implementation Spec
*Yordamchi bot · supervised, human-gated, self-healing · v3*

---

## 0 — How to use this document

This file is the **single source of truth** for the build. To implement:

1. Open a Claude Code (or Agent SDK) session at the repo root.
2. Say: **"Read `docs/self_improvement_spec.md`. Implement Phase 1 only. Plan first, stop for approval."**
3. Re-read this file at the start of **every** phase. **One phase = one PR.**

Do **not** implement more than the requested phase in a single run. Do **not**
paste this spec into chat — point the agent at the file (keeps it versioned,
cacheable, and diffable over time).

---

## 1 — Mission & operating principle

You are a senior Python engineer on **Yordamchi**, a single-user executive
assistant (Telegram bot, banking context). Build a **supervised self-improvement
subsystem**: the bot observes its own behaviour, proposes and implements both
**fixes** and **new features**, and — with the principal approving **every one of
6 gates** — ships, deploys, and self-heals on failure.

Improvement flows through **two channels** (see §3), both passing the same 6 gates:
- **A — autonomous:** the bot mines its own usage data and proposes mostly fixes.
- **B — principal-requested:** the principal asks for a feature via `/improve`.

**Operating principle: the bot is the hands; the principal is the brain.**
The bot does the mechanical work. The principal makes every decision. Nothing
reaches `main` or the server without an explicit, per-step approval. The **only**
autonomous action in the entire system is a **backward rollback to a known-good
state** when a deploy fails its health check (Phase 5). Rollback never moves
*forward* to new, untested code.

---

## 2 — Project ground truth (verify against the repo; do not assume)

- aiogram 3.x async Telegram bot; single principal enforced via `config.PRINCIPAL_USER_ID`.
- LLM access: `claude_service.py` — Anthropic Claude, strict **JSON-envelope** output
  contract, model router (`fast`/`default`/`complex`), streaming, circuit breaker.
  Internal (non-user) calls use an `internal_directive` like `[INTERNAL] ...`.
- DB: async SQLite (aiosqlite) in `database.py`. Existing tables include tasks,
  reminders, meetings, notes, contacts, corrections, principal_profile,
  conversation_history, pending_actions. Every LLM call is audited via
  `database.log_llm_call(...)` (purpose, model, tokens, cost, error label).
- System prompt is modular: `system_prompts/*.md`, concatenated in filename order,
  **prompt-cached** (`cache_control: ephemeral`). Assembled in `config._load_system_prompt()`.
  → Editing any prompt file invalidates the cache; account for re-cache cost.
- Scheduler: APScheduler in `scheduler.py` (08:00/18:00 briefings, follow-ups).
- PII redaction: `redaction.py`. Existing learning loop: `save_correction` →
  `corrections` table → surfaced as the `## STYLE CORRECTIONS` block in
  `claude_service._build_state_block()`.
- UI conventions: inline buttons use a `confirm:<token>` / `cancel:<token>` callback
  pattern (see `system_prompts/60_output_contract.md`). Existing commands include
  `/tasks`, `/meetings`, `/notes`, `/cockpit`, `/plan`.
- Tests: `tests/` (pytest); CI: `.github/workflows/test.yml`.
- Git: remote `github.com/rustamov-m-i/yordamchi_bot`; `gh` CLI authenticated.
- **Runtime: Google Compute Engine `e2-micro` VM, managed by systemd service
  `yordamchi.service`** (`ExecStart=.../venv/bin/python bot.py`, `Restart=always`,
  `RestartSec=10`). Manual update today = `git pull` + `sudo systemctl restart yordamchi`.

---

## 3 — What the system can build, and its limits

Two channels feed the implementation engine. **Both pass through the same 6 gates.**

**Channel A — Autonomous (nightly diagnosis).** Mines usage data (corrections,
errors, fallbacks, "unmet request" signals) and proposes mostly **fixes** and small
refinements. It proposes a *new* feature only when the data clearly shows a gap
(e.g., the principal repeatedly asks for something the bot can't do). Its
new-feature output is intentionally rare — it needs a signal.

**Channel B — Principal-requested (`/improve <request>`).** The principal asks for a
feature or fix in Telegram; the engine builds it. This is the **primary path for new
functionality** and never "dries up".

**What it can add well:** new commands, buttons, task/reminder fields, filters,
export formats, briefing sections, recurrence types — any reasonable change within
the existing architecture.

**Honest limits (respect them; never work around):**
1. **Large / architectural features** (multi-user, a whole new module) are NOT for
   the autonomous channel and should not be one-line `/improve` requests. They
   require the principal to provide a written spec (like this document). Note this
   in the proposal and stop.
2. **Protected paths** (§9) → flag "requires manual implementation"; never auto-build.
3. **Features needing a new secret/API key** → write the code, but the principal
   wires the secret into `.env` manually (the agent never touches `.env`).
4. **Complex/novel features** may produce an imperfect first PR — tests + the diff
   gate catch this; expect iteration. Never claim success without green tests.
5. The system **extends what exists**; it does NOT decide product direction.

---

## 4 — The 6-gate flow (the heart of this design)

The principal approves **every** gate. The bot stops and waits at each one.
Fixes and new features both use this exact flow.

| Gate | Bot does | What changes | Principal sees & approves | Phase |
|------|----------|--------------|---------------------------|-------|
| —    | Self-diagnose → proposal (Channel A) | nothing | (proposals appear in briefing) | 2 |
| **1** | (waits) | nothing | the **proposal / `/improve` scope** → ✅ start | 3 |
| **2** | Write code + tests in an isolated worktree, run `pytest` | worktree only | the **full diff + pytest result** → ✅ | 4 |
| **3** | Push the branch to GitHub | GitHub (branch) | (diff already seen) → ✅ push | 4 |
| **4** | Merge the PR into `main` | GitHub repo (`main`) | the **PR** → ✅ merge | 4 |
| **5** | Deploy to server (`git pull` on the VM) | **the server** | → ✅ deploy | 5 |
| **6** | Restart via the deployer + health-check | the running process | → ✅ restart | 5 |
| auto | If unhealthy → **rollback** to known-good + restart | server back to safe | (notified after the fact) | 5 |

If `pytest` fails at Gate 2, the bot does **not** offer to proceed — it reports the
failure and stops. Tests are a hard gate, not a step.

**Tap-reduction option:** consecutive low-risk gates (e.g., diff-approval → push)
MAY be combined into a single approval to reduce interruptions. Make this
configurable; default to separate. **Never** auto-combine Gates 5/6 (deploy/restart).

---

## 5 — Where the principal interacts (no new app to build)

All interaction happens in the **existing Telegram bot chat**, plus **GitHub** for
detailed code review. Build no separate dashboard or website.

**Commands to add:**
- `/improvements` — list pending Channel-A proposals, each with approve/reject buttons.
- `/improve <request>` — request a new feature or fix (Channel B).
- `/autopilot on|off` — master kill switch for the whole loop.

**Per-gate approval = Telegram inline buttons** (reuse the existing `confirm:<token>`
mechanism). Gate 4 (merge) links out to the GitHub PR; optionally also offer an
in-Telegram `[✅ Merge]` button via `gh pr merge` so GitHub is never strictly required.

**Notifications** (proposal ready, gate prompts, deploy result, rollback) arrive as
Telegram messages to the principal; a morning-briefing line surfaces the pending
proposal count.

Mock screens:
```
/improvements →
💡 Yaxshilanish takliflari (3)
1. «Hisobot» vazifalarini P1 qilish — 7 kunda 11 marta tuzatdingiz
   [✅ Tasdiqlash]  [❌ Rad et]  [📄 Batafsil]

Gate 2 →
🔧 Taklif #1 tayyor (Darvoza 2/6)
Fayl: system_prompts/20_task_capture.md (+4 −1) · Test: 150 passed ✅
Himoyalangan fayl: tegilmadi ✅
[👁 Diff]  [✅ Davom et]  [❌ Bekor]

Deploy result →
✅ Deploy muvaffaqiyatli — bot sog'lom. #42 · 14:32
   (yoki) ⚠️ Deploy buzildi — eski versiyaga qaytarildi. Bot ishlayapti.
```

---

## 6 — STEP 0: PLAN FIRST (mandatory, every phase)

Before writing **any** code for a phase, produce a short design plan:
files you will add/change, the data model, the trigger wiring, and exactly how you
will test it. **STOP and post the plan for approval.** Write no code until the
principal replies "approved". If the plan must touch a **protected path**
(see §9), say so explicitly and propose an alternative instead.

The module/table names below are a **proposed** design — confirm or improve them in
your plan. Do not treat them as fixed requirements.

---

## 7 — Phased delivery (one reviewable PR per phase)

### Phase 1 — Perception (telemetry)
- Add `metrics.py`: aggregate from the existing LLM audit log + error logs into
  queryable signals — error-rate by label, fallback frequency, `save_correction`
  frequency by theme, cost/latency trend, and an "unmet request" heuristic
  (turns with intent=`none` or an immediate user rephrase).
- Add read helpers in `database.py`. Add a light `improvement_metrics_daily` rollup
  table only if needed. **No user-visible behaviour change. Read-only.**
- Self-check: existing tests pass; new tests cover the aggregation math.

### Phase 2 — Diagnosis (Channel A)
- New nightly job in `scheduler.py` (~02:00) calling `claude_service` with a new
  directive `[INTERNAL] self_diagnose`, fed the Phase-1 metrics + recent
  corrections + recent fallback samples. Use the `complex` model tier (low
  frequency, high judgement).
- Output → `improvement_proposals` table: `id, created_at, source (auto|manual),
  title, problem, evidence, root_cause, fix_kind (prompt|code|config|data|feature),
  proposed_change, impact_estimate, status (new|approved|rejected|in_progress|
  pr_open|merged|deployed|reverted|done)`.
- If a proposal is large/architectural (per §3 limit 1), set it aside as
  "requires manual spec" — do not queue it for auto-implementation.
- Self-check: a proposal row is produced from seeded fixture metrics.

### Phase 3 — Proposal & request gate **[Gate 1]**
- New `/improvements` command + a morning-briefing line ("N ta yaxshilanish taklifi").
- New `/improve <request>` command — the manual (Channel B) entry that creates a
  `source=manual` proposal and immediately presents Gate 1 to confirm scope.
- Render each item with inline buttons `[✅ Tasdiqlash] [❌ Rad etish] [📄 Batafsil]`.
- Approval flips status to `approved`. **Never** start implementation without it.
- Self-check: rejecting an item never triggers Phase 4.

### Phase 4 — Implementation engine **[Gates 2–4]**
- New `dev_agent.py`: given an `approved` proposal (Channel A) **or** an `/improve`
  request (Channel B), spawn the **Claude Agent SDK** in an **isolated git worktree**
  (never the live tree). It must: implement the change, **add/extend tests**, run
  `pytest`, and only if green → present the diff + results (**Gate 2**). On approval
  → push the branch (**Gate 3**) → `gh pr create` → on approval → merge (**Gate 4**).
- `fix_kind=prompt` → edit `system_prompts/*.md`. `fix_kind=feature/code` → edit
  `handlers.py`/`database.py`/etc. — both still via PR through the same gates.
- If the work needs a secret/API key (§3 limit 3), build the code but report that
  the principal must add the key to `.env` before Gate 5.
- Post the PR link to the principal; advance proposal status at each gate.
- Self-check: a tiny seeded proposal AND a tiny `/improve` request each produce a
  real PR touching only allowed paths, with the principal prompted at Gates 2–4.

### Phase 5 — Supervised deploy + self-heal **[Gates 5–6]** — *highest stakes, see §8*
- Build the **separate deployer** (§8). On principal approval (**Gate 5** deploy,
  **Gate 6** restart) it: records the known-good commit → `git pull` → restart →
  health-check → **auto-rollback on failure** → notify.
- Self-check: **deliberately deploy a broken commit and prove it rolls back** to the
  previous good commit and the bot comes back alive.

### Phase 6 — Feedback (suggest-only)
- After a deploy, compare the targeted metric over a window. If it regressed, create
  a **new** proposal "Consider reverting #X" (suggest only — never auto-revert
  forward changes).

---

## 8 — Phase 5 in depth: the deployer & auto-rollback

This is the **highest-stakes code in the system.** Build it most carefully.

**Why a separate process:** a dead bot cannot roll itself back. The deploy /
restart / health-check / rollback logic MUST live **outside the bot process** — a
small `deployer` (shell script or `deployer.py` run as a systemd *oneshot*, or a
tiny always-on watchdog). The bot's only role is to record the principal's
approval; the deployer does the rest and survives the bot dying.

**Suggested mechanism (confirm in plan):**
1. Principal taps ✅ deploy/restart → bot writes a `deploy_request` signal (DB row
   or file) with the target commit. Bot does **not** run `git`/`systemctl` itself.
2. The deployer picks up the signal and:
   - `GOOD=$(git rev-parse HEAD)` — record the currently-running good commit
   - `git pull` (or checkout the target) → `sudo systemctl restart yordamchi`
   - **Health-check** for up to ~60s: service is `active` AND the bot wrote a fresh
     startup heartbeat (suggest: bot touches a heartbeat file / DB timestamp on
     successful startup and every ~30s).
   - **Healthy** → mark proposal `deployed`, notify "✅ Deploy muvaffaqiyatli — bot sog'lom."
   - **Unhealthy** → `git reset --hard $GOOD` → restart → re-check → notify
     "⚠️ Deploy buzildi — eski versiyaga qaytarildi (rollback). Bot ishlayapti."
3. Rollback only ever returns to `$GOOD`. It never advances to untested code.

**The deployer and its trigger path are protected (§9) — the dev_agent must never
modify them.** Test the rollback path explicitly (intentional broken commit) as
part of Phase 5 acceptance.

---

## 9 — Hard guardrails (non-negotiable — banking compliance)

1. **Protected paths** — the dev_agent must REFUSE to modify, and the worktree must
   block: `.env`, `config.py`, `redaction.py`, the `PRINCIPAL_USER_ID` access guard,
   `dev_agent.py` itself, the **deployer** and its trigger, and CI files. Any
   proposal/request touching these is flagged "requires manual implementation" —
   never auto-implemented.
2. **No secret access** — the agent's environment must not expose API keys; `.env`
   stays git-ignored and unreadable to the agent.
3. **Every gate is human-approved.** No ungated merge, deploy, or restart. The only
   autonomous action permitted anywhere is the backward rollback in §8.
4. **Budget + kill switch** — hard per-run token cap and a daily cap on
   self-improvement LLM spend; on breach, stop and notify. `/autopilot off` disables
   the whole loop instantly.
5. **Scoped server credentials** — the deployer's SSH/sudo rights are limited to
   exactly `git pull` and `systemctl restart/reset` of `yordamchi.service`. Nothing
   broader.
6. **Full audit trail** — every proposal, request, approval, action, deploy, and
   rollback is logged to a `self_improvement_audit` table (reuse `log_llm_call` for
   LLM spend). Must be reviewable end to end.
7. **Isolation** — all implementation runs in a throwaway git worktree; the live
   tree and process are never edited in place.

---

## 10 — Report contract (after every phase / gate, report EXACTLY)

- **Changed files** — one line each.
- **Tests** — `pytest` summary (N passed / N failed).
- **Protected-path check** — "none touched" OR the explicit list (and stop).
- **PR link** (when applicable).
- **Tokens/cost this run** vs the cap.
- **What you did NOT do, and any open questions.**

---

## 11 — Worked examples

### Example A — an autonomous FIX (Channel A), full loop + a rollback

```
Phase 1 signal: 11 `save_correction` rows in 7 days, all flipping P2→P1 on
  tasks whose title contains "hisobot". Fallback rate normal.

Phase 2 proposal: { source: auto, title: "Default 'hisobot' tasks to P1",
  fix_kind: prompt, proposed_change: "Add one rule to 20_task_capture.md",
  impact_estimate: "~1.5 fewer corrections/day" }

Gate 1 — principal opens /improvements, taps ✅ Tasdiqlash.
Gate 2 — dev_agent edits 20_task_capture.md in a worktree, adds a regression test,
  pytest 149 → 150 passed. Diff shown, no protected path. → ✅.
Gate 3 push → Gate 4 merge PR #42.
Gate 5 deploy (GOOD=abc123, git pull) → Gate 6 restart, health-check 60s.
  Healthy → "✅ Deploy muvaffaqiyatli — bot sog'lom."
  (Broken variant) → no heartbeat → git reset --hard abc123 → restart → healthy →
  "⚠️ Deploy buzildi — eski versiyaga qaytarildi. Bot ishlayapti." → Phase 6 follow-up.
```

### Example B — a NEW feature (Channel B) via /improve

```
Principal types: /improve eslatmalarga "kechiktirish" (snooze) tugmasi qo'sh

Gate 1 — bot confirms scope: "Snooze (15m / 1h / ertaga) tugmasini eslatmalarga
  qo'shaman. Davom etaymi?" → ✅.
Gate 2 — dev_agent (worktree) adds the inline button + `snooze:<id>:<delta>`
  callback in handlers.py, a reschedule helper in database.py, and tests.
  pytest 150 → 153 passed. Diff shown, no protected path. → ✅.
Gate 3 push → Gate 4 merge → Gate 5 deploy → Gate 6 restart, health-check passes.
  "✅ Snooze tugmasi qo'shildi va joylandi — bot sog'lom."

If a request needed a secret (e.g., a weather API), Gate 2 still produces the code
but reports: "⚠️ WEATHER_API_KEY ni .env ga qo'shing, keyin deploy'ni tasdiqlang."
The agent never edits .env.
```

---

## 12 — Acceptance criteria

- All existing tests pass; each phase adds tests for its own logic.
- Phases 1–2 run with zero user-visible behaviour change.
- An approved Channel-A proposal AND an `/improve` (Channel-B) request each flow
  through Gates 1–4 to a real PR touching only allowed paths, with the principal
  prompted at each gate.
- Modifying a protected path is provably blocked (write a test for it).
- A request needing a secret produces code + a "wire the key" message, never an
  `.env` edit.
- Phase 5: an intentionally broken deploy provably auto-rolls-back and the bot
  returns alive on the previous good commit.
- `/autopilot off` halts the loop; a budget breach halts and notifies.

---

## 13 — Non-goals (explicitly DO NOT build)

- Any **ungated** merge, deploy, restart, or forward auto-update.
- Autonomous *forward* changes of any kind (the only auto action is backward rollback).
- Autonomous building of **large/architectural** features — those require a written
  spec from the principal.
- Modifying authentication, redaction, config security, or the deployer without a human.
- Multi-user support.

> New features ARE in scope — but only through the human-approved gated channels
> (A and B). What is out of scope is *ungated* autonomy and *autonomous big-feature*
> invention.

---

## 14 — Working agreement

- Work on a feature branch; never commit directly to `main`.
- Read the relevant existing module before changing it; match its style and the
  JSON-envelope / internal-directive / `confirm:<token>` conventions already in the repo.
- If a requirement conflicts with a guardrail, **STOP and ask** — do not work around it.
- Keep each PR small and reviewable; update `README.md` for any new command.
- Treat Phase 5 (deployer/rollback) as the highest-risk work: smallest possible
  surface, most explicit tests, no shortcuts.
