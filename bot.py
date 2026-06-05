"""Yordamchi — main entry point. Runs the Telegram bot.

Run: python bot.py
"""

import asyncio
import logging
import signal
import sys
import warnings

# Silence noisy third-party logs that aren't actionable for us:
# - urllib3-future emits a WARNING when a server advertises HTTP/3 via Alt-Svc
#   but the local stack can't negotiate it. caldav (Apple iCloud) triggers this.
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=DeprecationWarning, module="caldav")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, ErrorEvent, MenuButtonDefault

import config
import database
import handlers
from fsm_storage import SQLiteStorage
from scheduler import YordamchiScheduler

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("yordamchi")


async def _register_bot_commands(bot: Bot) -> None:
    """Register the slash-command list shown in Telegram's command picker."""
    commands = [
        BotCommand(command="cockpit", description="🎛 Boshqaruv paneli"),
        BotCommand(command="today", description="📅 Bugungi briefing"),
        BotCommand(command="tasks", description="📌 Vazifalar"),
        BotCommand(command="reminders", description="⏰ Eslatmalar"),
        BotCommand(command="notes", description="📝 Notes (Inbox)"),
        BotCommand(command="team", description="👥 Ijrochilar paneli"),
        BotCommand(command="risks", description="🚨 Risklar paneli"),
        BotCommand(command="new", description="➕ Yangi vazifa"),
        BotCommand(command="meetings", description="🤝 Uchrashuvlar"),
        BotCommand(command="stats", description="📊 Statistika"),
        BotCommand(command="export", description="📤 Vazifalarni eksport (Excel)"),
        BotCommand(command="search", description="🔍 Qidiruv"),
        BotCommand(command="plan", description="🎯 Executive reja"),
        BotCommand(command="settings", description="⚙️ Sozlamalar"),
        BotCommand(command="calendar", description="📆 iCloud kalendar"),
        BotCommand(command="delegations", description="👥 Delegatsiyalar trekeri"),
        BotCommand(command="diagnostics", description="🔍 Bot holati"),
        BotCommand(command="backup", description="💾 Backup yaratish"),
        BotCommand(command="cancel", description="✕ Joriy amalni bekor qilish"),
        BotCommand(command="help", description="Yordam"),
    ]
    try:
        await bot.set_my_commands(commands)
        logger.info("Bot commands registered: %d", len(commands))
    except Exception:
        logger.exception("Failed to register bot commands")


async def _clear_menu_button(bot: Bot) -> None:
    """Ensure Telegram chat menu button is the default (commands list)."""
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except Exception:
        pass


async def main() -> None:
    config.ensure_paths()
    await database.init()
    logger.info("Database initialized")

    # Surface any in-flight user requests that didn't finish before the last
    # shutdown (handler crash, kill -9, OOM). These rows are diagnostic only —
    # we don't auto-retry because the user may have already worked around them.
    try:
        stuck = await database.list_stuck_pending_actions(stuck_after_minutes=5)
        if stuck:
            logger.warning(
                "Found %d stuck pending_actions from previous run "
                "(state in {pending,in_progress} older than 5min). "
                "First stuck id=%s, text=%r",
                len(stuck), stuck[0]["id"], (stuck[0].get("user_text") or "")[:80],
            )
        purged = await database.purge_old_pending_actions(retention_days=7)
        if purged:
            logger.info("Purged %d old pending_actions rows (>7 days, completed/failed)", purged)
    except Exception:
        logger.exception("pending_actions startup sweep failed (non-fatal)")

    # Warm iCloud CalDAV connection cache so the first user-triggered push is sub-second.
    if config.ICLOUD_ENABLED:
        try:
            import calendar_service
            cal = await asyncio.to_thread(calendar_service._get_calendar_cached)
            if cal:
                try:
                    cal_name = cal.get_display_name()
                except Exception:
                    cal_name = getattr(cal, "name", "?")
                logger.info("iCloud cache primed (calendar: %s)", cal_name)
            else:
                logger.warning("iCloud cache prime returned no calendar")
        except Exception:
            logger.exception("iCloud cache prime failed (non-fatal)")

    bot = Bot(
        token=config.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    fsm = SQLiteStorage()
    await fsm.init()
    # Drop FSM rows older than 24 hours on startup — backstop for the
    # in-handler TTL middleware (30 min) when the user never returns.
    purged_fsm = await fsm.purge_old_rows(max_age_hours=24)
    if purged_fsm:
        logger.info("Purged %d stale FSM rows (>24h)", purged_fsm)
    dispatcher = Dispatcher(storage=fsm)
    dispatcher.include_router(handlers.router)

    @dispatcher.errors()
    async def _on_unhandled_error(event: ErrorEvent) -> bool:
        """Catch-all safety net: any handler exception not handled locally lands
        here. Log it AND tell the user something went wrong — without this, an
        unexpected error leaves the user staring at silence."""
        logger.exception("Unhandled update error", exc_info=event.exception)
        chat_id = None
        upd = event.update
        try:
            if getattr(upd, "message", None):
                chat_id = upd.message.chat.id
            elif getattr(upd, "callback_query", None) and upd.callback_query.message:
                chat_id = upd.callback_query.message.chat.id
                try:
                    await upd.callback_query.answer("⚠️ Texnik xato")
                except Exception:
                    pass
        except Exception:
            pass
        if chat_id is not None:
            try:
                # Surface the real root cause (single-user bot) instead of a
                # generic "Texnik xato" — handlers._humanize_error maps the
                # exception to a clear O'zbek reason (plain text, always delivers).
                await bot.send_message(chat_id, handlers._humanize_error(event.exception))
            except Exception:
                logger.debug("Could not deliver error notice to user")
        return True  # handled — stop propagation

    await _register_bot_commands(bot)
    await _clear_menu_button(bot)

    scheduler = YordamchiScheduler(bot)
    scheduler.start()
    # Apply user-configured briefing times (DB-backed; defaults to 08:00 / 18:00 if unset).
    await scheduler.apply_briefing_settings()
    await scheduler.apply_reminder_settings()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass

    me = await bot.get_me()
    logger.info("Bot started: @%s (id=%s). Principal user_id=%s", me.username, me.id, config.PRINCIPAL_USER_ID)

    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    )
    tasks_to_watch = {polling_task, asyncio.create_task(stop_event.wait())}

    done, pending = await asyncio.wait(tasks_to_watch, return_when=asyncio.FIRST_COMPLETED)

    logger.info("Shutting down...")
    for task in pending:
        task.cancel()
    try:
        await dispatcher.stop_polling()
    except Exception:
        pass
    scheduler.stop()
    await bot.session.close()
    logger.info("Stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
