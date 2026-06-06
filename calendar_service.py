"""Apple iCloud Calendar integration (CalDAV).

Two-way sync:
  - Pull: every ICLOUD_SYNC_INTERVAL_MIN minutes, fetch events from iCloud and upsert into local DB.
  - Push: when the bot creates a meeting, also create the corresponding CalDAV event.

Authentication: Apple App-Specific Password (NOT the user's main Apple ID password).
Generate at https://account.apple.com → Sign-In and Security → App-Specific Passwords.

Why two-way:
  - If the principal creates an event on their iPhone/Mac, the bot picks it up next sync
    and includes it in briefings + conflict detection.
  - If the principal asks the bot to schedule a meeting, it lands on their phone/Mac calendar.

Failure modes are designed to be SOFT: any iCloud error logs a warning but does NOT block
the bot's normal operation. Local DB remains authoritative; iCloud is a side-channel.
"""

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import caldav
from caldav.lib.error import AuthorizationError
from icalendar import Calendar, Event, vText

import config
import database

logger = logging.getLogger(__name__)

ICLOUD_URL = "https://caldav.icloud.com/"

# ─────────────────────── CLIENT CACHE ───────────────────────
# Caching the CalDAV client + target calendar saves ~2-3 seconds per push because
# the TLS handshake + Basic auth + calendar discovery happen only once per TTL window.

_CACHE_TTL_SECONDS = 600  # 10 min — refresh in case of transient connection issues
_cache_lock = threading.Lock()
_cached_client: Optional[caldav.DAVClient] = None
_cached_calendar: Optional[caldav.Calendar] = None
_cached_at: float = 0.0


def _invalidate_cache() -> None:
    global _cached_client, _cached_calendar, _cached_at
    with _cache_lock:
        _cached_client = None
        _cached_calendar = None
        _cached_at = 0.0


def _is_transient_icloud_error(e: BaseException) -> bool:
    """True for read-stall / timeout / connection-drop errors — the expected,
    self-healing failures on a flaky laptop link (Apple HTTP/3 quirk, wifi/sleep,
    stale cached connection). These are logged concisely (no traceback) since the
    next sweep retries on a fresh connection. Auth/permission errors are NOT
    transient and stay loud (handled in _get_calendar_cached / push paths)."""
    s = (str(e) + " " + type(e).__name__).lower()
    return any(k in s for k in (
        "timeout", "timed out", "connection reset", "connection aborted",
        "max retries", "name or service not known", "temporarily unavailable",
        "remotedisconnected", "connectionerror", "protocolerror",
    ))


def _connect() -> Optional[caldav.DAVClient]:
    if not config.ICLOUD_ENABLED:
        return None
    return caldav.DAVClient(
        url=ICLOUD_URL,
        username=config.APPLE_ID,
        password=config.APPLE_APP_SPECIFIC_PASSWORD,
    )


def _get_calendar_cached() -> Optional[caldav.Calendar]:
    """Return the target Calendar object, with TTL-cached client+calendar to skip TLS handshake.

    On transient OSError(40, 'Message too long') — known iCloud CalDAV issue — retry 3 times
    before giving up. The cache is invalidated on each attempt for a clean reconnect.
    """
    global _cached_client, _cached_calendar, _cached_at
    now = time.time()
    with _cache_lock:
        if _cached_calendar is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
            return _cached_calendar

        last_err = None
        for attempt in range(3):
            client = _connect()
            if not client:
                return None
            try:
                principal = client.principal()
                calendars = principal.calendars()
            except AuthorizationError:
                logger.error("iCloud auth failed — check APPLE_ID and APPLE_APP_SPECIFIC_PASSWORD")
                return None
            except Exception as e:
                last_err = e
                logger.debug("iCloud connect attempt %d/3 failed: %s", attempt + 1, e)
                time.sleep(1.0 * (attempt + 1))
                continue

            if not calendars:
                logger.warning("No calendars found on iCloud account")
                return None

            target = None
            if config.ICLOUD_CALENDAR_NAME:
                for cal in calendars:
                    try:
                        if cal.name == config.ICLOUD_CALENDAR_NAME:
                            target = cal
                            break
                    except Exception:
                        continue
                if target is None:
                    logger.warning(
                        "Calendar %r not found — using first. Available: %s",
                        config.ICLOUD_CALENDAR_NAME,
                        [getattr(c, "name", "?") for c in calendars],
                    )

            _cached_client = client
            _cached_calendar = target or calendars[0]
            _cached_at = now
            return _cached_calendar

        logger.warning("iCloud connection failed after 3 attempts: %s", last_err)
        return None


# Backward-compat alias
def _get_calendar(client: caldav.DAVClient) -> Optional[caldav.Calendar]:
    return _get_calendar_cached()


# ─────────────────────── DIAGNOSTICS ───────────────────────


def list_available_calendars() -> list[str]:
    """Probe iCloud connection and return calendar names. Used by /calendar command."""
    client = _connect()
    if not client:
        return []
    try:
        principal = client.principal()
        return [getattr(c, "name", "?") for c in principal.calendars()]
    except Exception as e:
        logger.warning("Failed to list iCloud calendars: %s", e)
        return []


def test_connection(max_retries: int = 5) -> tuple[bool, str]:
    """Return (success, message). Used by /calendar command.

    iCloud occasionally returns OSError(40, 'Message too long') — a TLS-layer
    fragmentation issue on macOS. Retry with a short delay before giving up.
    """
    if not config.ICLOUD_ENABLED:
        return False, "iCloud sozlanmagan. .env'da APPLE_ID va APPLE_APP_SPECIFIC_PASSWORD ni to'ldiring."
    last_err = ""
    for attempt in range(max_retries):
        _invalidate_cache()
        client = _connect()
        if not client:
            return False, "iCloud klientni yaratib bo'lmadi."
        try:
            principal = client.principal()
            cals = principal.calendars()
            names = [getattr(c, "name", "?") for c in cals]
            return True, f"Ulanish OK. {len(cals)} ta kalendar topildi: {', '.join(names[:5])}"
        except AuthorizationError:
            return False, "Avtorizatsiya xatosi. App-Specific Password to'g'rimi?"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.debug("iCloud connect attempt %d/%d failed: %s", attempt + 1, max_retries, last_err)
            time.sleep(1.5)
    return False, f"Xato (5 urinishdan keyin): {last_err}"


# ─────────────────────── PUSH ───────────────────────


def push_meeting(meeting_id: str, title: str, dt_start: datetime, dt_end: datetime,
                 participants: Optional[list[str]] = None,
                 location: Optional[str] = None,
                 description: Optional[str] = None) -> Optional[str]:
    """Create an event on iCloud. Returns the CalDAV event UID or None on failure.

    Note: this is a SYNCHRONOUS function — callers (handlers, scheduler) invoke it
    via asyncio.to_thread() because the caldav library is sync-only.
    """
    if not config.ICLOUD_ENABLED:
        return None

    t0 = time.monotonic()
    cal = _get_calendar_cached()
    t_conn = time.monotonic() - t0
    if not cal:
        logger.warning("iCloud push: calendar unavailable (after %.2fs)", t_conn)
        return None

    try:
        uid = f"yordamchi-{meeting_id}@{config.APPLE_ID.split('@')[0]}"
        ical = Calendar()
        ical.add("prodid", "-//Yordamchi Bot//yordamchi//")
        ical.add("version", "2.0")

        ev = Event()
        ev.add("uid", uid)
        ev.add("summary", title)
        ev.add("dtstart", dt_start)
        ev.add("dtend", dt_end)
        if location:
            ev.add("location", vText(location))
        desc_parts = []
        if description:
            desc_parts.append(description)
        if participants:
            desc_parts.append("Ishtirokchilar: " + ", ".join(participants))
        desc_parts.append("Yordamchi botdan yaratildi.")
        ev.add("description", "\n".join(desc_parts))
        ev.add("dtstamp", datetime.utcnow())
        ical.add_component(ev)

        t1 = time.monotonic()
        cal.save_event(ical.to_ical().decode("utf-8"))
        t_save = time.monotonic() - t1
        t_total = time.monotonic() - t0
        logger.info("iCloud event pushed: %s (%s) — conn=%.2fs save=%.2fs total=%.2fs",
                    meeting_id, title, t_conn, t_save, t_total)
        return uid
    except AuthorizationError as e:
        _invalidate_cache()
        logger.exception("iCloud auth error during push — cache invalidated")
        _enqueue_retry("push", meeting_id, {
            "title": title, "dt_start": dt_start.isoformat(), "dt_end": dt_end.isoformat(),
            "participants": participants, "location": location, "description": description,
        }, f"AuthorizationError: {e}")
        return None
    except Exception as e:
        # On any error, invalidate cache so next push reconnects fresh, queue for retry
        _invalidate_cache()
        logger.exception("Failed to push event to iCloud — queued for retry")
        _enqueue_retry("push", meeting_id, {
            "title": title, "dt_start": dt_start.isoformat(), "dt_end": dt_end.isoformat(),
            "participants": participants, "location": location, "description": description,
        }, f"{type(e).__name__}: {e}")
        return None


def _enqueue_retry(operation: str, meeting_id: Optional[str], payload: dict, error: str) -> None:
    """Schedule a failed iCloud op for later retry. Sync-safe wrapper around the async DB call."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Schedule as a task on the running event loop (we're inside a `to_thread` call)
            asyncio.run_coroutine_threadsafe(
                database.enqueue_icloud_retry(operation, meeting_id, payload, error),
                loop,
            )
        else:
            asyncio.run(database.enqueue_icloud_retry(operation, meeting_id, payload, error))
    except Exception:
        logger.exception("Failed to enqueue iCloud retry (will be lost)")


def delete_meeting(meeting_id: str) -> bool:
    """Remove the corresponding iCloud event, if any."""
    if not config.ICLOUD_ENABLED:
        return False
    cal = _get_calendar_cached()
    if not cal:
        return False
    try:
        uid_prefix = f"yordamchi-{meeting_id}@"
        for ev in cal.events():
            try:
                ical = Calendar.from_ical(ev.data)
                for comp in ical.walk("VEVENT"):
                    uid = str(comp.get("uid", ""))
                    if uid.startswith(uid_prefix):
                        ev.delete()
                        logger.info("iCloud event deleted: %s", meeting_id)
                        return True
            except Exception:
                continue
        return False
    except Exception:
        _invalidate_cache()
        logger.exception("Failed to delete iCloud event")
        return False


# ─────────────────────── PULL ───────────────────────


async def sync_events_to_db() -> int:
    """Pull next 30 days of iCloud events into the meetings table.

    De-dup strategy: events created by the bot have UID `yordamchi-<meeting_id>@...` and
    are skipped (already in DB). Events created externally are inserted with `source="icloud"`
    so we know they came from outside.

    Returns: number of new meetings imported.
    """
    if not config.ICLOUD_ENABLED:
        return 0

    import asyncio
    return await asyncio.to_thread(_sync_events_to_db_sync)


def _sync_events_to_db_sync() -> int:
    client = _connect()
    if not client:
        return 0
    cal = _get_calendar(client)
    if not cal:
        return 0

    now = datetime.now(database.TZ)
    end = now + timedelta(days=30)

    try:
        events = cal.date_search(start=now, end=end, expand=True)
    except Exception as e:
        # Drop the cached connection so the NEXT sweep reconnects fresh (a stale
        # connection is the usual cause of a 30s read-stall after the laptop's
        # network changed). Log transient timeouts concisely — one warning line,
        # not a full traceback every minute. Unexpected errors keep more detail.
        _invalidate_cache()
        if _is_transient_icloud_error(e):
            logger.warning("iCloud sync skipped (transient connection issue: %s) — "
                           "cache reset, will retry next sweep", type(e).__name__)
        else:
            logger.warning("iCloud date_search failed: %s: %s",
                           type(e).__name__, str(e)[:160])
        return 0

    imported = 0
    for ev in events:
        try:
            ical = Calendar.from_ical(ev.data)
            for comp in ical.walk("VEVENT"):
                uid = str(comp.get("uid", ""))
                if uid.startswith("yordamchi-"):
                    continue  # We pushed this ourselves, already in DB.

                title = str(comp.get("summary", "Untitled"))
                dt_start = comp.get("dtstart")
                dt_end = comp.get("dtend")
                if dt_start is None:
                    continue
                start_dt = dt_start.dt if hasattr(dt_start, "dt") else dt_start
                if isinstance(start_dt, datetime):
                    start_iso = start_dt.astimezone(database.TZ).isoformat()
                else:
                    # All-day event — represent as start of day in TZ
                    start_iso = database.TZ.localize(datetime.combine(start_dt, datetime.min.time())).isoformat()
                if dt_end is not None:
                    end_dt = dt_end.dt if hasattr(dt_end, "dt") else dt_end
                    if isinstance(end_dt, datetime):
                        end_iso = end_dt.astimezone(database.TZ).isoformat()
                    else:
                        end_iso = database.TZ.localize(datetime.combine(end_dt, datetime.min.time())).isoformat()
                else:
                    end_iso = None
                location = str(comp.get("location", "")) or None
                description = str(comp.get("description", "")) or None

                # Idempotent: if a meeting with same icloud_uid exists, skip.
                if _meeting_with_icloud_uid_exists(uid):
                    continue

                # Use synchronous DB write — caldav is sync, we're in a thread.
                import asyncio
                asyncio.run(_save_imported_meeting(uid, title, start_iso, end_iso, location, description))
                imported += 1
        except Exception:
            logger.exception("Failed to parse iCloud event")
            continue

    if imported:
        logger.info("Imported %d new meetings from iCloud", imported)
    return imported


def _meeting_with_icloud_uid_exists(uid: str) -> bool:
    """Check if a meeting with this icloud_uid is already in DB."""
    import sqlite3
    con = sqlite3.connect(config.DATABASE_PATH)
    try:
        cur = con.execute("SELECT 1 FROM meetings WHERE icloud_uid = ? LIMIT 1", (uid,))
        return cur.fetchone() is not None
    except sqlite3.OperationalError:
        # icloud_uid column doesn't exist yet (older DB)
        return False
    finally:
        con.close()


async def _save_imported_meeting(uid: str, title: str, start_iso: str,
                                  end_iso: Optional[str], location: Optional[str],
                                  description: Optional[str]) -> None:
    """Insert a meeting imported from iCloud, tagged with the source UID."""
    mid = await database.create_meeting({
        "title": title,
        "datetime_start": start_iso,
        "datetime_end": end_iso,
        "participants": [],
        "location_or_link": location,
        "agenda": description,
    })
    # Tag with icloud_uid for de-dup on next pull
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        await db.execute("UPDATE meetings SET icloud_uid = ? WHERE id = ?", (uid, mid))
        await db.commit()
