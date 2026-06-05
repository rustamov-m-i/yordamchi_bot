"""Async SQLite operations for Yordamchi.

Tables: tasks, reminders, meetings, contacts, corrections, principal_profile, conversation_history, pending_actions.
All datetime fields are stored as ISO 8601 strings in Asia/Tashkent timezone.
"""

import json
import logging
import uuid
from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any, Optional

import aiosqlite
import pytz

import config

logger = logging.getLogger(__name__)
TZ = pytz.timezone(config.TIMEZONE)


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def new_id(prefix: str = "") -> str:
    short = uuid.uuid4().hex[:10]
    return f"{prefix}{short}" if prefix else short


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    deadline TEXT,
    priority TEXT CHECK(priority IN ('P0','P1','P2','P3')) DEFAULT 'P2',
    status TEXT CHECK(status IN ('todo','in_progress','blocked','done','cancelled')) DEFAULT 'todo',
    tags TEXT,
    assignee TEXT,
    recurrence_rule TEXT,
    recurrence_next_at TEXT,
    recurrence_parent_id TEXT,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reminded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
-- Hot paths flagged in audit: assignee filtering (team panel),
-- source-tagged queries (recurring/manual filters), reminded_at IS NULL scans.
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_source_created ON tasks(source, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_reminded ON tasks(reminded_at);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    datetime_start TEXT NOT NULL,
    datetime_end TEXT,
    participants TEXT,
    location_or_link TEXT,
    agenda TEXT,
    prep_notes TEXT,
    follow_up_actions TEXT,
    reminded_at TEXT,
    prep_sent_at TEXT,
    followup_sent_at TEXT,
    icloud_uid TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_meetings_start ON meetings(datetime_start);
-- Hot paths: reminded_at IS NULL scans for sweep claim;
-- followup_sent_at IS NULL for post-meeting follow-up sweep.
CREATE INDEX IF NOT EXISTS idx_meetings_reminded ON meetings(reminded_at);
CREATE INDEX IF NOT EXISTS idx_meetings_followup ON meetings(followup_sent_at);
-- idx_meetings_icloud is created in init() migration block (needs column to exist first
-- on databases that were created before this column was added).

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    role TEXT,
    formality_level INTEGER DEFAULT 3,
    preferred_channel TEXT,
    last_interaction TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    context TEXT,
    correction TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS principal_profile (
    key TEXT PRIMARY KEY,
    data TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conv_created ON conversation_history(created_at DESC);

CREATE TABLE IF NOT EXISTS llm_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    purpose TEXT,
    input_hash TEXT,
    input_chars INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_creation_tokens INTEGER,
    redacted_terms_count INTEGER DEFAULT 0,
    estimated_cost_usd REAL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON llm_audit_log(ts DESC);

CREATE TABLE IF NOT EXISTS task_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    action TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_task ON task_history(task_id, ts DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    note TEXT,
    remind_at TEXT NOT NULL,
    status TEXT CHECK(status IN ('scheduled','sent','done','cancelled')) DEFAULT 'scheduled',
    recurrence_rule TEXT,
    task_id TEXT,
    meeting_id TEXT,
    source TEXT,
    snooze_count INTEGER DEFAULT 0,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_status_time ON reminders(status, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_task ON reminders(task_id);
CREATE INDEX IF NOT EXISTS idx_reminders_meeting ON reminders(meeting_id);
-- Time-window scans (list_due_in_window, reminders_overview) benefit from a
-- standalone remind_at index. Note: SQLite is smart enough to use the leading
-- column of idx_reminders_status_time(status, remind_at) for status-filtered
-- queries, so this only helps date-range queries that don't filter on status.
CREATE INDEX IF NOT EXISTS idx_reminders_time ON reminders(remind_at);
-- NOTE: FOREIGN KEY constraints on reminders.task_id → tasks.id and
-- reminders.meeting_id → meetings.id were intentionally NOT added.
-- SQLite doesn't allow ALTER TABLE ADD CONSTRAINT, so retrofitting would
-- require full table recreation. Tracked as future work (Sprint C1.future):
-- needs orphan cleanup + migration script + downtime window.

CREATE TABLE IF NOT EXISTS icloud_retry_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,
    meeting_id TEXT,
    payload TEXT,
    attempts INTEGER DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_retry_next ON icloud_retry_queue(next_attempt_at);

-- Quick-capture inbox ("Qaydlar") — GTD-style notes waiting to be triaged.
-- Sources: forward, /qayd command, voice via LLM, manual via section UI.
-- Once converted to a task or reminder, status flips to 'processed' and
-- converted_to_{id,type} link to the produced item. Archived notes stay
-- in the table for full-text search but drop out of the inbox view.
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    -- HTML-formatted copy of the content (Telegram entities preserved as HTML
    -- tags). Used when rendering a forwarded note inside a <blockquote> so the
    -- original bold/italic/links/code formatting survives the round-trip.
    -- NULL for non-forward sources or older rows; render path falls back to
    -- escaped plain content.
    content_html TEXT,
    title TEXT,
    source TEXT NOT NULL,
    source_chat TEXT,
    source_author TEXT,
    source_message_id INTEGER,
    tags TEXT,
    status TEXT CHECK(status IN ('inbox','processed','archived'))
                 DEFAULT 'inbox' NOT NULL,
    converted_to_id TEXT,
    converted_to_type TEXT CHECK(converted_to_type IN ('task','reminder')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notes_status_created
    ON notes(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notes_source
    ON notes(source, created_at DESC);

-- Executive planning sessions (one /plan invocation = one row)
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    input_text TEXT NOT NULL,
    output_text TEXT NOT NULL,
    accepted INTEGER DEFAULT 0,           -- did the principal explicitly accept
    follow_up_asked_at TEXT,              -- when did we ask "how did this plan go"
    follow_up_response TEXT,              -- principal's review of the plan
    task_ids TEXT                          -- JSON array of task IDs created from this plan
);

CREATE INDEX IF NOT EXISTS idx_plans_created ON plans(created_at DESC);

-- Idempotency / crash-recovery queue for in-flight user message handling.
-- Pattern: enqueue BEFORE Claude call (state=pending) → mark in_progress →
-- complete on success → fail on exception. On bot restart, stuck rows
-- (state in {pending,in_progress} and older than ~5 min) are surfaced
-- so the principal knows their request was dropped instead of silently lost.
-- update_id is the Telegram update id and is UNIQUE so a redelivered update
-- (unusual but possible) cannot be double-processed.
CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER UNIQUE,
    chat_id INTEGER,
    message_id INTEGER,
    user_text TEXT,                        -- original input, redacted before LLM
    state TEXT NOT NULL DEFAULT 'pending', -- pending | in_progress | completed | failed
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_state ON pending_actions(state, updated_at);
"""


async def init() -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        # WAL mode: allow concurrent reads while a write is in progress, drastically
        # reducing "database is locked" errors when scheduler + handler + webapp coincide.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=5000")
        # Auto-checkpoint every 1000 pages keeps the WAL file from growing
        # unbounded between manual checkpoints; without this it can balloon
        # to >1GB after weeks of writes.
        await db.execute("PRAGMA wal_autocheckpoint=1000")
        await db.executescript(SCHEMA)

        # Idempotent migrations for existing DBs (CREATE TABLE IF NOT EXISTS doesn't add columns)
        cur = await db.execute("PRAGMA table_info(meetings)")
        meeting_cols = {row[1] for row in await cur.fetchall()}
        if "icloud_uid" not in meeting_cols:
            await db.execute("ALTER TABLE meetings ADD COLUMN icloud_uid TEXT")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_meetings_icloud ON meetings(icloud_uid)")

        cur = await db.execute("PRAGMA table_info(tasks)")
        task_cols = {row[1] for row in await cur.fetchall()}
        if "assignee" not in task_cols:
            await db.execute("ALTER TABLE tasks ADD COLUMN assignee TEXT")
        for col in ("recurrence_rule", "recurrence_next_at", "recurrence_parent_id"):
            if col not in task_cols:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_tasks_recurrence ON tasks(recurrence_rule, recurrence_next_at)")

        for col in ("prep_sent_at", "followup_sent_at", "completed_at"):
            if col not in meeting_cols:
                await db.execute(f"ALTER TABLE meetings ADD COLUMN {col} TEXT")

        # Notes: content_html column added later — backfill on existing rows
        # is unnecessary (renderer falls back to escaped content when NULL).
        try:
            cur = await db.execute("PRAGMA table_info(notes)")
            note_cols = {row[1] for row in await cur.fetchall()}
            if note_cols and "content_html" not in note_cols:
                await db.execute("ALTER TABLE notes ADD COLUMN content_html TEXT")
        except Exception:
            # notes table didn't exist before this run — the CREATE TABLE
            # above already includes content_html, nothing to migrate.
            pass

        await db.commit()


# ─────────────────────────────────────────── TASKS ───────────────────────────────────────────

async def _log_history(db, task_id: str, action: str, field: Optional[str] = None,
                        old_value: Optional[str] = None, new_value: Optional[str] = None,
                        source: Optional[str] = None) -> None:
    await db.execute(
        """INSERT INTO task_history (task_id, ts, action, field, old_value, new_value, source)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (task_id, now_iso(), action, field,
         str(old_value) if old_value is not None else None,
         str(new_value) if new_value is not None else None,
         source),
    )


async def create_task(data: dict) -> str:
    task_id = new_id("t-")
    now = now_iso()
    source = data.get("source", "telegram_text")
    recurrence_rule = normalize_recurrence_rule(data.get("recurrence_rule") or data.get("recurrence"))
    recurrence_next_at = data.get("recurrence_next_at")
    if recurrence_rule and not recurrence_next_at:
        recurrence_next_at = compute_next_recurrence(data.get("deadline"), recurrence_rule)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO tasks (id, title, description, deadline, priority, status, tags, assignee,
                                  recurrence_rule, recurrence_next_at, recurrence_parent_id,
                                  source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                data.get("title", "Vazifa"),
                data.get("description"),
                data.get("deadline"),
                data.get("priority", "P2"),
                data.get("status", "todo"),
                json.dumps(data.get("tags", []), ensure_ascii=False),
                data.get("assignee"),
                recurrence_rule,
                recurrence_next_at,
                data.get("recurrence_parent_id"),
                source,
                now,
                now,
            ),
        )
        await _log_history(db, task_id, "create", field=None,
                            new_value=data.get("title", "Vazifa"), source=source)
        await db.commit()
    return task_id


async def update_task(task_id: str, data: dict, source: str = "manual") -> bool:
    if not data:
        return False
    # Read old values first so we can log diffs.
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        before = await cur.fetchone()
        if before is None:
            return False

        fields = []
        values = []
        changes: list[tuple[str, str, str]] = []  # (field, old, new)
        for key in (
            "title", "description", "deadline", "priority", "status", "source", "assignee",
            "recurrence_rule", "recurrence_next_at", "recurrence_parent_id",
        ):
            if key in data:
                if key == "recurrence_rule":
                    data[key] = normalize_recurrence_rule(data[key])
                fields.append(f"{key} = ?")
                values.append(data[key])
                changes.append((key, before[key] if key in before.keys() else None, data[key]))
        if "tags" in data:
            fields.append("tags = ?")
            values.append(json.dumps(data["tags"], ensure_ascii=False))
            changes.append(("tags", before["tags"], json.dumps(data["tags"], ensure_ascii=False)))
        if not fields:
            return False
        # Deadline o'zgarsa, eski reminded_at ni tozalash — yangi muddatga qarab
        # qayta eslatma yuborilishi uchun (_task_reminder_sweep'ga yo'l ochish).
        if "deadline" in data and str(before["deadline"]) != str(data.get("deadline")):
            fields.append("reminded_at = ?")
            values.append(None)
        fields.append("updated_at = ?")
        values.append(now_iso())
        values.append(task_id)
        cur = await db.execute(
            f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values
        )
        for field, old, new in changes:
            if str(old) != str(new):
                await _log_history(db, task_id, "update", field=field,
                                    old_value=old, new_value=new, source=source)
        await db.commit()
        return cur.rowcount > 0


async def complete_task(task_id: str) -> bool:
    """Mark a task as done atomically. Only the first concurrent caller wins —
    subsequent callers see status='done' and return True without re-creating
    the recurring follow-up. This eliminates the duplicate-recurrence race
    between two handlers completing the same task."""
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        before = await cur.fetchone()
        if before is None:
            return False
        if before["status"] == "done":
            return True
        # Conditional UPDATE: another caller may have flipped status between our
        # SELECT and UPDATE. Only the caller whose rowcount > 0 owns the side-effects.
        cur = await db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status != ?",
            ("done", now, task_id, "done"),
        )
        won = cur.rowcount > 0
        if won:
            await _log_history(db, task_id, "update", field="status",
                                old_value=before["status"], new_value="done",
                                source="complete")
        await db.commit()
    if not won:
        return True
    completed = _row_to_task(before)
    if completed.get("recurrence_rule"):
        await create_next_recurring_task(completed)
    return True


def normalize_recurrence_rule(raw: Any) -> Optional[str]:
    """Normalize LLM/user recurrence wording to a small supported rule set."""
    if not raw:
        return None
    value = str(raw).strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "daily": "daily", "every day": "daily", "har kuni": "daily", "kunlik": "daily",
        "weekly": "weekly", "every week": "weekly", "har hafta": "weekly", "haftalik": "weekly",
        "monthly": "monthly", "every month": "monthly", "har oy": "monthly", "oylik": "monthly",
        "quarterly": "quarterly", "every quarter": "quarterly", "har chorak": "quarterly", "choraklik": "quarterly",
        "yearly": "yearly", "annual": "yearly", "every year": "yearly", "har yil": "yearly", "yillik": "yearly",
    }
    return aliases.get(value)


def _coerce_dt(iso: Optional[str]) -> datetime:
    """Parse an ISO timestamp into an Asia/Tashkent-aware datetime. Aware
    inputs in another zone are converted (not localized — calling
    pytz.localize on an already-aware dt raises and silently drifts the
    wall-clock time). Returns now() on parse failure."""
    if iso:
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                return dt.astimezone(TZ)
            return TZ.localize(dt)
        except (TypeError, ValueError):
            pass
    return datetime.now(TZ)


def parse_iso_dt(iso: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp into an Asia/Tashkent-aware datetime, or None
    if the input is missing/invalid. See _coerce_dt for tz-handling notes."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is not None:
            return dt.astimezone(TZ)
        return TZ.localize(dt)
    except (TypeError, ValueError):
        return None


def _add_months(dt: datetime, months: int) -> datetime:
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def compute_next_recurrence(base_iso: Optional[str], recurrence_rule: Optional[str]) -> Optional[str]:
    rule = normalize_recurrence_rule(recurrence_rule)
    if not rule:
        return None
    current = _coerce_dt(base_iso)
    now = datetime.now(TZ)

    for _ in range(36):
        if rule == "daily":
            current += timedelta(days=1)
        elif rule == "weekly":
            current += timedelta(weeks=1)
        elif rule == "monthly":
            current = _add_months(current, 1)
        elif rule == "quarterly":
            current = _add_months(current, 3)
        elif rule == "yearly":
            current = _add_months(current, 12)
        else:
            return None
        if current > now:
            return current.isoformat()
    return current.isoformat()


async def create_next_recurring_task(completed_task: dict) -> Optional[str]:
    rule = normalize_recurrence_rule(completed_task.get("recurrence_rule"))
    if not rule:
        return None
    next_deadline = compute_next_recurrence(completed_task.get("deadline"), rule)
    if not next_deadline:
        return None
    next_data = {
        "title": completed_task.get("title") or "Vazifa",
        "description": completed_task.get("description"),
        "deadline": next_deadline,
        "priority": completed_task.get("priority", "P2"),
        "status": "todo",
        "tags": completed_task.get("tags", []),
        "assignee": completed_task.get("assignee"),
        "recurrence_rule": rule,
        "recurrence_next_at": compute_next_recurrence(next_deadline, rule),
        "recurrence_parent_id": completed_task.get("recurrence_parent_id") or completed_task.get("id"),
        "source": "recurring",
    }
    new_task_id = await create_task(next_data)
    await update_task(completed_task["id"], {"recurrence_next_at": next_deadline}, source="recurring")
    return new_task_id


async def delete_task(task_id: str, source: str = "manual") -> bool:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT title FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        title = row["title"] if row else None
        cur = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        if cur.rowcount > 0:
            await _log_history(db, task_id, "delete", field=None,
                                old_value=title, source=source)
        await db.commit()
        return cur.rowcount > 0


# ─────────────── BULK DELETE (voice/text "barchasini o'chir" — always confirmed in handler) ───────────────

async def delete_all_tasks(status_in: Optional[list[str]] = None) -> int:
    """Delete tasks in bulk. Optional status filter (e.g. ['done']); None = ALL.
    Returns rows deleted. Caller MUST gate this behind a confirmation."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        if status_in:
            ph = ",".join("?" * len(status_in))
            cur = await db.execute(f"DELETE FROM tasks WHERE status IN ({ph})", tuple(status_in))
        else:
            cur = await db.execute("DELETE FROM tasks")
        await db.commit()
        return cur.rowcount


async def delete_all_meetings() -> int:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM meetings")
        await db.commit()
        return cur.rowcount


async def delete_all_notes() -> int:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM notes")
        await db.commit()
        return cur.rowcount


async def delete_all_reminders() -> int:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM reminders")
        await db.commit()
        return cur.rowcount


async def delete_all_contacts() -> int:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM contacts")
        await db.commit()
        return cur.rowcount


async def count_table(table: str) -> int:
    """Row count for a known table — used to preview bulk-delete impact."""
    if table not in {"tasks", "meetings", "notes", "reminders", "contacts"}:
        return 0
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_task_history(task_id: str, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM task_history WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]




async def get_task(task_id: str) -> Optional[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        row = await cur.fetchone()
        return _row_to_task(row) if row else None


async def list_tasks(status_in: Optional[list[str]] = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_in:
            placeholders = ",".join("?" * len(status_in))
            cur = await db.execute(
                f"""SELECT * FROM tasks
                    WHERE status IN ({placeholders})
                    ORDER BY
                      CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                      CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                      deadline ASC
                    LIMIT ?""",
                (*status_in, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_today_tasks() -> list[dict]:
    """STRICT today filter: only tasks whose deadline falls within today's date
    range. Excludes overdue (yesterday and earlier), future, and undated tasks.

    Design choice: undated tasks (created today without an explicit deadline)
    appear in /tasks (Aktiv) but NOT in /today — /today is reserved for items
    that the principal explicitly committed to TODAY."""
    today = datetime.now(TZ).date()
    start_of_day = TZ.localize(datetime.combine(today, datetime.min.time())).isoformat()
    end_of_day = TZ.localize(datetime.combine(today, datetime.max.time())).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress','blocked')
                 AND deadline IS NOT NULL
                 AND deadline >= ?
                 AND deadline <= ?
               ORDER BY
                 CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                 deadline ASC""",
            (start_of_day, end_of_day),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_overdue_tasks() -> list[dict]:
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress','blocked')
                 AND deadline IS NOT NULL
                 AND deadline < ?
               ORDER BY deadline ASC""",
            (now,),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_tasks_done_today() -> list[dict]:
    today_start = TZ.localize(datetime.combine(datetime.now(TZ).date(), datetime.min.time())).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status = 'done' AND updated_at >= ?
               ORDER BY updated_at DESC""",
            (today_start,),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def list_due_in_window(start_iso: str, end_iso: str) -> list[dict]:
    """Returns P0/P1 tasks due within [start_iso, end_iso] that have NOT been
    reminded yet. Once `mark_task_reminded` writes reminded_at, the task is
    permanently excluded — until `update_task` resets reminded_at (which it
    does automatically when the deadline changes).

    Avvalgi mantiqda `reminded_at < start_iso` shart ishlatilgan, lekin
    sweep har 5 daqiqada window'ni oldinga suradi → reminded_at darrov
    start_iso'dan oldin qoladi va bir xil eslatma takror yuborilaverardi.
    """
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress')
                 AND deadline IS NOT NULL
                 AND deadline >= ? AND deadline <= ?
                 AND reminded_at IS NULL
                 AND priority IN ('P0','P1')
               ORDER BY deadline ASC""",
            (start_iso, end_iso),
        )
        rows = await cur.fetchall()
        return [_row_to_task(r) for r in rows]


async def risk_score_counts() -> dict:
    """One-shot SELECT returning every count needed by handlers.compute_risk_score.
    Replaces the previous N+1 pattern (6 separate queries opening 6 connections)
    with a single aggregate query — same connection, same plan, ~6× faster on
    a cold cache and avoids racy cross-query inconsistencies."""
    now = datetime.now(TZ)
    h24 = (now + timedelta(hours=24)).isoformat()
    h48 = (now + timedelta(hours=48)).isoformat()
    now_iso_val = now.isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT
                  SUM(CASE WHEN deadline IS NOT NULL AND deadline < ? THEN 1 ELSE 0 END) AS overdue,
                  SUM(CASE WHEN deadline IS NOT NULL AND deadline >= ? AND deadline <= ? THEN 1 ELSE 0 END) AS due_24h,
                  SUM(CASE WHEN deadline IS NOT NULL AND deadline >= ? AND deadline <= ? THEN 1 ELSE 0 END) AS due_48h,
                  SUM(CASE WHEN deadline IS NULL THEN 1 ELSE 0 END) AS no_deadline,
                  SUM(CASE WHEN (assignee IS NULL OR TRIM(assignee) = '' OR LOWER(assignee) = 'belgilanmagan') THEN 1 ELSE 0 END) AS unassigned,
                  SUM(CASE WHEN priority = 'P0' THEN 1 ELSE 0 END) AS urgent_open,
                  SUM(CASE WHEN (assignee IS NULL OR TRIM(assignee) = '' OR LOWER(assignee) = 'belgilanmagan')
                           AND deadline IS NOT NULL AND deadline <= ? THEN 1 ELSE 0 END) AS unassigned_due_48h
               FROM tasks
               WHERE status IN ('todo', 'in_progress')""",
            (now_iso_val, now_iso_val, h24, now_iso_val, h48, h48),
        )
        row = await cur.fetchone()
    if not row:
        return {k: 0 for k in (
            "overdue", "due_24h", "due_48h", "no_deadline", "unassigned",
            "urgent_open", "unassigned_due_48h",
        )}
    return {k: int(row[k] or 0) for k in row.keys()}


async def list_unassigned_tasks(limit: int = 50) -> list[dict]:
    """Tasks with no assignee (or assigned to 'belgilanmagan')."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress')
                 AND (assignee IS NULL OR TRIM(assignee) = '' OR LOWER(assignee) = 'belgilanmagan')
               ORDER BY
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC
               LIMIT ?""",
            (limit,),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def list_tasks_without_deadline(limit: int = 50) -> list[dict]:
    """Active tasks with no deadline set."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline IS NULL
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def list_tasks_due_within(hours: int) -> list[dict]:
    """Active tasks whose deadline is within the next N hours from now (not overdue)."""
    now = datetime.now(TZ)
    later = (now + timedelta(hours=hours)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress')
                 AND deadline IS NOT NULL
                 AND deadline >= ? AND deadline <= ?
               ORDER BY deadline ASC""",
            (now.isoformat(), later),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def assignee_load_map() -> dict[str, dict]:
    """Per-assignee statistics: active count, urgent/important counts, overdue.

    Key: assignee name (string). Includes 'belgilanmagan' bucket for unassigned tasks.
    """
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT id, title, priority, status, assignee, deadline
               FROM tasks WHERE status IN ('todo','in_progress')""",
        )
        rows = await cur.fetchall()

    now_iso_str = now_iso()
    out: dict[str, dict] = {}
    for r in rows:
        key = (r["assignee"] or "").strip() or "belgilanmagan"
        if key.lower() in ("men", "oʻzim", "ozim", "o'zim"):
            key = "Men"
        d = out.setdefault(key, {
            "name": key, "active": 0, "urgent": 0, "important": 0,
            "overdue": 0, "next_deadline": None,
        })
        d["active"] += 1
        p = r["priority"]
        if p == "P0":
            d["urgent"] += 1
        elif p == "P1":
            d["important"] += 1
        if r["deadline"] and r["deadline"] < now_iso_str:
            d["overdue"] += 1
        # Track earliest upcoming deadline
        if r["deadline"] and r["deadline"] >= now_iso_str:
            if d["next_deadline"] is None or r["deadline"] < d["next_deadline"]:
                d["next_deadline"] = r["deadline"]
    return out


async def assignee_profile(name: str) -> dict:
    """Detailed profile for a single assignee.

    Returns: active/done/overdue/urgent/important counts, completion_rate,
    avg_closing_time (hours), tasks (top 10 by priority+deadline).
    """
    name_lc = name.strip().lower()
    if name_lc in ("men", "oʻzim", "ozim", "o'zim"):
        name_lc = "men"
        match_clause = "LOWER(TRIM(assignee)) IN ('men', 'oʻzim', 'ozim', \"o'zim\")"
    elif name_lc == "belgilanmagan":
        match_clause = "(assignee IS NULL OR TRIM(assignee) = '' OR LOWER(assignee) = 'belgilanmagan')"
    else:
        match_clause = "LOWER(TRIM(assignee)) = ?"

    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        params = () if "?" not in match_clause else (name_lc,)
        cur = await db.execute(
            f"""SELECT * FROM tasks WHERE {match_clause}""",
            params,
        )
        rows = await cur.fetchall()

    active = []
    completed = []
    overdue = 0
    urgent = 0
    important = 0
    durations_hours = []
    now_iso_str = now_iso()
    for r in rows:
        rd = _row_to_task(r)
        if rd["status"] == "done":
            completed.append(rd)
            try:
                c = datetime.fromisoformat(rd["created_at"])
                u = datetime.fromisoformat(rd["updated_at"])
                durations_hours.append((u - c).total_seconds() / 3600.0)
            except (ValueError, TypeError):
                pass
        elif rd["status"] in ("todo", "in_progress"):
            active.append(rd)
            if rd["priority"] == "P0":
                urgent += 1
            elif rd["priority"] == "P1":
                important += 1
            if rd.get("deadline") and rd["deadline"] < now_iso_str:
                overdue += 1

    total = len(active) + len(completed)
    completion_rate = round((len(completed) / total) * 100, 1) if total else 0.0
    avg_hours = round(sum(durations_hours) / len(durations_hours), 1) if durations_hours else 0.0
    # Sort active tasks by priority then deadline
    active.sort(key=lambda t: (
        {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(t.get("priority", "P2"), 2),
        t.get("deadline") or "9999",
    ))
    return {
        "name": name,
        "active": len(active),
        "completed": len(completed),
        "overdue": overdue,
        "urgent": urgent,
        "important": important,
        "completion_rate": completion_rate,
        "avg_closing_hours": avg_hours,
        "tasks": active[:10],
    }


async def list_delegated_open_tasks(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress')
                 AND assignee IS NOT NULL
                 AND LOWER(TRIM(assignee)) NOT IN (
                     '', 'men', 'siz', 'belgilanmagan', '—',
                     'oʻzim', 'o''zim', 'o''z', 'ozim'
                 )
               ORDER BY
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC
               LIMIT ?""",
            (limit,),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def list_recurring_tasks(limit: int = 30) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE recurrence_rule IS NOT NULL
                 AND status IN ('todo','in_progress')
               ORDER BY
                 CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                 deadline ASC
               LIMIT ?""",
            (limit,),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def mark_task_reminded(task_id: str) -> bool:
    """Conditional claim: returns True only if THIS caller flipped reminded_at
    from NULL. Lets the scheduler 'claim then send' so two overlapping sweep
    instances cannot both deliver the same reminder."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE tasks SET reminded_at = ? WHERE id = ? AND reminded_at IS NULL",
            (now_iso(), task_id),
        )
        await db.commit()
        return cur.rowcount > 0


def _row_to_task(row) -> dict:
    d = dict(row)
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except json.JSONDecodeError:
            logger.warning("Corrupted JSON in tasks.tags for id=%s — defaulting to []", d.get("id"))
            d["tags"] = []
    else:
        d["tags"] = []
    return d


# ─────────────────────────────────────────── REMINDERS ───────────────────────────────────────────

async def create_reminder(data: dict) -> str:
    reminder_id = new_id("r-")
    now = now_iso()
    recurrence_rule = normalize_recurrence_rule(data.get("recurrence_rule") or data.get("recurrence"))
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO reminders (id, title, note, remind_at, status, recurrence_rule,
                                      task_id, meeting_id, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reminder_id,
                data.get("title", "Eslatma"),
                data.get("note"),
                data.get("remind_at"),
                data.get("status", "scheduled"),
                recurrence_rule,
                data.get("task_id"),
                data.get("meeting_id"),
                data.get("source", "manual"),
                now,
                now,
            ),
        )
        await db.commit()
    return reminder_id


async def get_reminder(reminder_id: str) -> Optional[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        row = await cur.fetchone()
        return _row_to_reminder(row) if row else None


async def update_reminder(reminder_id: str, data: dict) -> bool:
    if not data:
        return False
    allowed = {
        "title", "note", "remind_at", "status", "recurrence_rule", "task_id",
        "meeting_id", "source", "snooze_count", "sent_at",
    }
    fields = []
    values: list[Any] = []
    for key, value in data.items():
        if key not in allowed:
            continue
        if key == "recurrence_rule":
            value = normalize_recurrence_rule(value)
        fields.append(f"{key} = ?")
        values.append(value)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(now_iso())
    values.append(reminder_id)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()
        return cur.rowcount > 0


async def cancel_reminder(reminder_id: str) -> bool:
    return await update_reminder(reminder_id, {"status": "cancelled"})


async def complete_reminder(reminder_id: str) -> bool:
    return await update_reminder(reminder_id, {"status": "done"})


async def snooze_reminder(reminder_id: str, remind_at_iso: str) -> bool:
    reminder = await get_reminder(reminder_id)
    if not reminder:
        return False
    return await update_reminder(reminder_id, {
        "remind_at": remind_at_iso,
        "status": "scheduled",
        "sent_at": None,
        "snooze_count": int(reminder.get("snooze_count") or 0) + 1,
    })


async def list_reminders(status_in: Optional[list[str]] = None, limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status_in:
            placeholders = ",".join("?" * len(status_in))
            cur = await db.execute(
                f"""SELECT * FROM reminders
                    WHERE status IN ({placeholders})
                    ORDER BY
                      CASE WHEN remind_at < ? AND status = 'scheduled' THEN 0 ELSE 1 END,
                      remind_at ASC
                    LIMIT ?""",
                (*status_in, now_iso(), limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM reminders WHERE status != 'cancelled' ORDER BY remind_at DESC LIMIT ?",
                (limit,),
            )
        return [_row_to_reminder(r) for r in await cur.fetchall()]


async def list_today_reminders(limit: int = 50) -> list[dict]:
    today = datetime.now(TZ).date()
    start = TZ.localize(datetime.combine(today, datetime.min.time())).isoformat()
    end = TZ.localize(datetime.combine(today, datetime.max.time())).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM reminders
               WHERE status = 'scheduled'
                 AND remind_at BETWEEN ? AND ?
               ORDER BY remind_at ASC
               LIMIT ?""",
            (start, end, limit),
        )
        return [_row_to_reminder(r) for r in await cur.fetchall()]


async def list_due_reminders(now_iso_value: Optional[str] = None, limit: int = 20) -> list[dict]:
    current = now_iso_value or now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM reminders
               WHERE status = 'scheduled'
                 AND remind_at <= ?
               ORDER BY remind_at ASC
               LIMIT ?""",
            (current, limit),
        )
        return [_row_to_reminder(r) for r in await cur.fetchall()]


async def search_reminders(query: str, limit: int = 30) -> list[dict]:
    q = f"%{query.strip().lower()}%"
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM reminders
               WHERE status != 'cancelled'
                 AND (LOWER(title) LIKE ? OR LOWER(COALESCE(note,'')) LIKE ?)
               ORDER BY remind_at DESC LIMIT ?""",
            (q, q, limit),
        )
        return [_row_to_reminder(r) for r in await cur.fetchall()]


async def reminders_overview() -> dict:
    now = datetime.now(TZ)
    today_start = TZ.localize(datetime.combine(now.date(), datetime.min.time())).isoformat()
    today_end = TZ.localize(datetime.combine(now.date(), datetime.max.time())).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        async def count(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return int(row[0] or 0)

        scheduled = await count("SELECT COUNT(*) FROM reminders WHERE status = 'scheduled'")
        overdue = await count(
            "SELECT COUNT(*) FROM reminders WHERE status = 'scheduled' AND remind_at < ?",
            (now.isoformat(),),
        )
        today = await count(
            """SELECT COUNT(*) FROM reminders
               WHERE status = 'scheduled' AND remind_at BETWEEN ? AND ?""",
            (today_start, today_end),
        )
        sent = await count("SELECT COUNT(*) FROM reminders WHERE status = 'sent'")
        recurring = await count(
            "SELECT COUNT(*) FROM reminders WHERE status = 'scheduled' AND recurrence_rule IS NOT NULL"
        )
    return {
        "scheduled": scheduled,
        "overdue": overdue,
        "today": today,
        "sent": sent,
        "recurring": recurring,
    }


async def mark_reminder_sent(reminder_id: str) -> bool:
    """Atomically mark a reminder as sent, or roll it forward if it has a
    recurrence_rule. Single transaction: the conditional UPDATE on
    status='scheduled' ensures only one concurrent caller succeeds, so the
    scheduler cannot fire the same reminder twice during overlapping sweeps."""
    sent_at = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        reminder = await cur.fetchone()
        if reminder is None:
            return False
        if reminder["status"] != "scheduled":
            return False  # already sent / cancelled by another caller
        rule = normalize_recurrence_rule(reminder["recurrence_rule"])
        next_at = compute_next_recurrence(reminder["remind_at"], rule) if rule else None
        if next_at:
            cur = await db.execute(
                "UPDATE reminders SET remind_at = ?, status = 'scheduled', "
                "sent_at = ?, updated_at = ? WHERE id = ? AND status = 'scheduled'",
                (next_at, sent_at, sent_at, reminder_id),
            )
        else:
            cur = await db.execute(
                "UPDATE reminders SET status = 'sent', sent_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'scheduled'",
                (sent_at, sent_at, reminder_id),
            )
        won = cur.rowcount > 0
        await db.commit()
        return won


def _row_to_reminder(row) -> dict:
    return dict(row)


# ─────────────────────────────────────────── NOTES (Qaydlar) ───────────────────────────────────────────

_NOTES_ALLOWED_STATUSES = ("inbox", "processed", "archived")
_NOTES_ALLOWED_SOURCES = ("forward", "command", "voice", "manual", "llm")


def _derive_title(content: str, max_len: int = 60) -> str:
    """First non-empty line of content, trimmed to max_len. Used when the
    caller doesn't supply an explicit title."""
    first_line = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
    if not first_line:
        return "Qayd"
    if len(first_line) <= max_len:
        return first_line
    return first_line[: max_len - 1] + "…"


def _row_to_note(row) -> dict:
    d = dict(row)
    if d.get("tags"):
        try:
            d["tags"] = json.loads(d["tags"])
        except json.JSONDecodeError:
            logger.warning("Corrupted JSON in notes.tags for id=%s — defaulting to []", d.get("id"))
            d["tags"] = []
    else:
        d["tags"] = []
    return d


async def create_note(data: dict) -> str:
    """Insert a new note row. Required: content. Everything else is optional
    with sensible defaults. Returns the new note id."""
    content = (data.get("content") or "").strip()
    if not content:
        return ""
    note_id = new_id("n-")
    now = now_iso()
    source = data.get("source", "manual")
    if source not in _NOTES_ALLOWED_SOURCES:
        source = "manual"
    title = (data.get("title") or "").strip() or _derive_title(content)
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    content_html = data.get("content_html")  # optional rich HTML for forwards
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO notes (id, content, content_html, title, source,
                                  source_chat, source_author, source_message_id,
                                  tags, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'inbox', ?, ?)""",
            (
                note_id, content, content_html, title, source,
                data.get("source_chat"), data.get("source_author"),
                data.get("source_message_id"),
                json.dumps(tags, ensure_ascii=False),
                now, now,
            ),
        )
        await db.commit()
    return note_id


async def get_note(note_id: str) -> Optional[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM notes WHERE id = ?", (note_id,))
        row = await cur.fetchone()
        return _row_to_note(row) if row else None


async def list_notes(status: Optional[str] = "inbox", limit: int = 200) -> list[dict]:
    """Return notes filtered by status (default 'inbox'), newest first.
    Pass status=None to fetch all (used by search/global views)."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cur = await db.execute(
                """SELECT * FROM notes WHERE status = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (status, limit),
            )
        else:
            cur = await db.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        return [_row_to_note(r) for r in await cur.fetchall()]


async def count_notes_in_status(status: str) -> int:
    """Cheap count for the daily briefing's inbox-pending line."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(*) FROM notes WHERE status = ?", (status,),
        )
        row = await cur.fetchone()
        return int(row[0] or 0)


async def search_notes(query: str, limit: int = 30) -> list[dict]:
    """Case-insensitive LIKE search across title + content. Excludes
    archived notes from the default surface."""
    q = f"%{query.strip().lower()}%"
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM notes
               WHERE status != 'archived'
                 AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (q, q, limit),
        )
        return [_row_to_note(r) for r in await cur.fetchall()]


async def update_note(note_id: str, data: dict) -> bool:
    """Patch a subset of note fields. Mainly used by conversion flows."""
    allowed = {"content", "title", "tags", "status", "converted_to_id",
               "converted_to_type"}
    fields, values = [], []
    for key, value in data.items():
        if key not in allowed:
            continue
        if key == "tags":
            if not isinstance(value, list):
                value = [str(value)]
            value = json.dumps(value, ensure_ascii=False)
        if key == "status" and value not in _NOTES_ALLOWED_STATUSES:
            continue
        fields.append(f"{key} = ?")
        values.append(value)
    if not fields:
        return False
    fields.append("updated_at = ?")
    values.append(now_iso())
    values.append(note_id)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            f"UPDATE notes SET {', '.join(fields)} WHERE id = ?",
            values,
        )
        await db.commit()
        return cur.rowcount > 0


async def archive_note(note_id: str) -> bool:
    return await update_note(note_id, {"status": "archived"})


async def delete_note(note_id: str) -> bool:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        await db.commit()
        return cur.rowcount > 0


async def mark_note_processed(note_id: str, converted_to_type: str,
                                converted_to_id: str) -> bool:
    """Flip status='processed' and record the link to the produced task/reminder."""
    if converted_to_type not in ("task", "reminder"):
        return False
    return await update_note(note_id, {
        "status": "processed",
        "converted_to_id": converted_to_id,
        "converted_to_type": converted_to_type,
    })


# ─────────────────────────────────────────── MEETINGS ───────────────────────────────────────────

def _agenda_to_text(value) -> Optional[str]:
    """`agenda` is a plain-TEXT column (read back as a string everywhere). Claude
    sends it as a list of bullet points, which SQLite can't bind directly
    ("type 'list' is not supported"). Normalize a list (or any value) to a
    string so the INSERT/UPDATE never fails on it."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
        return "\n".join(f"• {x}" for x in items) if items else None
    return str(value)


async def create_meeting(data: dict) -> str:
    meeting_id = new_id("m-")
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO meetings (id, title, datetime_start, datetime_end, participants,
                                     location_or_link, agenda, prep_notes, follow_up_actions, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                meeting_id,
                data.get("title", "Uchrashuv"),
                data.get("datetime_start"),
                data.get("datetime_end"),
                json.dumps(data.get("participants", []), ensure_ascii=False),
                data.get("location_or_link"),
                _agenda_to_text(data.get("agenda")),
                data.get("prep_notes"),
                json.dumps(data.get("follow_up_actions", []), ensure_ascii=False),
                now_iso(),
            ),
        )
        await db.commit()
    return meeting_id


async def update_meeting(meeting_id: str, data: dict) -> bool:
    if not data:
        return False
    allowed = {
        "title", "datetime_start", "datetime_end", "participants", "location_or_link",
        "agenda", "prep_notes", "follow_up_actions", "reminded_at", "prep_sent_at",
        "followup_sent_at", "icloud_uid",
    }
    fields = []
    values: list[Any] = []
    for key, value in data.items():
        if key not in allowed:
            continue
        fields.append(f"{key} = ?")
        if key in ("participants", "follow_up_actions") and isinstance(value, list):
            value = json.dumps(value, ensure_ascii=False)
        elif key == "agenda":
            value = _agenda_to_text(value)
        values.append(value)
    if not fields:
        return False
    values.append(meeting_id)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(f"UPDATE meetings SET {', '.join(fields)} WHERE id = ?", values)
        await db.commit()
        return cur.rowcount > 0


async def cancel_meeting(meeting_id: str) -> bool:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        await db.commit()
        return cur.rowcount > 0


async def complete_meeting(meeting_id: str) -> bool:
    """Mark a meeting as attended/done. It then drops out of the active
    (Bugun/Haftalik/…) views and shows with a ✅ in O'tgan."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE meetings SET completed_at = ? WHERE id = ?",
            (now_iso(), meeting_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def uncomplete_meeting(meeting_id: str) -> bool:
    """Undo a 'done' mark — returns the meeting to the active views."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE meetings SET completed_at = NULL WHERE id = ?",
            (meeting_id,),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_today_meetings() -> list[dict]:
    today = datetime.now(TZ).date()
    start = TZ.localize(datetime.combine(today, datetime.min.time())).isoformat()
    end = TZ.localize(datetime.combine(today, datetime.max.time())).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
               ORDER BY datetime_start ASC""",
            (start, end),
        )
        rows = await cur.fetchall()
        return [_row_to_meeting(r) for r in rows]


async def list_upcoming_meetings(within_minutes: int = 15) -> list[dict]:
    now = datetime.now(TZ)
    soon = now + timedelta(minutes=within_minutes)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
                 AND (reminded_at IS NULL OR reminded_at < ?)
               ORDER BY datetime_start ASC""",
            (now.isoformat(), soon.isoformat(), now.isoformat()),
        )
        rows = await cur.fetchall()
        return [_row_to_meeting(r) for r in rows]


async def mark_meeting_reminded(meeting_id: str) -> bool:
    """Conditional claim — see mark_task_reminded."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE meetings SET reminded_at = ? WHERE id = ? AND reminded_at IS NULL",
            (now_iso(), meeting_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def mark_meeting_prep_sent(meeting_id: str) -> bool:
    """Conditional claim — see mark_task_reminded."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE meetings SET prep_sent_at = ? WHERE id = ? AND prep_sent_at IS NULL",
            (now_iso(), meeting_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def mark_meeting_followup_sent(meeting_id: str) -> bool:
    """Conditional claim — see mark_task_reminded."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "UPDATE meetings SET followup_sent_at = ? WHERE id = ? AND followup_sent_at IS NULL",
            (now_iso(), meeting_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def list_meetings_needing_prep(window_minutes: int = 60) -> list[dict]:
    now = datetime.now(TZ)
    soon = now + timedelta(minutes=window_minutes)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
                 AND prep_sent_at IS NULL
               ORDER BY datetime_start ASC""",
            (now.isoformat(), soon.isoformat()),
        )
        return [_row_to_meeting(r) for r in await cur.fetchall()]


async def list_meetings_needing_followup(min_age_minutes: int = 30, max_age_hours: int = 24) -> list[dict]:
    now = datetime.now(TZ)
    earliest = now - timedelta(hours=max_age_hours)
    latest = now - timedelta(minutes=min_age_minutes)
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE COALESCE(datetime_end, datetime_start) BETWEEN ? AND ?
                 AND followup_sent_at IS NULL
               ORDER BY datetime_start ASC""",
            (earliest.isoformat(), latest.isoformat()),
        )
        return [_row_to_meeting(r) for r in await cur.fetchall()]


async def get_meeting(meeting_id: str) -> Optional[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,))
        row = await cur.fetchone()
        return _row_to_meeting(row) if row else None


async def list_unreminded_future_meetings() -> list[dict]:
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start > ?
                 AND reminded_at IS NULL
               ORDER BY datetime_start ASC""",
            (now,),
        )
        rows = await cur.fetchall()
        return [_row_to_meeting(r) for r in rows]


async def next_first_meeting() -> Optional[dict]:
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings WHERE datetime_start >= ?
               ORDER BY datetime_start ASC LIMIT 1""",
            (now,),
        )
        row = await cur.fetchone()
        return _row_to_meeting(row) if row else None


def _row_to_meeting(row) -> dict:
    d = dict(row)
    for key in ("participants", "follow_up_actions"):
        if d.get(key):
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                logger.warning("Corrupted JSON in meetings.%s for id=%s — defaulting to []",
                                key, d.get("id"))
                d[key] = []
        else:
            d[key] = []
    return d


async def list_meetings_in_window(start_iso: str, end_iso: str) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
               ORDER BY datetime_start ASC""",
            (start_iso, end_iso),
        )
        rows = await cur.fetchall()
        return [_row_to_meeting(r) for r in rows]


async def search_all(query: str, limit: int = 30) -> dict:
    """Full-text-ish search across tasks, reminders, meetings, contacts. Uses LIKE for simplicity."""
    q = f"%{query.strip().lower()}%"
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE LOWER(title) LIKE ?
                  OR LOWER(COALESCE(description,'')) LIKE ?
                  OR LOWER(COALESCE(tags,'')) LIKE ?
                  OR LOWER(COALESCE(assignee,'')) LIKE ?
               ORDER BY created_at DESC LIMIT ?""",
            (q, q, q, q, limit),
        )
        tasks = [_row_to_task(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE LOWER(title) LIKE ? OR LOWER(COALESCE(agenda,'')) LIKE ?
                  OR LOWER(COALESCE(participants,'')) LIKE ? OR LOWER(COALESCE(location_or_link,'')) LIKE ?
               ORDER BY datetime_start DESC LIMIT ?""",
            (q, q, q, q, limit),
        )
        meetings = [_row_to_meeting(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM contacts
               WHERE LOWER(name) LIKE ? OR LOWER(COALESCE(role,'')) LIKE ?
               ORDER BY last_interaction DESC LIMIT ?""",
            (q, q, limit),
        )
        contacts = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM reminders
               WHERE status != 'cancelled'
                 AND (LOWER(title) LIKE ? OR LOWER(COALESCE(note,'')) LIKE ?)
               ORDER BY remind_at DESC LIMIT ?""",
            (q, q, limit),
        )
        reminders = [_row_to_reminder(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM notes
               WHERE status != 'archived'
                 AND (LOWER(title) LIKE ? OR LOWER(content) LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (q, q, limit),
        )
        notes = [_row_to_note(r) for r in await cur.fetchall()]

    return {"tasks": tasks, "meetings": meetings, "contacts": contacts,
            "reminders": reminders, "notes": notes,
            "total": len(tasks) + len(meetings) + len(contacts) + len(reminders) + len(notes)}


async def list_meetings_in_month(year: int, month: int) -> list[dict]:
    """All meetings whose start falls inside the given calendar month."""
    from calendar import monthrange
    start_iso = TZ.localize(datetime(year, month, 1)).isoformat()
    last_day = monthrange(year, month)[1]
    end_iso = TZ.localize(datetime(year, month, last_day, 23, 59, 59)).isoformat()
    return await list_meetings_in_window(start_iso, end_iso)


async def list_tasks_in_month(year: int, month: int) -> list[dict]:
    """Tasks with deadlines in the given calendar month."""
    from calendar import monthrange
    start_iso = TZ.localize(datetime(year, month, 1)).isoformat()
    last_day = monthrange(year, month)[1]
    end_iso = TZ.localize(datetime(year, month, last_day, 23, 59, 59)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE deadline IS NOT NULL AND deadline >= ? AND deadline <= ?
               ORDER BY deadline ASC""",
            (start_iso, end_iso),
        )
        return [_row_to_task(r) for r in await cur.fetchall()]


async def weekly_stats(week_start_iso: str) -> dict:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE created_at >= ?",
            (week_start_iso,),
        )
        created = (await cur.fetchone())["n"]

        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status = 'done' AND updated_at >= ?",
            (week_start_iso,),
        )
        done = (await cur.fetchone())["n"]

        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM meetings WHERE datetime_start >= ?",
            (week_start_iso,),
        )
        meetings = (await cur.fetchone())["n"]

        cur = await db.execute(
            "SELECT priority, COUNT(*) AS n FROM tasks WHERE status IN ('todo','in_progress') GROUP BY priority"
        )
        priority_rows = await cur.fetchall()
        by_priority = {r["priority"]: r["n"] for r in priority_rows}

    completion_rate = round(done / created * 100, 1) if created > 0 else 0.0

    return {
        "tasks_created": created,
        "tasks_done": done,
        "meetings": meetings,
        "completion_rate_pct": completion_rate,
        "by_priority": by_priority,
    }


async def executive_stats(days: int = 7) -> dict:
    """Return decision-grade operational metrics for /stats and /report."""
    now = datetime.now(TZ)
    start = now - timedelta(days=days)
    next_24 = now + timedelta(hours=24)
    next_48 = now + timedelta(hours=48)
    next_7 = now + timedelta(days=7)

    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        async def count(sql: str, params: tuple = ()) -> int:
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return int(row[0] or 0)

        tasks_created = await count("SELECT COUNT(*) FROM tasks WHERE created_at >= ?", (start.isoformat(),))
        tasks_done = await count("SELECT COUNT(*) FROM tasks WHERE status = 'done' AND updated_at >= ?", (start.isoformat(),))
        active_count = await count("SELECT COUNT(*) FROM tasks WHERE status IN ('todo','in_progress')")
        overdue_count = await count(
            """SELECT COUNT(*) FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline IS NOT NULL AND deadline < ?""",
            (now.isoformat(),),
        )
        no_deadline_count = await count(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('todo','in_progress') AND deadline IS NULL"
        )
        due_24_count = await count(
            """SELECT COUNT(*) FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline BETWEEN ? AND ?""",
            (now.isoformat(), next_24.isoformat()),
        )
        due_48_count = await count(
            """SELECT COUNT(*) FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline BETWEEN ? AND ?""",
            (now.isoformat(), next_48.isoformat()),
        )
        due_7_count = await count(
            """SELECT COUNT(*) FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline BETWEEN ? AND ?""",
            (now.isoformat(), next_7.isoformat()),
        )
        recurring_count = await count(
            "SELECT COUNT(*) FROM tasks WHERE status IN ('todo','in_progress') AND recurrence_rule IS NOT NULL"
        )

        cur = await db.execute(
            """SELECT priority, COUNT(*) AS n FROM tasks
               WHERE status IN ('todo','in_progress') GROUP BY priority"""
        )
        by_priority = {r["priority"]: r["n"] for r in await cur.fetchall()}

        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline IS NOT NULL AND deadline < ?
               ORDER BY deadline ASC LIMIT 5""",
            (now.isoformat(),),
        )
        overdue_tasks = [_row_to_task(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline IS NULL
               ORDER BY
                 CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                 created_at ASC LIMIT 5"""
        )
        no_deadline_tasks = [_row_to_task(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status IN ('todo','in_progress') AND deadline BETWEEN ? AND ?
               ORDER BY deadline ASC LIMIT 8""",
            (now.isoformat(), next_48.isoformat()),
        )
        risk_tasks = [_row_to_task(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT assignee, COUNT(*) AS total,
                      SUM(CASE WHEN deadline IS NOT NULL AND deadline < ? THEN 1 ELSE 0 END) AS overdue
               FROM tasks
               WHERE status IN ('todo','in_progress')
                 AND assignee IS NOT NULL
                 AND LOWER(TRIM(assignee)) NOT IN (
                     '', 'men', 'siz', 'belgilanmagan', '—',
                     'oʻzim', 'o''zim', 'o''z', 'ozim'
                 )
               GROUP BY assignee ORDER BY total DESC LIMIT 8""",
            (now.isoformat(),),
        )
        delegation = [dict(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM tasks
               WHERE status = 'done' AND updated_at >= ?
               ORDER BY updated_at DESC LIMIT 200""",
            (start.isoformat(),),
        )
        done_rows = [_row_to_task(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
               ORDER BY datetime_start ASC""",
            (start.isoformat(), now.isoformat()),
        )
        period_meetings = [_row_to_meeting(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT * FROM meetings
               WHERE datetime_start BETWEEN ? AND ?
               ORDER BY datetime_start ASC""",
            (now.isoformat(), next_7.isoformat()),
        )
        upcoming_meetings = [_row_to_meeting(r) for r in await cur.fetchall()]

        cur = await db.execute(
            """SELECT DATE(created_at) AS day, COUNT(*) AS created
               FROM tasks WHERE created_at >= ?
               GROUP BY DATE(created_at)""",
            ((now - timedelta(days=6)).isoformat(),),
        )
        created_by_day = {r["day"]: r["created"] for r in await cur.fetchall()}

        cur = await db.execute(
            """SELECT DATE(updated_at) AS day, COUNT(*) AS done
               FROM tasks WHERE status = 'done' AND updated_at >= ?
               GROUP BY DATE(updated_at)""",
            ((now - timedelta(days=6)).isoformat(),),
        )
        done_by_day = {r["day"]: r["done"] for r in await cur.fetchall()}

        cur = await db.execute(
            """SELECT provider, COUNT(*) AS calls, COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM llm_audit_log WHERE ts >= ?
               GROUP BY provider ORDER BY calls DESC""",
            (start.isoformat(),),
        )
        llm_by_provider = [dict(r) for r in await cur.fetchall()]

    completion_rate = round((tasks_done / tasks_created) * 100, 1) if tasks_created else 0.0

    durations = []
    for task in done_rows:
        created_at = parse_iso_dt(task.get("created_at"))
        updated_at = parse_iso_dt(task.get("updated_at"))
        if created_at and updated_at and updated_at >= created_at:
            durations.append((updated_at - created_at).total_seconds() / 3600)
    avg_completion_hours = round(sum(durations) / len(durations), 1) if durations else 0.0

    def meeting_hours(meetings: list[dict]) -> float:
        total = 0.0
        for meeting in meetings:
            start_dt = parse_iso_dt(meeting.get("datetime_start"))
            end_dt = parse_iso_dt(meeting.get("datetime_end"))
            if start_dt and end_dt and end_dt > start_dt:
                total += (end_dt - start_dt).total_seconds() / 3600
            elif start_dt:
                total += 1.0
        return round(total, 1)

    meeting_action_items = sum(len(m.get("follow_up_actions") or []) for m in period_meetings)
    meetings_with_prep = sum(1 for m in period_meetings if m.get("prep_notes") or m.get("prep_sent_at"))
    meetings_with_followup = sum(1 for m in period_meetings if m.get("follow_up_actions") or m.get("followup_sent_at"))

    trend = []
    for i in range(6, -1, -1):
        d = (now - timedelta(days=i)).date().isoformat()
        trend.append({"day": d, "created": created_by_day.get(d, 0), "done": done_by_day.get(d, 0)})

    risk_score = min(
        100,
        overdue_count * 18
        + by_priority.get("P0", 0) * 12
        + by_priority.get("P1", 0) * 6
        + due_24_count * 8
        + no_deadline_count * 3,
    )

    return {
        "period_days": days,
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        "tasks": {
            "created": tasks_created,
            "done": tasks_done,
            "active": active_count,
            "overdue": overdue_count,
            "no_deadline": no_deadline_count,
            "due_24h": due_24_count,
            "due_48h": due_48_count,
            "due_7d": due_7_count,
            "recurring": recurring_count,
            "completion_rate_pct": completion_rate,
            "avg_completion_hours": avg_completion_hours,
            "by_priority": by_priority,
            "overdue_tasks": overdue_tasks,
            "no_deadline_tasks": no_deadline_tasks,
            "risk_tasks": risk_tasks,
        },
        "delegation": delegation,
        "meetings": {
            "count": len(period_meetings),
            "hours": meeting_hours(period_meetings),
            "prep_count": meetings_with_prep,
            "followup_count": meetings_with_followup,
            "action_items": meeting_action_items,
            "upcoming_7d_count": len(upcoming_meetings),
            "upcoming_7d_hours": meeting_hours(upcoming_meetings),
        },
        "trend": trend,
        "llm": {
            "providers": llm_by_provider,
            "calls": sum(r["calls"] for r in llm_by_provider),
            "cost": round(sum(float(r["cost"] or 0) for r in llm_by_provider), 4),
        },
        "risk_score": risk_score,
    }


# ─────────────────────────────────────────── CONTACTS ───────────────────────────────────────────

async def save_contact(data: dict) -> str:
    """Idempotent upsert by contact name. Uses a single INSERT … ON CONFLICT
    statement so two concurrent callers with the same name cannot both pass
    the existence check and race to INSERT — the old SELECT-then-INSERT
    pattern crashed on the second caller with a UNIQUE constraint violation."""
    name = data.get("name")
    if not name:
        return ""
    candidate_id = new_id("c-")
    role = data.get("role")
    formality = data.get("formality_level")
    preferred = data.get("preferred_channel")
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        # excluded.* are the "would-have-inserted" values from the VALUES clause.
        # COALESCE(excluded.col, contacts.col) preserves existing data when the
        # caller didn't provide a new value (matches original update-only-if-set logic).
        cur = await db.execute(
            """INSERT INTO contacts (id, name, role, formality_level, preferred_channel,
                                     last_interaction, created_at)
               VALUES (?, ?, ?, COALESCE(?, 3), ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   role = COALESCE(excluded.role, contacts.role),
                   formality_level = COALESCE(?, contacts.formality_level),
                   preferred_channel = COALESCE(excluded.preferred_channel, contacts.preferred_channel),
                   last_interaction = excluded.last_interaction
               RETURNING id""",
            (candidate_id, name, role, formality, preferred, now, now, formality),
        )
        row = await cur.fetchone()
        await db.commit()
        return row["id"] if row else ""


async def list_contacts() -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM contacts
               ORDER BY CASE WHEN last_interaction IS NULL THEN 1 ELSE 0 END,
                        last_interaction DESC
               LIMIT 30"""
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────── CORRECTIONS ───────────────────────────────────────────

async def save_correction(data: dict) -> str:
    cid = new_id("corr-")
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO corrections (id, context, correction, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (cid, data.get("context"), data.get("correction"), data.get("reason"), now_iso()),
        )
        await db.commit()
    return cid


async def list_recent_corrections(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM corrections ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


# ─────────────────────────────────────────── CONVERSATION HISTORY ───────────────────────────────────────────

DEFAULT_SETTINGS = {
    "notifications_enabled": True,
    "language": "uz",
    "morning_briefing_time": "08:00",
    "evening_summary_time": "18:00",
    "meeting_reminder_min": 15,
    "task_reminder_hours": 2,
    # Quiet hours — default OFF so existing users see no behavior change
    # until they explicitly enable via /settings.
    "quiet_hours_enabled": False,
    "quiet_hours_start": "22:00",
    "quiet_hours_end": "07:00",
    # Voice: auto-process the transcript without an extra confirm tap.
    # Default ON — power users can re-enable confirmation via /settings.
    "voice_auto_confirm": config.VOICE_AUTO_CONFIRM,
    # Confirm before creating tasks/meetings. Default ON for safety so a
    # mis-transcribed voice message can't quietly create wrong items.
    "confirm_create_actions": config.CONFIRM_CREATE_ACTIONS,
}


async def get_settings() -> dict:
    """Return user settings merged with defaults."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT data FROM principal_profile WHERE key = 'settings'")
        row = await cur.fetchone()
    saved: dict = {}
    if row and row["data"]:
        try:
            saved = json.loads(row["data"])
        except json.JSONDecodeError:
            logger.error(
                "principal_profile.settings JSON is corrupted — falling back to "
                "defaults. User-customised reminder/briefing times have been lost "
                "until /settings is used again."
            )
            saved = {}
    merged = {**DEFAULT_SETTINGS, **saved}
    return merged


async def set_setting(key: str, value) -> None:
    """Update a single setting key."""
    current = await get_settings()
    current[key] = value
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO principal_profile (key, data, updated_at) VALUES ('settings', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
            (json.dumps(current, ensure_ascii=False), now_iso()),
        )
        await db.commit()


async def append_message(role: str, content: str) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO conversation_history (role, content, created_at) VALUES (?, ?, ?)",
            (role, content, now_iso()),
        )
        await db.commit()


async def recent_messages(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT role, content FROM conversation_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = await cur.fetchall()
        return list(reversed([dict(r) for r in rows]))


async def trim_history(keep: int = 200) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM conversation_history")
        (total,) = await cur.fetchone()
        if total > keep:
            await db.execute(
                """DELETE FROM conversation_history
                   WHERE id IN (
                       SELECT id FROM conversation_history
                       ORDER BY id ASC LIMIT ?
                   )""",
                (total - keep,),
            )
            await db.commit()


async def purge_old_conversation_history(retention_days: int) -> int:
    """Time-based retention purge — drops every conversation_history row
    older than retention_days. Complements trim_history's count cap to
    satisfy NBU / banking data retention rules (1–3 year limits depending
    on data class). Returns rows deleted."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(TZ) - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM conversation_history WHERE created_at < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount


async def llm_cost_breakdown(days: int = 7) -> dict:
    """Cost analytics for the past N days, broken down by model and cache
    hit/miss. Used by /diagnostics and the executive dashboard to track
    where Claude spend is going and whether prompt caching is actually
    working as intended."""
    cutoff = (datetime.now(TZ) - timedelta(days=days)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT
                  model,
                  COUNT(*) AS calls,
                  SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                  SUM(COALESCE(output_tokens, 0)) AS output_tokens,
                  SUM(COALESCE(cache_read_tokens, 0)) AS cache_read,
                  SUM(COALESCE(cache_creation_tokens, 0)) AS cache_write,
                  SUM(COALESCE(estimated_cost_usd, 0)) AS cost_usd
               FROM llm_audit_log
               WHERE ts >= ? AND error IS NULL
               GROUP BY model
               ORDER BY cost_usd DESC""",
            (cutoff,),
        )
        rows = [dict(r) for r in await cur.fetchall()]

    totals = {
        "calls": sum(r["calls"] or 0 for r in rows),
        "input_tokens": sum(r["input_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["output_tokens"] or 0 for r in rows),
        "cache_read": sum(r["cache_read"] or 0 for r in rows),
        "cache_write": sum(r["cache_write"] or 0 for r in rows),
        "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in rows), 4),
    }
    # Cache hit rate = cache_read / (input_tokens + cache_read). If it's
    # >50%, prompt caching is paying off significantly.
    denom = totals["input_tokens"] + totals["cache_read"]
    totals["cache_hit_rate"] = round(totals["cache_read"] / denom, 3) if denom else 0.0
    return {"by_model": rows, "totals": totals, "days": days}


async def purge_old_audit_logs(retention_days: int) -> int:
    """Same idea for the LLM audit log table — used by scheduler nightly."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.now(TZ) - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM llm_audit_log WHERE ts < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount


# ─────────────────────────────────────────── PLANS ───────────────────────────────────────────

async def save_plan(input_text: str, output_text: str, task_ids: Optional[list] = None) -> str:
    """Save an executive planning session for later review and learning."""
    plan_id = new_id("p-")
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO plans (id, created_at, input_text, output_text, task_ids)
               VALUES (?, ?, ?, ?, ?)""",
            (plan_id, now_iso(), input_text, output_text,
             json.dumps(task_ids or [], ensure_ascii=False)),
        )
        await db.commit()
    return plan_id


async def mark_plan_accepted(plan_id: str) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("UPDATE plans SET accepted = 1 WHERE id = ?", (plan_id,))
        await db.commit()


async def list_recent_plans(limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


# ─────────────────────────────────────────── PENDING ACTIONS (idempotency) ───────────────────────────────────────────


async def enqueue_pending_action(update_id: Optional[int], chat_id: Optional[int],
                                  message_id: Optional[int], user_text: str) -> Optional[int]:
    """Record that we're about to process a user message. Returns the row id,
    or None if this update_id was already processed (idempotency win — a
    redelivered Telegram update won't trigger a second Claude call)."""
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        try:
            cur = await db.execute(
                """INSERT INTO pending_actions
                   (update_id, chat_id, message_id, user_text, state, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (update_id, chat_id, message_id, user_text, now, now),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            # update_id UNIQUE constraint hit — already processed.
            logger.info("Skipping duplicate Telegram update_id=%s", update_id)
            return None


async def mark_pending_in_progress(pending_id: int) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "UPDATE pending_actions SET state='in_progress', updated_at=? WHERE id=?",
            (now_iso(), pending_id),
        )
        await db.commit()


async def complete_pending_action(pending_id: int) -> None:
    """Mark an action as completed. The row is kept (not deleted) so it
    serves as an idempotency record for the update_id."""
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "UPDATE pending_actions SET state='completed', updated_at=?, completed_at=? WHERE id=?",
            (now, now, pending_id),
        )
        await db.commit()


async def fail_pending_action(pending_id: int, error: str) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "UPDATE pending_actions SET state='failed', error=?, updated_at=? WHERE id=?",
            (error[:500], now_iso(), pending_id),
        )
        await db.commit()


async def list_stuck_pending_actions(stuck_after_minutes: int = 5) -> list[dict]:
    """Return rows still in pending/in_progress state past the timeout —
    these likely belong to a crashed handler. Caller decides whether to
    notify the principal, retry, or just log."""
    cutoff = (datetime.now(TZ) - timedelta(minutes=stuck_after_minutes)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM pending_actions
               WHERE state IN ('pending','in_progress') AND updated_at < ?
               ORDER BY updated_at ASC""",
            (cutoff,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def purge_old_pending_actions(retention_days: int = 7) -> int:
    """Drop completed/failed rows older than retention_days. Returns rows
    deleted. Keeps the table bounded over time."""
    cutoff = (datetime.now(TZ) - timedelta(days=retention_days)).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        cur = await db.execute(
            "DELETE FROM pending_actions WHERE state IN ('completed','failed') AND updated_at < ?",
            (cutoff,),
        )
        await db.commit()
        return cur.rowcount


# ─────────────────────────────────────────── iCLOUD RETRY QUEUE ───────────────────────────────────────────

# Exponential backoff: 1min → 5min → 15min → 1h → 4h → give up
_RETRY_BACKOFFS_SECONDS = [60, 300, 900, 3600, 14400]


async def enqueue_icloud_retry(operation: str, meeting_id: Optional[str],
                                payload: dict, error: str) -> None:
    """Add a failed iCloud operation to the retry queue."""
    next_at = (datetime.now(TZ) + timedelta(seconds=_RETRY_BACKOFFS_SECONDS[0])).isoformat()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO icloud_retry_queue
               (operation, meeting_id, payload, attempts, next_attempt_at, last_error, created_at)
               VALUES (?, ?, ?, 0, ?, ?, ?)""",
            (operation, meeting_id, json.dumps(payload, ensure_ascii=False),
             next_at, error[:500], now_iso()),
        )
        await db.commit()


async def list_due_icloud_retries(limit: int = 20) -> list[dict]:
    now = now_iso()
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT * FROM icloud_retry_queue
               WHERE next_attempt_at <= ? AND attempts < ?
               ORDER BY next_attempt_at ASC LIMIT ?""",
            (now, len(_RETRY_BACKOFFS_SECONDS), limit),
        )
        rows = await cur.fetchall()
        results = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                d["payload"] = {}
            results.append(d)
        return results


async def mark_icloud_retry_success(retry_id: int) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("DELETE FROM icloud_retry_queue WHERE id = ?", (retry_id,))
        await db.commit()


async def mark_icloud_retry_dead(retry_id: int, error: str) -> None:
    """Permanently retire a retry row — sets attempts past the max so the
    list_due_icloud_retries() WHERE clause filters it out. Use for
    unrecoverable failures (auth, missing calendar) where further retries
    would just spam logs without ever succeeding."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            "UPDATE icloud_retry_queue SET attempts = ?, last_error = ? WHERE id = ?",
            (len(_RETRY_BACKOFFS_SECONDS), error[:500], retry_id),
        )
        await db.commit()


async def mark_icloud_retry_failure(retry_id: int, error: str) -> None:
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT attempts FROM icloud_retry_queue WHERE id = ?", (retry_id,))
        row = await cur.fetchone()
        if not row:
            return
        new_attempts = row["attempts"] + 1
        if new_attempts >= len(_RETRY_BACKOFFS_SECONDS):
            # Permanent failure — keep row for inspection but stop trying
            await db.execute(
                "UPDATE icloud_retry_queue SET attempts = ?, last_error = ? WHERE id = ?",
                (new_attempts, error[:500], retry_id),
            )
        else:
            next_at = (datetime.now(TZ) + timedelta(seconds=_RETRY_BACKOFFS_SECONDS[new_attempts])).isoformat()
            await db.execute(
                "UPDATE icloud_retry_queue SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
                (new_attempts, next_at, error[:500], retry_id),
            )
        await db.commit()


# ─────────────────────────────────────────── LLM AUDIT LOG ───────────────────────────────────────────

async def log_llm_call(
    provider: str,
    model: Optional[str],
    purpose: str,
    input_hash: Optional[str],
    input_chars: int,
    input_tokens: Optional[int],
    output_tokens: Optional[int],
    cache_read_tokens: Optional[int] = None,
    cache_creation_tokens: Optional[int] = None,
    redacted_terms_count: int = 0,
    estimated_cost_usd: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    """Append an audit row. NEVER stores the input itself — only its hash and metrics."""
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute(
            """INSERT INTO llm_audit_log (
                ts, provider, model, purpose, input_hash, input_chars,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                redacted_terms_count, estimated_cost_usd, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now_iso(), provider, model, purpose, input_hash, input_chars,
                input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                redacted_terms_count, estimated_cost_usd, error,
            ),
        )
        await db.commit()
