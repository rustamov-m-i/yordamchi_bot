"""Background scheduler: morning/evening briefings, task & meeting reminders, follow-up intelligence."""

import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from caldav.lib.error import AuthorizationError, NotFoundError

import calendar_service
import claude_service
import config
import database
from typing import Optional

# Errors that mean "this iCloud operation will never succeed without manual
# intervention" — auth failure, deleted calendar, etc. We mark these dead
# immediately instead of burning the full 5-attempt backoff sequence.
_ICLOUD_PERMANENT_ERRORS = (AuthorizationError, NotFoundError, PermissionError)

logger = logging.getLogger(__name__)

# Singleton instance — used by handlers to register one-shot reminders when meetings are scheduled.
_instance: Optional["YordamchiScheduler"] = None


def get_scheduler() -> Optional["YordamchiScheduler"]:
    return _instance


class YordamchiScheduler:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
        self.meeting_reminder_min = 15
        self.task_reminder_hours = 2
        # Lazy-imported once and cached; sentinel False means "import attempted
        # and failed" so we don't retry the (expensive, log-spamming) import
        # on every 5-minute sweep.
        self._meeting_prep_generator: object = None
        global _instance
        _instance = self

    def _get_meeting_prep_generator(self):
        """Return _generate_meeting_prep coroutine factory, or None if it
        can't be imported. Caches both success and failure to avoid retrying
        a broken import on every sweep."""
        if self._meeting_prep_generator is False:
            return None
        if self._meeting_prep_generator is not None:
            return self._meeting_prep_generator
        try:
            from handlers import _generate_meeting_prep
        except Exception:
            logger.exception("Cannot import meeting prep generator (caching failure)")
            self._meeting_prep_generator = False
            return None
        self._meeting_prep_generator = _generate_meeting_prep
        return _generate_meeting_prep

    @staticmethod
    def _parse_hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
        """Parse an 'HH:MM' string. Returns default on invalid input."""
        try:
            h, m = (value or "").split(":", 1)
            h, m = int(h), int(m)
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h, m
        except (ValueError, AttributeError):
            pass
        return default

    def _in_quiet_hours(self, settings: dict) -> bool:
        """True if the current local time falls inside the user's configured
        quiet hours window. Returns False if quiet hours are disabled or
        misconfigured. Handles wrap-around windows (e.g. 22:00 → 07:00)."""
        if not settings.get("quiet_hours_enabled", False):
            return False
        start = self._parse_hhmm(settings.get("quiet_hours_start", "22:00"), (22, 0))
        end = self._parse_hhmm(settings.get("quiet_hours_end", "07:00"), (7, 0))
        now = datetime.now(database.TZ)
        cur = (now.hour, now.minute)
        if start == end:
            return False  # zero-length window
        if start < end:
            # Same-day window (e.g. 13:00 → 14:00 lunch quiet)
            return start <= cur < end
        # Wrap-around window (e.g. 22:00 → 07:00) — quiet if we're past
        # start OR before end.
        return cur >= start or cur < end

    def start(self) -> None:
        # Briefings are registered with defaults; an async _apply_settings_briefings()
        # call right after start() reschedules them with the user's settings.
        self.scheduler.add_job(
            self._morning_briefing,
            CronTrigger(hour=8, minute=0, timezone=config.TIMEZONE),
            id="morning_briefing",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._evening_summary,
            CronTrigger(hour=18, minute=0, timezone=config.TIMEZONE),
            id="evening_summary",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._followup_check,
            IntervalTrigger(hours=6),
            id="followup_check",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(minutes=10),
        )
        # Rehydrate one-shot reminders for meetings that already exist in the DB
        # (covers restarts). Sweeps every 5 minutes as a safety net.
        self.scheduler.add_job(
            self._reschedule_pending_meeting_reminders,
            IntervalTrigger(minutes=5),
            id="meeting_reminder_rehydrate",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(seconds=10),
        )
        self.scheduler.add_job(
            self._task_reminder_sweep,
            IntervalTrigger(minutes=5),
            id="task_reminder",
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._reminder_sweep,
            IntervalTrigger(minutes=1),
            id="personal_reminder",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(seconds=20),
        )
        self.scheduler.add_job(
            self._meeting_prep_sweep,
            IntervalTrigger(minutes=5),
            id="meeting_prep",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(seconds=30),
        )
        self.scheduler.add_job(
            self._post_meeting_followup_sweep,
            IntervalTrigger(minutes=15),
            id="post_meeting_followup",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(minutes=5),
        )
        # Hourly conversation_history trim. claude_service.process_message already
        # trims on each call, but during long silences (overnight, weekends) the
        # table can drift; this guarantees an upper bound regardless of activity.
        self.scheduler.add_job(
            self._trim_history_sweep,
            IntervalTrigger(hours=1),
            id="trim_history",
            replace_existing=True,
            next_run_time=datetime.now(database.TZ) + timedelta(minutes=10),
        )
        # Nightly retention purge — NBU / banking compliance time-based TTL.
        # Runs at 03:00 local when the bot is idle and DB load is minimal.
        self.scheduler.add_job(
            self._retention_purge_sweep,
            CronTrigger(hour=3, minute=0, timezone=config.TIMEZONE),
            id="retention_purge",
            replace_existing=True,
        )
        # Friday 18:00 — weekly retrospective via Claude.
        self.scheduler.add_job(
            self._weekly_retrospective,
            CronTrigger(day_of_week="fri", hour=18, minute=0, timezone=config.TIMEZONE),
            id="weekly_retrospective",
            replace_existing=True,
        )
        # Nightly 02:30 — Haiku-based dependency / blocker analysis.
        # Only pushes a message if something actionable is found.
        self.scheduler.add_job(
            self._proactive_dependency_check,
            CronTrigger(hour=2, minute=30, timezone=config.TIMEZONE),
            id="proactive_dependency_check",
            replace_existing=True,
        )
        # Daily 09:30 — delegation auto-chase: nudge about tasks delegated to
        # others that have been open too long. Pushes only when stale ones exist.
        self.scheduler.add_job(
            self._stale_delegation_digest,
            CronTrigger(hour=9, minute=30, timezone=config.TIMEZONE),
            id="stale_delegation_digest",
            replace_existing=True,
        )

        if config.ICLOUD_ENABLED:
            self.scheduler.add_job(
                self._icloud_sync,
                IntervalTrigger(minutes=config.ICLOUD_SYNC_INTERVAL_MIN),
                id="icloud_sync",
                replace_existing=True,
                next_run_time=datetime.now(database.TZ) + timedelta(seconds=20),
            )
            self.scheduler.add_job(
                self._icloud_retry_sweep,
                IntervalTrigger(minutes=2),
                id="icloud_retry",
                replace_existing=True,
                next_run_time=datetime.now(database.TZ) + timedelta(minutes=1),
            )
            logger.info("iCloud sync enabled (every %d min) + retry queue (every 2 min)",
                        config.ICLOUD_SYNC_INTERVAL_MIN)

        self.scheduler.start()
        logger.info("Scheduler started")

    def remove_meeting_reminder(self, meeting_id: str) -> None:
        """Cancel a scheduled reminder for a meeting (idempotent)."""
        job_id = f"meeting_reminder:{meeting_id}"
        try:
            self.scheduler.remove_job(job_id)
            logger.info("Meeting reminder cancelled for %s", meeting_id)
        except Exception:
            # Job may not exist (e.g. reminder already fired or never scheduled)
            pass

    def schedule_meeting_reminder(self, meeting_id: str, meeting_start_iso: str) -> None:
        """Register a one-shot reminder for a single meeting.

        Called by handlers right after a meeting is created. Safe to call repeatedly
        for the same meeting_id — replace_existing=True ensures only one job exists.
        """
        try:
            start = datetime.fromisoformat(meeting_start_iso)
        except (ValueError, TypeError):
            logger.warning("Cannot schedule reminder: invalid datetime %r", meeting_start_iso)
            return
        # Aware inputs from other zones get converted, not relabeled — calling
        # localize() on an already-aware dt would shift wall-clock by the offset.
        if start.tzinfo is None:
            start = database.TZ.localize(start)
        else:
            start = start.astimezone(database.TZ)
        lead_minutes = max(1, int(getattr(self, "meeting_reminder_min", 15) or 15))
        fire_at = start - timedelta(minutes=lead_minutes)
        if fire_at <= datetime.now(database.TZ):
            logger.info("Skipping past-due meeting reminder for %s (start=%s)", meeting_id, start)
            return
        self.scheduler.add_job(
            self._fire_meeting_reminder,
            DateTrigger(run_date=fire_at, timezone=config.TIMEZONE),
            args=[meeting_id, lead_minutes],
            id=f"meeting_reminder:{meeting_id}",
            replace_existing=True,
            misfire_grace_time=300,
        )
        logger.info("Meeting reminder scheduled for %s at %s", meeting_id, fire_at.isoformat())

    async def apply_briefing_settings(self) -> None:
        """Read current morning/evening times from DB settings and reschedule jobs.

        Called once right after start() (post-DB-init) and again every time the user
        changes a briefing time from the /settings UI. Idempotent.
        """
        settings = await database.get_settings()
        morning_h, morning_m = self._parse_hhmm(
            settings.get("morning_briefing_time", "08:00"), default=(8, 0)
        )
        evening_h, evening_m = self._parse_hhmm(
            settings.get("evening_summary_time", "18:00"), default=(18, 0)
        )
        self.scheduler.reschedule_job(
            "morning_briefing",
            trigger=CronTrigger(hour=morning_h, minute=morning_m, timezone=config.TIMEZONE),
        )
        self.scheduler.reschedule_job(
            "evening_summary",
            trigger=CronTrigger(hour=evening_h, minute=evening_m, timezone=config.TIMEZONE),
        )
        logger.info("Briefings rescheduled from settings — morning %02d:%02d, evening %02d:%02d",
                    morning_h, morning_m, evening_h, evening_m)

    async def apply_reminder_settings(self) -> None:
        """Read reminder lead times from DB and re-register pending meeting reminders."""
        settings = await database.get_settings()
        try:
            self.meeting_reminder_min = max(1, int(settings.get("meeting_reminder_min", 15)))
        except (TypeError, ValueError):
            self.meeting_reminder_min = 15
        try:
            self.task_reminder_hours = max(1, int(settings.get("task_reminder_hours", 2)))
        except (TypeError, ValueError):
            self.task_reminder_hours = 2
        await self._reschedule_pending_meeting_reminders()
        logger.info(
            "Reminder settings applied — meetings %d min, tasks %d h",
            self.meeting_reminder_min,
            self.task_reminder_hours,
        )

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    async def _send(self, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None,
                     bypass_quiet_hours: bool = False) -> None:
        try:
            settings = await database.get_settings()
            if not settings.get("notifications_enabled", True):
                logger.info("Scheduled message suppressed because notifications are disabled")
                return
            # Quiet hours — silence non-urgent pushes during user's chosen
            # window (e.g. 22:00 → 07:00). Urgent flows that absolutely must
            # interrupt (P0 escalations) can pass bypass_quiet_hours=True.
            if not bypass_quiet_hours and self._in_quiet_hours(settings):
                logger.info("Scheduled message suppressed by quiet hours")
                return
        except Exception:
            logger.exception("Failed to read notification settings; sending message anyway")
        try:
            await self.bot.send_message(
                config.PRINCIPAL_USER_ID, text, parse_mode="Markdown", reply_markup=reply_markup
            )
        except TelegramBadRequest as e:
            if "parse" in str(e).lower():
                try:
                    await self.bot.send_message(config.PRINCIPAL_USER_ID, text, reply_markup=reply_markup)
                except Exception:
                    logger.exception("Failed to send scheduled message (plain fallback)")
            else:
                logger.exception("TelegramBadRequest sending scheduled message")
        except Exception:
            logger.exception("Failed to send scheduled message")

    async def _morning_briefing(self) -> None:
        logger.info("Generating morning briefing")
        from handlers import _build_briefing_text
        text = await _build_briefing_text()
        if text:
            await self._send(text)

    async def _evening_summary(self) -> None:
        logger.info("Generating evening summary")
        response = await claude_service.process_message(
            "", internal_directive="[INTERNAL] generate_evening_summary"
        )
        text = response.get("user_message", "")
        if text:
            await self._send(text)

    async def _followup_check(self) -> None:
        # A proactive follow-up is a nag the principal reads later — there is never
        # a reason to GENERATE one overnight, and _send would suppress it during
        # quiet hours / when notifications are off anyway. Skip the paid LLM call
        # entirely in those cases instead of paying to produce a message we'd drop.
        # (Fixed 22:00–08:00 sleep window is checked IN ADDITION to quiet hours,
        # which may be disabled in settings yet a 3 AM follow-up is still useless.)
        hour = datetime.now(database.TZ).hour
        if hour >= 22 or hour < 8:
            logger.info("Skipping follow-up check during sleep hours (%02d:00 local)", hour)
            return
        try:
            settings = await database.get_settings()
            if not settings.get("notifications_enabled", True) or self._in_quiet_hours(settings):
                logger.info("Skipping follow-up check — message would be suppressed")
                return
        except Exception:
            logger.exception("Follow-up check: settings read failed; proceeding")

        logger.info("Running follow-up check")
        response = await claude_service.process_message(
            "",
            internal_directive=(
                "[INTERNAL] check_followups — review current state for: "
                "(a) in_progress tasks not updated in 48h, "
                "(b) overdue tasks, "
                "(c) meetings ended without follow-up actions. "
                "If nothing actionable, respond with user_message='' and actions=[]."
            ),
        )
        text = response.get("user_message", "").strip()
        if text:
            await self._send(text)

    async def _fire_meeting_reminder(self, meeting_id: str, lead_minutes: Optional[int] = None) -> None:
        """Single-meeting reminder. Scheduled at create-time.
        Claims the reminder slot BEFORE sending — if a parallel sweep already
        claimed it (e.g. multi-instance bot), we silently skip instead of
        double-notifying the user."""
        meeting = await database.get_meeting(meeting_id)
        if not meeting:
            logger.info("Meeting %s no longer exists, skipping reminder", meeting_id)
            return
        if meeting.get("reminded_at"):
            return
        if not await database.mark_meeting_reminded(meeting_id):
            return
        try:
            dt = datetime.fromisoformat(meeting["datetime_start"]).astimezone(database.TZ)
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            time_str = meeting["datetime_start"]
        participants = ", ".join(meeting.get("participants", [])) or "—"
        location = meeting.get("location_or_link") or "—"
        prep = meeting.get("prep_notes") or ""
        text_lines = [
            f"📞 **15 daqiqa qoldi: {meeting['title']}**",
            f"• Vaqt: {time_str}",
            f"• Ishtirokchilar: {participants}",
            f"• Joy/havola: {location}",
        ]
        if prep:
            text_lines.append(f"• Tayyorgarlik: {prep}")
        lead_label = lead_minutes or self.meeting_reminder_min
        text_lines[0] = f"📞 **{lead_label} daqiqa qoldi: {meeting['title']}**"
        await self._send("\n".join(text_lines))

    async def _meeting_prep_sweep(self) -> None:
        """Send a prep brief once for meetings starting in the next hour.
        Claims each meeting BEFORE generating prep — if a sibling sweep already
        claimed it, we skip immediately and avoid both the duplicate Claude call
        and the duplicate notification."""
        meetings = await database.list_meetings_needing_prep(window_minutes=60)
        if not meetings:
            return
        gen = self._get_meeting_prep_generator()
        if gen is None:
            return
        for meeting in meetings[:3]:
            if not await database.mark_meeting_prep_sent(meeting["id"]):
                continue
            try:
                text = await gen(meeting)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📝 Keyin action items", callback_data=f"meeting_followup:{meeting['id']}")
                ]])
                await self._send(text, reply_markup=kb)
            except Exception:
                logger.exception("Meeting prep sweep failed for %s", meeting.get("id"))

    async def _post_meeting_followup_sweep(self) -> None:
        """Ask for notes after a meeting so action items don't evaporate.
        Claims first to avoid duplicate prompts across overlapping sweeps."""
        meetings = await database.list_meetings_needing_followup(min_age_minutes=30, max_age_hours=24)
        for meeting in meetings[:3]:
            if not await database.mark_meeting_followup_sent(meeting["id"]):
                continue
            try:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="📝 Action items chiqarish", callback_data=f"meeting_followup:{meeting['id']}")
                ]])
                await self._send(
                    f"📝 **Uchrashuv follow-up:** {meeting['title']}\n\n"
                    "Uchrashuvdan keyingi qisqa yozuv yoki ovoz yuborsangiz, action itemlarni tasklarga ajrataman.",
                    reply_markup=kb,
                )
            except Exception:
                logger.exception("Post-meeting follow-up sweep failed for %s", meeting.get("id"))

    async def _icloud_prime_cache(self) -> None:
        """Eagerly warm the CalDAV client cache so the first user push is fast."""
        try:
            import asyncio
            cal = await asyncio.to_thread(calendar_service._get_calendar_cached)
            if cal:
                logger.info("iCloud cache primed (calendar: %s)", getattr(cal, "name", "?"))
        except Exception:
            logger.exception("iCloud cache prime failed (non-fatal)")

    async def _trim_history_sweep(self) -> None:
        """Bound conversation_history regardless of LLM activity. Without this
        the table only shrinks when a Claude call runs (claude_service.process_message
        calls trim_history at the end). During multi-day silences the table can
        accumulate stale rows from background internal-directive calls."""
        try:
            await database.trim_history(keep=200)
        except Exception:
            logger.exception("trim_history sweep failed")

    async def _proactive_dependency_check(self) -> None:
        """Nightly Haiku pass — feed all open tasks to Claude and ask for
        critical-path / circular-dependency / blocker warnings. Cheap because
        Haiku + cache; pushes only when something actionable is found."""
        try:
            tasks = await database.list_tasks(status_in=["todo", "in_progress"], limit=100)
            if len(tasks) < 5:
                return  # Too few to warrant a dep analysis
            task_brief = "\n".join(
                f"- [{t['priority']}] {t['title']} (id={t['id']}, deadline={t.get('deadline') or '—'}, "
                f"assignee={t.get('assignee') or '—'})"
                for t in tasks[:50]
            )
            directive = (
                "[INTERNAL] task_dependency_check\n\n"
                "Quyidagi aktiv vazifalar ro'yxati. Tahlil qiling:\n"
                "  1. Critical path — qaysi vazifalar boshqa muhim vazifalarni bloklab turadi?\n"
                "  2. Risk — deadline yaqin va aktiv ish ko'rinmayotgan vazifalar.\n"
                "  3. Circular dependency yoki konflikt.\n\n"
                f"VAZIFALAR:\n{task_brief}\n\n"
                "Agar hech qanday risk yo'q bo'lsa, user_message='' qaytaring.\n"
                "Aks holda 2-3 punktli qisqa Uzbek alert yozing."
            )
            response = await claude_service.process_message(
                "", internal_directive=directive, complexity="fast",
            )
            text = (response.get("user_message") or "").strip()
            if text:
                await self._send("🔗 **VAZIFA BOG'LANISHLARI**\n\n" + text)
        except Exception:
            logger.exception("Proactive dependency check failed")

    async def _stale_delegation_digest(self) -> None:
        """Daily delegation auto-chase — surface tasks delegated to others that
        have been open >= 3 days so the principal follows up. No LLM, no spam:
        pushes only when stale delegations exist."""
        try:
            stale = await database.list_stale_delegations(min_age_days=3, limit=8)
            if not stale:
                return
            lines = [
                "📋 **DELEGATSIYA NAZORATI**",
                "",
                f"{len(stale)} ta topshiriq uzoq kutyapti — follow-up kerakmi?",
                "",
            ]
            for t in stale:
                age = int(t.get("age_days") or 0)
                badge = "🔴" if age >= 7 else "🟠" if age >= 5 else "🟡"
                who = (t.get("assignee") or "—").strip()
                title = (t.get("title") or "—").strip()
                lines.append(f"{badge} «{title[:50]}» — 👤 {who} · ⏱ {age} kun")
            lines.extend(["", "_Ijrochilar paneli → ⏳ Kutilayotganlar_"])
            await self._send("\n".join(lines))
        except Exception:
            logger.exception("Stale delegation digest failed")

    async def _weekly_retrospective(self) -> None:
        """Friday 18:00 — auto-generate the week's retrospective via Claude.
        Aggregates last-7-day stats and asks Claude to identify wins,
        bottlenecks, and one concrete suggestion for next week."""
        try:
            stats = await database.executive_stats(days=7)
            directive = (
                "[INTERNAL] weekly_retrospective\n\n"
                "Bu Juma kuni avtomatik hisobot. Quyidagi statistika asosida:\n"
                f"  - Yaratilgan vazifalar: {stats.get('tasks', {}).get('created_7d', 0)}\n"
                f"  - Yopilgan vazifalar: {stats.get('tasks', {}).get('done_7d', 0)}\n"
                f"  - O'tgan muddat: {stats.get('tasks', {}).get('overdue_count', 0)}\n"
                f"  - Risk score: {stats.get('risk_score', 0)}/100\n"
                f"  - Uchrashuvlar (7-kun): {stats.get('meetings', {}).get('count', 0)}\n\n"
                "Quyidagi tarkibda Uzbek tilida qisqa hisobot yozing (300-400 so'z):\n"
                "1. 🏆 HAFTANING G'ALABALARI (2-3 punkt)\n"
                "2. ⚠️ E'TIBOR KERAK (2-3 punkt)\n"
                "3. 🎯 KEYINGI HAFTA UCHUN TAVSIYA (1 aniq qadam)\n\n"
                "user_message ichiga to'liq matnni qaytaring. actions=[]."
            )
            response = await claude_service.process_message(
                "", internal_directive=directive, complexity="complex"
            )
            text = response.get("user_message", "").strip()
            if text:
                await self._send("📊 **HAFTALIK RETROSPEKTIV**\n\n" + text)
        except Exception:
            logger.exception("Weekly retrospective failed")

    async def _retention_purge_sweep(self) -> None:
        """Time-based retention purge — NBU / banking compliance. Drops
        conversation_history and llm_audit_log rows older than the
        configured TTL. Logged for audit trail."""
        try:
            conv = await database.purge_old_conversation_history(config.CONVERSATION_TTL_DAYS)
            audit = await database.purge_old_audit_logs(config.LLM_AUDIT_TTL_DAYS)
            if conv or audit:
                logger.info(
                    "Retention purge: %d conversation_history rows, %d llm_audit_log rows (TTL conv=%dd, audit=%dd)",
                    conv, audit, config.CONVERSATION_TTL_DAYS, config.LLM_AUDIT_TTL_DAYS,
                )
        except Exception:
            logger.exception("Retention purge sweep failed")

    async def _icloud_sync(self) -> None:
        """Pull next-30-days events from iCloud into local DB."""
        try:
            imported = await calendar_service.sync_events_to_db()
            if imported:
                logger.info("iCloud sync: %d new meetings imported", imported)
        except Exception:
            logger.exception("iCloud sync failed (non-fatal)")

    async def _icloud_retry_sweep(self) -> None:
        """Retry queued iCloud operations whose backoff has elapsed."""
        import asyncio
        from datetime import datetime as dt_

        due = await database.list_due_icloud_retries(limit=10)
        if not due:
            return

        processed = succeeded = failed = 0
        for item in due:
            processed += 1
            try:
                op = item["operation"]
                payload = item["payload"]
                if op == "push":
                    uid = await asyncio.to_thread(
                        calendar_service.push_meeting,
                        item["meeting_id"],
                        payload.get("title", "Uchrashuv"),
                        dt_.fromisoformat(payload["dt_start"]),
                        dt_.fromisoformat(payload["dt_end"]),
                        payload.get("participants"),
                        payload.get("location"),
                        payload.get("description"),
                    )
                    if uid:
                        # Persist UID on the meeting and clear the retry row
                        import aiosqlite
                        async with aiosqlite.connect(config.DATABASE_PATH) as db:
                            await db.execute(
                                "UPDATE meetings SET icloud_uid = ? WHERE id = ?",
                                (uid, item["meeting_id"]),
                            )
                            await db.commit()
                        await database.mark_icloud_retry_success(item["id"])
                        succeeded += 1
                    else:
                        await database.mark_icloud_retry_failure(item["id"], "push returned None")
                        failed += 1
                elif op == "delete":
                    ok = await asyncio.to_thread(calendar_service.delete_meeting, item["meeting_id"])
                    if ok:
                        await database.mark_icloud_retry_success(item["id"])
                        succeeded += 1
                    else:
                        await database.mark_icloud_retry_failure(item["id"], "delete returned False")
                        failed += 1
                else:
                    logger.warning("Unknown retry operation: %s", op)
                    await database.mark_icloud_retry_success(item["id"])  # drop unknown ops
                    succeeded += 1
            except _ICLOUD_PERMANENT_ERRORS as e:
                # Auth/permission/not-found are not transient. Retrying 4 more
                # times with backoff just delays the alert without fixing it.
                logger.error(
                    "iCloud permanent failure on retry %s (op=%s) — marking dead: %s",
                    item["id"], item.get("operation"), e,
                )
                await database.mark_icloud_retry_dead(item["id"], f"{type(e).__name__}: {e}")
                failed += 1
            except Exception as e:
                logger.exception("iCloud retry sweep item failed")
                await database.mark_icloud_retry_failure(item["id"], f"{type(e).__name__}: {e}")
                failed += 1

        # Single summary line per sweep — concise but informative for ops monitoring.
        still_pending = max(0, processed - succeeded - failed)
        logger.info(
            "iCloud retry sweep: %d processed, %d succeeded, %d failed, %d still pending",
            processed, succeeded, failed, still_pending,
        )

    async def _reschedule_pending_meeting_reminders(self) -> None:
        """Safety net: re-register one-shot reminders for any future meeting without a job.

        Handles two scenarios: (a) bot restart between meeting creation and reminder time,
        (b) meeting created via a path that didn't call schedule_meeting_reminder.
        """
        upcoming = await database.list_unreminded_future_meetings()
        for meeting in upcoming:
            self.schedule_meeting_reminder(meeting["id"], meeting["datetime_start"])

    async def _task_reminder_sweep(self) -> None:
        """Claim-then-notify: atomic mark_task_reminded acts as a lock token so
        overlapping sweeps (multi-instance / restart-overlap) cannot deliver
        the same reminder twice."""
        now = datetime.now(database.TZ)
        settings = await database.get_settings()
        try:
            hours = max(1, int(settings.get("task_reminder_hours", self.task_reminder_hours)))
        except (TypeError, ValueError):
            hours = self.task_reminder_hours
        self.task_reminder_hours = hours
        window_start = (now + timedelta(hours=hours, minutes=-5)).isoformat()
        window_end = (now + timedelta(hours=hours, minutes=5)).isoformat()
        due_soon = await database.list_due_in_window(window_start, window_end)
        # Raw P0/P1 kodlari foydalanuvchiga ko'rinmasin — Uzbek labellarda chiqaramiz.
        priority_uz = {"P0": "Shoshilinch", "P1": "Muhim", "P2": "Rejadagi", "P3": "Oddiy"}
        for task in due_soon:
            if not await database.mark_task_reminded(task["id"]):
                continue
            deadline = task.get("deadline", "")
            try:
                dt = datetime.fromisoformat(deadline)
                deadline_str = dt.strftime("%d-%m %H:%M")
            except (ValueError, TypeError):
                deadline_str = deadline
            pri_label = priority_uz.get(task.get("priority"), task.get("priority") or "—")
            await self._send(
                f"⏰ **{hours} soat qoldi:** {task['title']}\n"
                f"• Muddat: {deadline_str}\n"
                f"• Ustuvorlik: {pri_label}"
            )

    async def _reminder_sweep(self) -> None:
        """mark_reminder_sent is the atomic claim — if it returns False the
        reminder was already handled by a sibling sweep, so we skip the send."""
        due = await database.list_due_reminders(limit=20)
        if not due:
            return
        for reminder in due:
            try:
                if not await database.mark_reminder_sent(reminder["id"]):
                    continue
                remind_at = reminder.get("remind_at", "")
                try:
                    dt = datetime.fromisoformat(remind_at).astimezone(database.TZ)
                    time_label = dt.strftime("%d-%m %H:%M")
                except (ValueError, TypeError):
                    time_label = remind_at or "—"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"remdone:{reminder['id']}"),
                        InlineKeyboardButton(text="⏰ 15 daq", callback_data=f"remsnooze:{reminder['id']}:15m"),
                    ],
                    [
                        InlineKeyboardButton(text="🕐 1 soat", callback_data=f"remsnooze:{reminder['id']}:1h"),
                        InlineKeyboardButton(text="📅 Ertaga", callback_data=f"remsnooze:{reminder['id']}:1d"),
                    ],
                ])
                lines = [
                    f"⏰ **Eslatma:** {reminder['title']}",
                    f"• Vaqt: {time_label}",
                ]
                if reminder.get("note"):
                    lines.append(f"• Izoh: {reminder['note']}")
                if reminder.get("recurrence_rule"):
                    labels = {
                        "daily": "har kuni",
                        "weekly": "har hafta",
                        "monthly": "har oy",
                        "quarterly": "har chorak",
                        "yearly": "har yil",
                    }
                    lines.append(f"• Takror: {labels.get(reminder['recurrence_rule'], reminder['recurrence_rule'])}")
                await self._send("\n".join(lines), reply_markup=kb)
            except Exception:
                logger.exception("Reminder sweep failed for %s", reminder.get("id"))
