"""Telegram message and callback handlers."""

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup, default_state
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import calendar_service
import claude_service
import config
import database
import scheduler as scheduler_module
import voice_service


_TG_SOFT_LIMIT = 3500  # split outgoing messages above this to preserve readability


def _split_for_telegram(text: str, limit: int = _TG_SOFT_LIMIT) -> list[str]:
    """Split a long message at clean boundaries (blank lines, then single lines).
    Returns the original text as a 1-item list when under the limit.
    """
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf = ""
    # Prefer splitting at blank-line boundaries to keep sections intact.
    for block in text.split("\n\n"):
        chunk = block if not buf else f"{buf}\n\n{block}"
        if len(chunk) <= limit:
            buf = chunk
            continue
        if buf:
            parts.append(buf)
            buf = ""
        # Block alone exceeds the limit — fall back to per-line splitting.
        if len(block) <= limit:
            buf = block
            continue
        line_buf = ""
        for line in block.split("\n"):
            cand = line if not line_buf else f"{line_buf}\n{line}"
            if len(cand) <= limit:
                line_buf = cand
            else:
                if line_buf:
                    parts.append(line_buf)
                line_buf = line[:limit]
        if line_buf:
            buf = line_buf
    if buf:
        parts.append(buf)
    return parts


async def _safe_answer(message: Message, text: str, **kwargs) -> None:
    """Send a message, falling back to plain text if Markdown parsing fails.
    Auto-splits messages longer than _TG_SOFT_LIMIT at section boundaries.
    Reply markup is attached to the last chunk only.
    """
    chunks = _split_for_telegram(text)
    reply_markup = kwargs.pop("reply_markup", None)
    last_idx = len(chunks) - 1
    for i, chunk in enumerate(chunks):
        chunk_kwargs = dict(kwargs)
        if i == last_idx and reply_markup is not None:
            chunk_kwargs["reply_markup"] = reply_markup
        try:
            await message.answer(chunk, **chunk_kwargs)
        except TelegramBadRequest as e:
            if "can't parse entities" in str(e).lower() or "parse" in str(e).lower():
                chunk_kwargs.pop("parse_mode", None)
                await message.answer(chunk, **chunk_kwargs)
            else:
                raise


# ─────────────────────── PERSISTENT REPLY KEYBOARD ───────────────────────


# Main menu — Executive Task Management Assistant layout
BTN_COCKPIT = "🎛 Boshqaruv paneli"
BTN_TODAY = "📅 Bugun"
BTN_TASKS = "📌 Vazifalar"
BTN_REMINDERS = "⏰ Eslatmalar"
# Legacy reply-keyboard labels still cached on user devices — keep accepting them
# so the button works even if the keyboard hasn't refreshed yet.
_LEGACY_BTN_TASKS = {"📋 Vazifalar"}
BTN_TEAM = "👥 Ijrochilar"
BTN_RISKS = "🚨 Risklar"
BTN_NEW = "➕ Yangi"
BTN_STATS = "📊 Statistika"
BTN_SEARCH = "🔍 Qidiruv"
BTN_MEETINGS = "🤝 Uchrashuvlar"
BTN_SETTINGS = "⚙️ Sozlamalar"


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Persistent main menu — daily executive workflow."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_COCKPIT), KeyboardButton(text=BTN_TODAY)],
            [KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_REMINDERS)],
            [KeyboardButton(text=BTN_MEETINGS), KeyboardButton(text=BTN_NEW)],
            [KeyboardButton(text=BTN_SEARCH), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Matn yoki ovoz yuboring...",
    )


# ─────────────────────── SECTION REPLY KEYBOARDS ───────────────────────
# Bo'limga kirgan foydalanuvchi avtomatik o'sha bo'limning filter/amal
# tugmalarini reply kbd sifatida ko'radi. Har label noyob prefiks bilan
# (📌 = Vazifalar, 🤝 = Uchrashuvlar) — FSM state shart emas, label
# o'zi qaysi bo'limga tegishliligini bildiradi.

BTN_BACK_MAIN = "⬅️ Asosiy menyu"

# Section reply labels — prefiks yo'q (toza ko'rinish uchun).
# Kontekst (qaysi bo'lim) SectionFSM state orqali aniqlanadi.
TBTN_TASKS_ACTIVE = "Aktiv"
TBTN_TASKS_TODAY = "Bugun"
TBTN_TASKS_OVERDUE = "O'tgan"
TBTN_TASKS_IMPORTANT = "Muhim"
TBTN_TASKS_DONE = "Bajarilgan"
TBTN_TASKS_ALL = "Barchasi"
TBTN_TASKS_NEW = "➕ Yangi vazifa"
TBTN_TASKS_SEARCH = "🔍 Vazifa qidirish"

_TASKS_SECTION_FILTERS = {
    TBTN_TASKS_ACTIVE:   "active",
    TBTN_TASKS_TODAY:    "today",
    TBTN_TASKS_OVERDUE:  "overdue",
    TBTN_TASKS_IMPORTANT: "important",
    TBTN_TASKS_DONE:     "done",
    TBTN_TASKS_ALL:      "all",
}

MBTN_MEETINGS_WEEK = "Haftalik"
MBTN_MEETINGS_TODAY = "Bugun"
MBTN_MEETINGS_TOMORROW = "Ertaga"
MBTN_MEETINGS_ALL = "Barchasi"
MBTN_MEETINGS_PAST = "O'tgan"
MBTN_MEETINGS_NEW = "➕ Yangi uchrashuv"
MBTN_MEETINGS_SEARCH = "🔍 Uchrashuv qidirish"

_MEETINGS_SECTION_FILTERS = {
    MBTN_MEETINGS_WEEK:     "week",
    MBTN_MEETINGS_TODAY:    "today",
    MBTN_MEETINGS_TOMORROW: "tomorrow",
    MBTN_MEETINGS_ALL:      "all",
    MBTN_MEETINGS_PAST:     "past",
}

RBTN_REMINDERS_TODAY = "⏰ Bugun"
RBTN_REMINDERS_UPCOMING = "⏭ Keyingi"
RBTN_REMINDERS_SENT = "📤 Yuborilgan"
RBTN_REMINDERS_ALL = "🗂 Barchasi"
RBTN_REMINDERS_NEW = "➕ Yangi eslatma"
RBTN_REMINDERS_SEARCH = "🔍 Eslatma qidirish"

_REMINDERS_SECTION_FILTERS = {
    RBTN_REMINDERS_TODAY: "today",
    RBTN_REMINDERS_UPCOMING: "upcoming",
    RBTN_REMINDERS_SENT: "sent",
    RBTN_REMINDERS_ALL: "all",
}

# ── Qaydlar (Notes) section labels ──
NBTN_NOTES_INBOX = "📥 Inbox"
NBTN_NOTES_PROCESSED = "⚙️ Ishlangan"
NBTN_NOTES_ARCHIVED = "📦 Arxiv"
NBTN_NOTES_NEW = "➕ Yangi note"
NBTN_NOTES_SEARCH = "🔍 Note qidirish"

_NOTES_SECTION_FILTERS = {
    NBTN_NOTES_INBOX:     "inbox",
    NBTN_NOTES_PROCESSED: "processed",
    NBTN_NOTES_ARCHIVED:  "archived",
}

# Per-note source labels (short, used in card meta line)
_NOTES_SOURCE_BADGE = {
    "forward": "🔁 forward",
    "command": "⚡ buyruq",
    "voice":   "🎙 ovoz",
    "manual":  "✍️ qo'lda",
    "llm":     "🤖 LLM",
}
_NOTES_PER_PAGE = 10


class SectionFSM(StatesGroup):
    """Aktiv bo'limni eslab qolish — reply kbd labellari noyob emas,
    shuning uchun "Bugun"/"Barchasi"/"O'tgan" qaysi bo'limga tegishliligini
    state bilan aniqlanadi. State faqat shu maqsad uchun ishlatiladi —
    boshqa FSM oqimlar bilan to'qnashmaydi."""
    in_tasks = State()
    in_reminders = State()
    in_meetings = State()
    in_stats = State()
    in_team = State()
    in_risks = State()
    in_today = State()
    in_new = State()
    in_search = State()
    in_settings = State()
    in_notes = State()


class NoteCaptureFSM(StatesGroup):
    """One-shot FSM for `/qayd` with no body or "➕ Yangi note" button —
    next text/voice message becomes the note content."""
    awaiting_text = State()


# ── Statistika section labels ──
SBTN_STATS_TODAY = "Bugun"
SBTN_STATS_WEEK = "7 kun"
SBTN_STATS_MONTH = "30 kun"
SBTN_STATS_REPORT_WEEK = "📄 Hisobot 7 kun"
SBTN_STATS_REPORT_MONTH = "📄 Hisobot 30 kun"

_STATS_SECTION_PERIODS = {
    SBTN_STATS_TODAY: 1,
    SBTN_STATS_WEEK:  7,
    SBTN_STATS_MONTH: 30,
}

# ── Ijrochilar section labels ──
YBTN_TEAM_REFRESH = "🔄 Yangilash"
YBTN_TEAM_UNASSIGNED = "👤 Ijrochisiz vazifalar"
YBTN_TEAM_REASSIGN = "🔄 Qayta taqsimlash"

# ── Risklar section labels ──
RBTN_RISKS_REFRESH = "🔄 Yangilash"

# ── Bugun section labels ──
DBTN_TODAY_EVENING = "🌙 Kechki yakun"
DBTN_TODAY_ALL_TASKS = "📋 Hamma vazifalar"
DBTN_TODAY_NEW_TASK = "➕ Yangi vazifa (Bugun)"  # main "➕ Yangi" bilan to'qnashmasin
DBTN_TODAY_MEETINGS = "🤝 Bugungi uchrashuvlar"

# ── Yangi section labels ──
NBTN_NEW_TASK = "📝 Yangi vazifa"
NBTN_NEW_MEETING = "🤝 Yangi uchrashuv"
NBTN_NEW_REMINDER = "⏰ Yangi eslatma"
NBTN_NEW_NOTE = "📥 Yangi note"
NBTN_NEW_VOICE = "🎙 Ovozli vazifa"
NBTN_NEW_POLISH = "✏️ Matn tahrirlash"

# ── Qidiruv section labels ──
QBTN_SEARCH_TASKS = "📌 Faqat vazifalar"
QBTN_SEARCH_MEETINGS = "🤝 Faqat uchrashuvlar"
QBTN_SEARCH_CONTACTS = "👥 Faqat kontaktlar"
QBTN_SEARCH_ALL = "🗂 Hammasi"

# ── Sozlamalar section labels ──
GBTN_SETTINGS_NOTIFY = "🔔 Bildirishnoma"
GBTN_SETTINGS_BRIEFING = "⏰ Brifing vaqti"
GBTN_SETTINGS_EVENING = "🌙 Kechki vaqt"  # "yakun" is the action in Today section; here we set the time
GBTN_SETTINGS_REMINDER = "📲 Eslatma parametrlari"
GBTN_SETTINGS_VOICE = "🎙 Ovoz tasdig'i"
GBTN_SETTINGS_CREATE_CONFIRM = "✅ Yaratish tasdig'i"
GBTN_SETTINGS_CALENDAR = "📅 Kalendar holati"


def tasks_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Vazifalar bo'limida — filterlar va sub-amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            # Barchasi — alohida, eng tepada (to'liq kenglik).
            # Keyin fokus filtrlari, so'ng arxiv/ko'rib chiqish.
            [KeyboardButton(text=TBTN_TASKS_ALL)],
            [KeyboardButton(text=TBTN_TASKS_ACTIVE),
             KeyboardButton(text=TBTN_TASKS_TODAY),
             KeyboardButton(text=TBTN_TASKS_IMPORTANT)],
            [KeyboardButton(text=TBTN_TASKS_OVERDUE),
             KeyboardButton(text=TBTN_TASKS_DONE)],
            [KeyboardButton(text=TBTN_TASKS_NEW),
             KeyboardButton(text=TBTN_TASKS_SEARCH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Filter tanlang yoki yangi vazifa...",
    )


def meetings_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Uchrashuvlar bo'limida — filterlar va sub-amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=MBTN_MEETINGS_WEEK),
             KeyboardButton(text=MBTN_MEETINGS_TODAY),
             KeyboardButton(text=MBTN_MEETINGS_TOMORROW)],
            [KeyboardButton(text=MBTN_MEETINGS_ALL),
             KeyboardButton(text=MBTN_MEETINGS_PAST)],
            [KeyboardButton(text=MBTN_MEETINGS_NEW),
             KeyboardButton(text=MBTN_MEETINGS_SEARCH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Filter tanlang yoki yangi uchrashuv...",
    )


def reminders_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Eslatmalar bo'limida — filterlar va asosiy amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=RBTN_REMINDERS_TODAY),
             KeyboardButton(text=RBTN_REMINDERS_UPCOMING)],
            [KeyboardButton(text=RBTN_REMINDERS_SENT),
             KeyboardButton(text=RBTN_REMINDERS_ALL)],
            [KeyboardButton(text=RBTN_REMINDERS_NEW),
             KeyboardButton(text=RBTN_REMINDERS_SEARCH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Eslatma tanlang yoki yangi eslatma...",
    )


def notes_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Notes bo'limida — Inbox / Ishlangan / Arxiv + amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NBTN_NOTES_INBOX),
             KeyboardButton(text=NBTN_NOTES_PROCESSED),
             KeyboardButton(text=NBTN_NOTES_ARCHIVED)],
            [KeyboardButton(text=NBTN_NOTES_NEW),
             KeyboardButton(text=NBTN_NOTES_SEARCH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Note tanlang yoki yangisini yozing...",
    )


def stats_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Statistika bo'limida — davrlar va hisobotlar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SBTN_STATS_TODAY),
             KeyboardButton(text=SBTN_STATS_WEEK),
             KeyboardButton(text=SBTN_STATS_MONTH)],
            [KeyboardButton(text=SBTN_STATS_REPORT_WEEK),
             KeyboardButton(text=SBTN_STATS_REPORT_MONTH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Davr tanlang yoki hisobot...",
    )


def team_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Ijrochilar bo'limida — tezkor amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=YBTN_TEAM_UNASSIGNED)],
            [KeyboardButton(text=YBTN_TEAM_REFRESH),
             KeyboardButton(text=YBTN_TEAM_REASSIGN)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Ijrochi raqamini yoki amal tanlang...",
    )


def risks_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Risklar bo'limida — minimal."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=RBTN_RISKS_REFRESH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Risk raqamini bosing yoki yangilang...",
    )


def today_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Bugun bo'limida — tezkor amallar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=DBTN_TODAY_EVENING),
             KeyboardButton(text=DBTN_TODAY_ALL_TASKS)],
            [KeyboardButton(text=DBTN_TODAY_NEW_TASK),
             KeyboardButton(text=DBTN_TODAY_MEETINGS)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Vazifa raqamini yoki amal tanlang...",
    )


def new_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Yangi bo'limida — turlar."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=NBTN_NEW_TASK),
             KeyboardButton(text=NBTN_NEW_MEETING)],
            [KeyboardButton(text=NBTN_NEW_REMINDER),
             KeyboardButton(text=NBTN_NEW_NOTE)],
            [KeyboardButton(text=NBTN_NEW_VOICE),
             KeyboardButton(text=NBTN_NEW_POLISH)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Yangi item turini tanlang...",
    )


def search_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Qidiruv bo'limida — scope filterlari."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=QBTN_SEARCH_TASKS),
             KeyboardButton(text=QBTN_SEARCH_MEETINGS)],
            [KeyboardButton(text=QBTN_SEARCH_CONTACTS),
             KeyboardButton(text=QBTN_SEARCH_ALL)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Scope tanlang yoki so'z yuboring...",
    )


def settings_section_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply kbd Sozlamalar bo'limida — parametr toifalari."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=GBTN_SETTINGS_NOTIFY),
             KeyboardButton(text=GBTN_SETTINGS_BRIEFING)],
            [KeyboardButton(text=GBTN_SETTINGS_EVENING),
             KeyboardButton(text=GBTN_SETTINGS_REMINDER)],
            [KeyboardButton(text=GBTN_SETTINGS_VOICE),
             KeyboardButton(text=GBTN_SETTINGS_CREATE_CONFIRM)],
            [KeyboardButton(text=GBTN_SETTINGS_CALENDAR)],
            [KeyboardButton(text=BTN_BACK_MAIN)],
        ],
        resize_keyboard=True, is_persistent=True,
        input_field_placeholder="Sozlama tanlang...",
    )


_SECTION_LABELS: set[str] = (
    set(_TASKS_SECTION_FILTERS)
    | set(_REMINDERS_SECTION_FILTERS)
    | set(_MEETINGS_SECTION_FILTERS)
    | set(_STATS_SECTION_PERIODS)
    | {TBTN_TASKS_NEW, TBTN_TASKS_SEARCH,
       MBTN_MEETINGS_NEW, MBTN_MEETINGS_SEARCH,
       RBTN_REMINDERS_NEW, RBTN_REMINDERS_SEARCH,
       SBTN_STATS_REPORT_WEEK, SBTN_STATS_REPORT_MONTH,
       YBTN_TEAM_REFRESH, YBTN_TEAM_UNASSIGNED, YBTN_TEAM_REASSIGN,
       RBTN_RISKS_REFRESH,
       DBTN_TODAY_EVENING, DBTN_TODAY_ALL_TASKS, DBTN_TODAY_NEW_TASK, DBTN_TODAY_MEETINGS,
       NBTN_NEW_TASK, NBTN_NEW_MEETING, NBTN_NEW_REMINDER, NBTN_NEW_VOICE, NBTN_NEW_POLISH,
       QBTN_SEARCH_TASKS, QBTN_SEARCH_MEETINGS, QBTN_SEARCH_CONTACTS, QBTN_SEARCH_ALL,
       GBTN_SETTINGS_NOTIFY, GBTN_SETTINGS_BRIEFING, GBTN_SETTINGS_EVENING,
       GBTN_SETTINGS_REMINDER, GBTN_SETTINGS_VOICE, GBTN_SETTINGS_CREATE_CONFIRM,
       GBTN_SETTINGS_CALENDAR,
       BTN_BACK_MAIN}
)


# Bot restart'dan keyin section labellarni state'ga avtomatik tiklash
# (E1 edge case). Foydalanuvchi cache'dagi section reply kbd dan tugma
# bossa, mos state'ga o'rnatamiz va to'g'ri handler ishlay oladi.

def back_button(callback_data: str = "nav_cockpit", text: str = "⬅️ Orqaga") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def single_back_keyboard(callback_data: str = "nav_cockpit", text: str = "⬅️ Orqaga") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[back_button(callback_data, text)]])


def task_inline_actions(task: dict) -> InlineKeyboardMarkup:
    """Per-task quick action row: 3 most-used buttons.

    [👤 Ijrochi] [✅ Bajarildi] [⋯ Batafsil]

    Batafsil opens task_detail_menu with the full action set.
    """
    tid = task["id"]
    if task.get("status") == "done":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↺ Qaytarish", callback_data=f"reopen:{tid}"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"task_del:{tid}"),
            InlineKeyboardButton(text="⋯ Batafsil", callback_data=f"task_detail:{tid}"),
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👤 Ijrochi", callback_data=f"set_assignee:{tid}"),
        InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"complete:{tid}"),
        InlineKeyboardButton(text="⋯ Batafsil", callback_data=f"task_detail:{tid}"),
    ]])


def task_detail_menu(task: dict) -> InlineKeyboardMarkup:
    """Full action menu — compact 2-column layout from ⋯ Batafsil."""
    tid = task["id"]
    if task.get("status") == "done":
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="↺ Qaytarish", callback_data=f"reopen:{tid}"),
                InlineKeyboardButton(text="✏️ Tahrir", callback_data=f"edit:{tid}"),
            ],
            [
                InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"task_del:{tid}"),
                InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"taskopen:{tid}"),
            ],
        ])
    # Quick lifecycle actions only. Field edits (Muddat, Prioritet, Status, …)
    # all live in ✏️ Tahrir → task_edit_menu, so we don't duplicate them here.
    # 👤 Ijrochi stays (it has no field-editor equivalent and is a frequent action).
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"complete:{tid}"),
            InlineKeyboardButton(text="👤 Ijrochi", callback_data=f"set_assignee:{tid}"),
        ],
        [
            InlineKeyboardButton(text="✏️ Tahrir", callback_data=f"edit:{tid}"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"task_del:{tid}"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"taskopen:{tid}"),
        ],
    ])


class TaskEditFSM(StatesGroup):
    awaiting_value = State()


class AssigneeFSM(StatesGroup):
    awaiting_name = State()


class TaskSearchFSM(StatesGroup):
    awaiting_query = State()


class MeetingSearchFSM(StatesGroup):
    awaiting_query = State()


class MeetingFollowupFSM(StatesGroup):
    awaiting_notes = State()


class MeetingEditFSM(StatesGroup):
    awaiting_value = State()


class MeetingProtocolFSM(StatesGroup):
    awaiting_notes = State()
    awaiting_revision = State()


class VoiceConfirmFSM(StatesGroup):
    """Free-form ovoz xabari konfirmatsiyasi. Foydalanuvchi ovoz yuboradi →
    bot transkripsiyani ko'rsatadi → foydalanuvchi tasdiqlaydi/tahrirlaydi/bekor.
    Faqat FSM holatisiz holatlarda faollashadi (boshqa flow ichida emas)."""
    awaiting_action = State()
    awaiting_revision = State()


class PolishRevisionFSM(StatesGroup):
    """✎ Yana tahrir — polished matnni qayta ishlash. Foydalanuvchi ko'rsatma
    yuboradi (masalan «qisqartir»), bot original + ko'rsatmani qayta polish qiladi."""
    awaiting = State()


class CreateActionConfirmFSM(StatesGroup):
    """Tasdiq state — yangi vazifa/uchrashuv yaratishdan oldin foydalanuvchi
    "Tasdiqlayman/Bekor qilish" bossin uchun. Claude'ning to'liq javobi
    state.data['pending_response']'da saqlanadi; tasdiqdan keyin bajariladi."""
    awaiting = State()


class GlobalSearchFSM(StatesGroup):
    """Global search across tasks + meetings."""
    awaiting_query = State()


class NewTaskFSM(StatesGroup):
    """Step-by-step guided form for creating a task."""
    awaiting_title = State()
    awaiting_priority = State()
    awaiting_deadline = State()           # step 1: pick a day
    awaiting_deadline_time = State()      # step 2: pick a time for the chosen day
    awaiting_deadline_manual = State()    # power path: type a full date+time
    awaiting_assignee = State()
    awaiting_confirm = State()


class NewReminderFSM(StatesGroup):
    """Step-by-step guided form for creating a standalone reminder."""
    awaiting_title = State()
    awaiting_time = State()
    awaiting_time_manual = State()
    awaiting_repeat = State()
    awaiting_confirm = State()


class ReminderSearchFSM(StatesGroup):
    awaiting_query = State()


class ReminderEditFSM(StatesGroup):
    awaiting_value = State()


def task_edit_menu(task: dict) -> InlineKeyboardMarkup:
    """Menu of editable fields for a task — used by 📝 Tahrir flow."""
    tid = task["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Sarlavha", callback_data=f"editfield:{tid}:title")],
        [InlineKeyboardButton(text="📄 Tavsif", callback_data=f"editfield:{tid}:description")],
        [InlineKeyboardButton(text="⚡ Prioritet", callback_data=f"editfield:{tid}:priority")],
        [InlineKeyboardButton(text="📅 Deadline", callback_data=f"editfield:{tid}:deadline")],
        [InlineKeyboardButton(text="📊 Status", callback_data=f"editfield:{tid}:status")],
        [InlineKeyboardButton(text="🏷 Teglar", callback_data=f"editfield:{tid}:tags")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"taskopen:{tid}")],
    ])


def priority_picker(task_id: str) -> InlineKeyboardMarkup:
    # Uzbek labels — NewTaskFSM bilan bir xil; raw P0/P1/P2/P3 kodlari foydalanuvchiga ko'rinmasin.
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 Shoshilinch", callback_data=f"setfield:{task_id}:priority:P0"),
            InlineKeyboardButton(text="🟠 Muhim",      callback_data=f"setfield:{task_id}:priority:P1"),
        ],
        [
            InlineKeyboardButton(text="🔵 Rejadagi",   callback_data=f"setfield:{task_id}:priority:P2"),
            InlineKeyboardButton(text="⚪ Oddiy",      callback_data=f"setfield:{task_id}:priority:P3"),
        ],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"edit:{task_id}")],
    ])


def status_picker(task_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏳ Bajariladi", callback_data=f"setfield:{task_id}:status:todo"),
            InlineKeyboardButton(text="🔄 Jarayonda", callback_data=f"setfield:{task_id}:status:in_progress"),
        ],
        [
            InlineKeyboardButton(text="⚠️ To'silgan", callback_data=f"setfield:{task_id}:status:blocked"),
            InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"setfield:{task_id}:status:done"),
        ],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"edit:{task_id}")],
    ])


def deadline_picker(task_id: str) -> InlineKeyboardMarkup:
    """Quick deadline presets + manual entry."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Bugun 17:00", callback_data=f"deadline_preset:{task_id}:today"),
            InlineKeyboardButton(text="📅 Ertaga 09:00", callback_data=f"deadline_preset:{task_id}:tomorrow"),
        ],
        [
            InlineKeyboardButton(text="📅 +3 kun", callback_data=f"deadline_preset:{task_id}:plus3"),
            InlineKeyboardButton(text="📅 Hafta oxiri", callback_data=f"deadline_preset:{task_id}:weekend"),
        ],
        [
            InlineKeyboardButton(text="✏️ Qo'lda kiritish", callback_data=f"deadline_manual:{task_id}"),
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"setfield:{task_id}:deadline:none"),
        ],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"edit:{task_id}")],
    ])


def meeting_inline_actions(meeting: dict) -> InlineKeyboardMarkup:
    """Drill-down actions for a single meeting — STATE-AWARE.

    Before the meeting happens (not completed): ✅ Bo'ldi · reschedule · edit ·
    cancel. NO "Bayonnoma yaratish" — minutes only make sense after the meeting,
    and a cancelled meeting needs none.
    After ✅ Bo'ldi (completed): 📝 Bayonnoma yaratish · ↺ undo · ⬅️ Ro'yxatga.
    """
    mid = meeting["id"]
    if meeting.get("completed_at"):
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Bayonnoma yaratish", callback_data=f"protocol:{mid}")],
            [InlineKeyboardButton(text="↺ Bo'ldi'ni bekor qilish", callback_data=f"meeting_undone:{mid}")],
            [back_button("meetingfilter:week", "⬅️ Ro'yxatga")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Bo'ldi", callback_data=f"meeting_done:{mid}")],
        [InlineKeyboardButton(text="🔄 Vaqtni o'zgartirish", callback_data=f"reschedule:{mid}")],
        [
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"meeting_edit:{mid}"),
            InlineKeyboardButton(text="✕ Bekor qilish", callback_data=f"meeting_cancel:{mid}"),
        ],
        [back_button("meetingfilter:week")],
    ])


def settings_keyboard(settings: dict) -> InlineKeyboardMarkup:
    """Settings menu — actions + Back."""
    notif_on = settings.get("notifications_enabled", True)
    notif_label = "🔔 Bildirishnomalar: YOQ" if notif_on else "🔕 Bildirishnomalar: O'CHIQ"
    morning_time = settings.get("morning_briefing_time", "08:00")
    evening_time = settings.get("evening_summary_time", "18:00")
    quiet_on = settings.get("quiet_hours_enabled", False)
    qh_start = settings.get("quiet_hours_start", "22:00")
    qh_end = settings.get("quiet_hours_end", "07:00")
    quiet_label = (f"🌙 Sukunat: {qh_start}–{qh_end}" if quiet_on
                    else "🌙 Sukunat: o'chiq")
    voice_auto = settings.get("voice_auto_confirm", True)
    voice_label = ("🎙 Ovoz: AVTO (tasdiqsiz)" if voice_auto
                    else "🎙 Ovoz: tasdiq so'rash")
    confirm_create = settings.get("confirm_create_actions", True)
    create_label = ("⚠️ Yaratish tasdig'i: YOQ" if confirm_create
                     else "⚠️ Yaratish tasdig'i: O'CHIQ")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=notif_label, callback_data="setting:notifications_toggle")],
        [InlineKeyboardButton(text=f"⏰ Brifing vaqti: {morning_time}", callback_data="setting:briefing_time")],
        [InlineKeyboardButton(text=f"🌙 Kechki yakun: {evening_time}", callback_data="setting:evening_time")],
        [InlineKeyboardButton(text=quiet_label, callback_data="setting:quiet_hours")],
        [InlineKeyboardButton(text=voice_label, callback_data="setting:voice_auto_toggle")],
        [InlineKeyboardButton(text=create_label, callback_data="setting:confirm_create_toggle")],
        [InlineKeyboardButton(text="📲 Eslatma parametrlari", callback_data="setting:reminders")],
        [InlineKeyboardButton(text="📅 Kalendar holati", callback_data="setting:calendar")],
        [back_button()],
    ])


async def _keep_typing(bot: Bot, chat_id: int) -> None:
    """Persistent typing indicator. Telegram's indicator lasts ~5s, so we refresh every 4s."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        return
    except Exception as e:
        # Network blip, bot blocked, chat deleted — don't crash the caller, but
        # leave a breadcrumb so silent typing-failures are diagnosable.
        logger.debug("_keep_typing stopped on chat %s: %s", chat_id, e)

logger = logging.getLogger(__name__)
router = Router()


# Strong refs to fire-and-forget tasks. Without these, Python's GC can collect
# the task object mid-flight (PEP 3156 only holds weak refs), causing the
# coroutine to be silently cancelled and any exception swallowed.
_background_tasks: set[asyncio.Task] = set()


def _log_background_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background task %r failed: %s", task.get_name(), exc, exc_info=exc)


def _spawn_background(coro, *, name: str) -> asyncio.Task:
    """Schedule a fire-and-forget coroutine while keeping a strong reference and
    logging any unhandled exception. Use instead of bare asyncio.create_task()
    for work that runs past the current handler's response."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    task.add_done_callback(_log_background_exception)
    return task


def _cb_int(data: str | None, *, default: int = 0) -> int:
    """Extract an int from callback_data of the form 'prefix:N'. Returns
    `default` on any parse failure (missing data, no colon, non-numeric tail)
    so a malformed or stale callback can't crash the handler with
    IndexError/ValueError. The auth middleware blocks non-principal users
    so this is mainly defence against stale buttons from older bot versions."""
    if not data:
        return default
    parts = data.split(":", 1)
    if len(parts) < 2:
        return default
    try:
        return int(parts[1])
    except (TypeError, ValueError):
        return default


def _cb_part(data: str | None, index: int, *, default: str = "") -> str:
    """Safe positional access into a colon-separated callback_data string.
    `_cb_part('a:b:c', 2)` → 'c'; `_cb_part('a:b', 2)` → ''. Use in place of
    `query.data.split(':')[index]` when the tail is optional."""
    if not data:
        return default
    parts = data.split(":")
    if 0 <= index < len(parts):
        return parts[index]
    return default


async def _get_text_or_transcribe(message: Message, bot: Bot | None = None) -> str | None:
    """Universal text extractor for state handlers that accept F.text | F.voice.
    Returns the message text (for text messages) OR the STT transcript (for
    voice). Returns None if the voice download/transcribe failed — caller
    should respond with a retry prompt in that case.

    This lets every FSM state accept voice without each handler re-implementing
    download + transcribe + error messaging."""
    if message.text:
        return message.text
    if message.voice and (bot or message.bot):
        b = bot or message.bot
        if message.voice.file_size and message.voice.file_size > voice_service.MAX_AUDIO_BYTES:
            await message.answer(
                f"Ovoz xabari juda katta ({message.voice.file_size // 1024} KB). "
                f"Iltimos, {voice_service.MAX_AUDIO_BYTES // (1024 * 1024)} MB dan kichikroq yuboring."
            )
            return None
        try:
            file = await b.get_file(message.voice.file_id)
            audio_io = await b.download_file(file.file_path)
            if hasattr(audio_io, "getvalue"):
                audio_bytes = audio_io.getvalue()
            elif hasattr(audio_io, "read"):
                audio_bytes = audio_io.read()
            else:
                audio_bytes = bytes(audio_io)
        except Exception:
            logger.exception("Voice download failed")
            await message.answer("Ovozni yuklab ololmadim. Iltimos, qaytadan yuboring.")
            return None
        transcript = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not transcript:
            await message.answer("Ovozni o'qiy olmadim. Matn yozib yuboring yoki qaytadan urinib ko'ring.")
            return None
        # Patch message.text in-place so handler bodies using `message.text`
        # transparently see the transcript. Pydantic v2 forbids regular
        # assignment on Message; object.__setattr__ bypasses the descriptor.
        try:
            object.__setattr__(message, "text", transcript)
        except Exception:
            logger.debug("Could not patch message.text in-place; caller must use return value")
        return transcript
    return None


# ─────────────────────── AUTH MIDDLEWARE ───────────────────────


def _is_principal(user_id: int | None) -> bool:
    return user_id == config.PRINCIPAL_USER_ID


def _is_private_chat(chat) -> bool:
    """True if chat is a 1:1 private chat. This bot is designed for the
    principal in private — group/channel/supergroup behavior is undefined
    (formatting, FSM state, mentions all break)."""
    return chat is not None and chat.type == "private"


async def _reject(message_or_query, reason: str = "Bu shaxsiy bot."):
    if isinstance(message_or_query, Message):
        await message_or_query.answer(reason)
    elif isinstance(message_or_query, CallbackQuery):
        await message_or_query.answer(reason, show_alert=True)


_FSM_STATE_TTL_SECONDS = 30 * 60  # 30 minutes


@router.message.middleware()
async def auth_message_middleware(handler, event: Message, data):
    if not _is_principal(event.from_user.id if event.from_user else None):
        await _reject(event)
        return
    # Reject group/channel use — even from the principal. Forwarding the bot
    # to a group is the most common accident; this keeps state and formatting
    # behaviour predictable. The principal can DM directly.
    if not _is_private_chat(event.chat):
        await _reject(event, "Bu bot faqat shaxsiy chat'da ishlaydi. Iltimos, men bilan to'g'ridan-to'g'ri yozing.")
        return
    # FSM state TTL: if the user started a flow (say, NewTaskFSM.awaiting_title),
    # walked away for >30 min, and comes back with a random "Salom", we'd
    # store "Salom" as the task title. Detect stale state via timestamp in
    # state.data and auto-clear with a friendly notice.
    state = data.get("state")
    if state is not None:
        try:
            current = await state.get_state()
            if current and current != "default_state":
                sdata = await state.get_data()
                started_at = sdata.get("_fsm_started_at")
                now_ts = datetime.now(database.TZ).timestamp()
                if started_at and (now_ts - float(started_at)) > _FSM_STATE_TTL_SECONDS:
                    await state.clear()
                    await event.answer(
                        "⏱ Avvalgi amaliyot 30 daqiqadan ortiq vaqt o'tgani uchun bekor qilindi. "
                        "Yangi xabar yuboring yoki /start bosing.",
                    )
                    return
                # Refresh the timestamp on each interaction so an active flow
                # never times out mid-conversation.
                await state.update_data(_fsm_started_at=now_ts)
        except Exception:
            logger.debug("FSM TTL check failed (non-fatal)", exc_info=True)
    return await handler(event, data)


@router.callback_query.middleware()
async def auth_callback_middleware(handler, event: CallbackQuery, data):
    if not _is_principal(event.from_user.id if event.from_user else None):
        await _reject(event)
        return
    if event.message and not _is_private_chat(event.message.chat):
        await _reject(event, "Faqat shaxsiy chat'da ishlaydi.")
        return
    return await handler(event, data)


# Inline-query auth happens INSIDE the handler (handle_inline_query) — we return
# empty results to non-principal users without alerting them. Telegram's inline UI
# doesn't tolerate the same kind of rejection messages we use elsewhere.


# ─────────────────────── ACTION EXECUTOR ───────────────────────


async def _push_meeting_to_icloud(meeting_id: str, data: dict) -> None:
    """Background-task push: doesn't block the bot's reply to the user."""
    try:
        start_dt = datetime.fromisoformat(data["datetime_start"])
        end_iso = data.get("datetime_end") or (start_dt + timedelta(hours=1)).isoformat()
        end_dt = datetime.fromisoformat(end_iso)
        uid = await asyncio.to_thread(
            calendar_service.push_meeting,
            meeting_id, data.get("title", "Uchrashuv"), start_dt, end_dt,
            data.get("participants"), data.get("location_or_link"),
            data.get("agenda"),
        )
        if uid:
            import aiosqlite
            async with aiosqlite.connect(config.DATABASE_PATH) as db:
                await db.execute("UPDATE meetings SET icloud_uid = ? WHERE id = ?", (uid, meeting_id))
                await db.commit()
            logger.info("iCloud sync complete for %s", meeting_id)
        else:
            logger.warning("iCloud push returned no UID for %s", meeting_id)
    except Exception:
        logger.exception("Background iCloud push failed for %s", meeting_id)


_SELF_ASSIGNEE_NAMES = {"men", "o'zim", "ozim", "o'z", "oz", ""}


async def _upsert_contacts(names: list[str]) -> int:
    """Avtomatik tarzda kontaktlar jadvaliga ismlarni qo'shadi.
    Mavjud bo'lganlar (case-insensitive) o'tkazib yuboriladi. 'men/o'zim' kabi
    self-reference ismlar saqlanmaydi. Yangi yaratilganlar sonini qaytaradi.
    """
    clean = []
    for raw in names:
        name = (raw or "").strip()
        if not name or name.lower() in _SELF_ASSIGNEE_NAMES:
            continue
        clean.append(name)
    if not clean:
        return 0
    try:
        existing = await database.list_contacts()
        existing_names = {c["name"].lower().strip() for c in existing}
    except Exception:
        logger.warning("Auto-contact: failed to load existing contacts")
        return 0
    created = 0
    for name in clean:
        if name.lower() in existing_names:
            continue
        try:
            await database.save_contact({
                "name": name,
                "role": None,
                "formality_level": 3,
            })
            existing_names.add(name.lower())
            created += 1
        except Exception:
            logger.warning("Auto-contact upsert failed for %s", name)
    return created


async def _execute_actions(actions: list[dict]) -> dict[str, list[str]]:
    """Execute Claude-returned actions. Return map of type → list of affected IDs.
    Side effect: assignees on create_task/update_task and participants on
    schedule_meeting are auto-upserted into the contacts table.
    """
    created_ids: dict[str, list[str]] = {
        "task": [], "reminder": [], "meeting": [], "contact": [], "correction": [],
        "note": [], "_failed": [], "_refresh": [],
    }

    for action in actions:
        atype = action.get("type", "")
        data = action.get("data", {})
        target_id = action.get("id")

        try:
            if atype == "create_task":
                tid = await database.create_task(data)
                created_ids["task"].append(tid)
                await _upsert_contacts([data.get("assignee") or ""])
            elif atype == "create_reminder":
                rid = await database.create_reminder(data)
                created_ids["reminder"].append(rid)
            elif atype == "update_task" and target_id:
                await database.update_task(target_id, data)
                created_ids["task"].append(target_id)
                if data.get("assignee"):
                    await _upsert_contacts([data.get("assignee") or ""])
            elif atype == "complete_task" and target_id:
                await database.complete_task(target_id)
                created_ids["task"].append(target_id)
            elif atype == "delete_task" and target_id:
                await database.delete_task(target_id)
            elif atype == "schedule_meeting":
                mid = await database.create_meeting(data)
                created_ids["meeting"].append(mid)
                sched = scheduler_module.get_scheduler()
                if sched and data.get("datetime_start"):
                    sched.schedule_meeting_reminder(mid, data["datetime_start"])
                # Push to iCloud as a FIRE-AND-FORGET background task — the user gets their
                # bot reply within ~1 second; iCloud sync happens in parallel (typically 1-3s).
                if config.ICLOUD_ENABLED and data.get("datetime_start"):
                    _spawn_background(_push_meeting_to_icloud(mid, data), name=f"icloud_push:{mid}")
                await _upsert_contacts(list(data.get("participants") or []))
            elif atype == "cancel_meeting" and target_id:
                await database.cancel_meeting(target_id)
                sched = scheduler_module.get_scheduler()
                if sched:
                    try:
                        sched.remove_meeting_reminder(target_id)
                    except Exception:
                        logger.exception("Failed to remove meeting reminder for %s", target_id)
                if config.ICLOUD_ENABLED:
                    try:
                        await asyncio.to_thread(calendar_service.delete_meeting, target_id)
                    except Exception:
                        logger.exception("iCloud delete failed (non-fatal)")
            elif atype == "save_contact":
                cid = await database.save_contact(data)
                if cid:
                    created_ids["contact"].append(cid)
            elif atype == "save_correction":
                corr_id = await database.save_correction(data)
                created_ids["correction"].append(corr_id)
            elif atype == "create_note":
                # Quick-capture inbox. Source defaults to "llm" — Claude
                # decided this was a note rather than a task/reminder.
                if data.get("content"):
                    nid = await database.create_note({
                        "content": data.get("content"),
                        "title": data.get("title"),
                        "tags": data.get("tags") or [],
                        "source": data.get("source") or "llm",
                    })
                    if nid:
                        created_ids["note"].append(nid)
            elif atype == "delete_all_tasks":
                n = await database.delete_all_tasks(data.get("status_in"))
                created_ids["_refresh"].append("task")
                logger.info("Bulk delete: %d tasks (status_in=%s)", n, data.get("status_in"))
            elif atype == "delete_all_meetings":
                n = await database.delete_all_meetings()
                created_ids["_refresh"].append("meeting")
                logger.info("Bulk delete: %d meetings", n)
            elif atype == "delete_all_notes":
                n = await database.delete_all_notes()
                created_ids["_refresh"].append("note")
                logger.info("Bulk delete: %d notes", n)
            elif atype == "delete_all_reminders":
                n = await database.delete_all_reminders()
                created_ids["_refresh"].append("reminder")
                logger.info("Bulk delete: %d reminders", n)
            elif atype == "delete_all_contacts":
                n = await database.delete_all_contacts()
                logger.info("Bulk delete: %d contacts", n)
            elif atype == "none":
                pass
            else:
                logger.warning("Unknown action type: %s", atype)
        except Exception:
            logger.exception("Failed to execute action %s", action)
            created_ids["_failed"].append(atype or "unknown")

    return created_ids


# Action type → user-facing noun for the "saqlanmadi" warning.
_ACTION_NOUN_UZ = {
    "create_task": "vazifa", "update_task": "vazifa yangilash",
    "complete_task": "vazifa yakunlash", "create_reminder": "eslatma",
    "schedule_meeting": "uchrashuv", "cancel_meeting": "uchrashuvni bekor qilish",
    "create_note": "note", "save_contact": "kontakt",
}


def _failed_actions_note(ids_by_type: dict[str, list[str]]) -> str:
    """If any action failed inside _execute_actions, return a short warning to
    append to the reply — so a silent DB error never looks like success."""
    failed = ids_by_type.get("_failed") if ids_by_type else None
    if not failed:
        return ""
    nouns = ", ".join(sorted({_ACTION_NOUN_UZ.get(a, a) for a in failed}))
    return (f"\n\n⚠️ **Saqlanmadi:** {nouns}. Texnik xato yuz berdi — "
            f"qaytadan urinib ko'ring yoki /diagnostics.")


# ─────────────────────── KEYBOARD BUILDER ───────────────────────


_TEMP_TOKENS = {
    "t-new": ("task", 0),
    "t-latest": ("task", -1),
    "m-new": ("meeting", 0),
    "m-latest": ("meeting", -1),
    "r-new": ("reminder", 0),
    "r-latest": ("reminder", -1),
    "c-new": ("contact", 0),
    "polish": (None, None),
}


def _resolve_callback(callback: str, ids_by_type: dict[str, list[str]]) -> str | None:
    """Replace placeholder tokens in callback data with real IDs from executed actions.

    Returns None if the callback references a placeholder that has no real ID (drops the button).
    """
    for placeholder, (entity_type, idx) in _TEMP_TOKENS.items():
        if placeholder in callback:
            if entity_type is None:
                return callback
            ids = ids_by_type.get(entity_type, [])
            if not ids:
                return None
            real_id = ids[idx] if idx == -1 else ids[0]
            return callback.replace(placeholder, real_id)
    return callback


def _build_keyboard(buttons: list, ids_by_type: dict[str, list[str]]) -> InlineKeyboardMarkup | None:
    """Convert Claude's button structure to aiogram InlineKeyboardMarkup."""
    if not buttons:
        return None

    rows: list[list[InlineKeyboardButton]] = []

    if isinstance(buttons, list) and buttons and isinstance(buttons[0], dict):
        buttons = [buttons]

    for row in buttons:
        kb_row: list[InlineKeyboardButton] = []
        for btn in row:
            label = btn.get("label", "")
            callback = btn.get("callback", "")
            if not label or not callback:
                continue
            resolved = _resolve_callback(callback, ids_by_type)
            if resolved is None:
                continue
            if len(resolved) > 64:
                resolved = resolved[:64]
            kb_row.append(InlineKeyboardButton(text=label, callback_data=resolved))
        if kb_row:
            rows.append(kb_row)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _append_back_row(kb: InlineKeyboardMarkup | None,
                     callback_data: str = "nav_cockpit") -> InlineKeyboardMarkup:
    rows = [list(row) for row in (kb.inline_keyboard if kb else [])]
    if not rows or all(btn.callback_data != callback_data for row in rows for btn in row):
        rows.append([back_button(callback_data)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ─────────────────────── CORE PROCESSING ───────────────────────


_STREAM_EDIT_MIN_INTERVAL_SEC = 1.2  # Telegram message-edit rate limit headroom
_STREAM_EDIT_MIN_DELTA_CHARS = 24    # don't spam edits for tiny additions


_DESTRUCTIVE_ACTION_TYPES = {"create_task", "schedule_meeting"}

# Mass-delete actions ("barchasini o'chir"). These ALWAYS require confirmation —
# independent of the confirm_create_actions setting — because they're
# irreversible and a mis-heard voice command could wipe everything.
_BULK_DELETE_ACTION_TYPES = {
    "delete_all_tasks", "delete_all_meetings", "delete_all_notes",
    "delete_all_reminders", "delete_all_contacts",
}
_BULK_DELETE_LABEL = {
    "delete_all_tasks": ("vazifa", "tasks"),
    "delete_all_meetings": ("uchrashuv", "meetings"),
    "delete_all_notes": ("note", "notes"),
    "delete_all_reminders": ("eslatma", "reminders"),
    "delete_all_contacts": ("kontakt", "contacts"),
}

# Single delete/cancel via voice/text. ALWAYS confirm (a mis-heard "X'ni o'chir"
# shouldn't silently delete) — independent of the confirm_create_actions setting.
_SINGLE_DELETE_ACTION_TYPES = {"delete_task", "cancel_meeting"}


async def _maybe_refresh_section(
    message: Message,
    state: "FSMContext | None",
    ids_by_type: dict[str, list[str]],
) -> None:
    """When the user is currently inside a section view (`/tasks`, `/meetings`,
    `/reminders`, `/today`) and an item of the matching type was just created,
    send a fresh section render so the new item is visible immediately.

    Without this, the previously-rendered section list (an old Telegram
    message) shows stale data and the user thinks the create silently
    failed — a real reported bug."""
    if state is None or not ids_by_type:
        return
    try:
        current = await state.get_state()
    except Exception:
        return
    if not current:
        return

    # `_refresh` lets non-create actions (e.g. bulk deletes) ask for a section
    # re-render even though they produce no new item IDs.
    refresh = set(ids_by_type.get("_refresh") or [])
    created_tasks = bool(ids_by_type.get("task")) or "task" in refresh
    created_meetings = bool(ids_by_type.get("meeting")) or "meeting" in refresh
    created_reminders = bool(ids_by_type.get("reminder")) or "reminder" in refresh
    created_notes = bool(ids_by_type.get("note")) or "note" in refresh

    # Map current section → render call. Only fire when a matching item was
    # actually created, otherwise we'd spam the user with a redundant list.
    try:
        if current == SectionFSM.in_tasks.state and created_tasks:
            await _render_tasks_for_filter(message, "active")
        elif current == SectionFSM.in_today.state and (created_tasks or created_meetings):
            # /today shows both — re-render via cmd_today.
            await cmd_today(message)
        elif current == SectionFSM.in_meetings.state and created_meetings:
            await _render_meetings_for_filter(message, "week")
        elif current == SectionFSM.in_reminders.state and created_reminders:
            await _render_reminders_for_filter(message, "upcoming")
        elif current == SectionFSM.in_notes.state and created_notes:
            await _render_notes_for_filter(message, "inbox")
    except Exception:
        logger.exception("Section auto-refresh failed (non-fatal)")


_SHOW_ACTION_TYPES = {
    "show_tasks", "show_meetings", "show_notes", "show_reminders", "show_contacts",
}


async def _render_show_action(message: Message, state: "FSMContext | None", action: dict) -> None:
    """Render a full, DB-backed section list for a "ko'rsat/ro'yxat" request.

    Claude only sees today+overdue tasks in its state block, so if it enumerates
    "all tasks" itself the list is INCOMPLETE (reported bug). We instead render
    the real section straight from the DB — every item, with filters/pagination.
    """
    atype = action.get("type")
    filt = (action.get("data") or {}).get("filter") or ""
    if atype == "show_tasks":
        if state is not None:
            await state.set_state(SectionFSM.in_tasks)
        await message.answer("📋 **VAZIFALAR**", parse_mode="Markdown",
                             reply_markup=tasks_section_reply_keyboard())
        await _render_tasks_for_filter(message, filt or "active")
    elif atype == "show_meetings":
        if state is not None:
            await state.set_state(SectionFSM.in_meetings)
        await message.answer("🤝 **UCHRASHUVLAR**", parse_mode="Markdown",
                             reply_markup=meetings_section_reply_keyboard())
        await _render_meetings_for_filter(message, filt or "week")
    elif atype == "show_notes":
        await cmd_notes(message, state)
    elif atype == "show_reminders":
        await cmd_reminders(message, state)
    elif atype == "show_contacts":
        await cmd_team(message, state)


async def _format_create_preview(actions: list[dict]) -> str:
    """Render a confirm-prompt preview for create / bulk-delete actions, shown
    before execution. Bulk deletes show the LIVE row count so the user sees
    exactly how much would be wiped."""
    lines = ["⚠️ **TASDIQLAYSIZMI?**", ""]
    for a in actions:
        t = a.get("type")
        d = a.get("data", {}) or {}
        if t == "delete_task":
            try:
                task = await database.get_task(a.get("id"))
            except Exception:
                task = None
            title = (task or {}).get("title", a.get("id", "—"))
            lines.append(f"🗑 **Vazifa o'chiriladi:** {title}")
            lines.append("   _Qaytarib bo'lmaydi._")
            lines.append("")
            continue
        if t == "cancel_meeting":
            try:
                m = await database.get_meeting(a.get("id"))
            except Exception:
                m = None
            title = (m or {}).get("title", a.get("id", "—"))
            lines.append(f"🗑 **Uchrashuv bekor qilinadi:** {title}")
            lines.append("   _Qaytarib bo'lmaydi._")
            lines.append("")
            continue
        if t in _BULK_DELETE_ACTION_TYPES:
            noun, table = _BULK_DELETE_LABEL[t]
            try:
                n = await database.count_table(table)
            except Exception:
                n = 0
            scope = ""
            if t == "delete_all_tasks" and d.get("status_in"):
                scope = f" ({', '.join(d['status_in'])})"
            lines.append(f"🗑 **Barcha {noun}lar o'chiriladi{scope}** — {n} ta")
            lines.append("   _Bu amalni qaytarib bo'lmaydi._")
            lines.append("")
            continue
        if t == "create_task":
            title = (d.get("title") or "—").strip()
            assignee = (d.get("assignee") or "belgilanmagan").strip()
            deadline_label, _ = _format_deadline_short(d.get("deadline"))
            priority = d.get("priority", "P2")
            pri_label = _PRIORITY_LABEL_UZ.get(priority, priority)
            lines.append("📝 **Yangi vazifa**")
            lines.append(f"   {title}")
            lines.append(f"   👤 Ijrochi: {assignee}")
            lines.append(f"   ⏳ Muddat: {deadline_label}")
            lines.append(f"   🔺 Ustuvorlik: {pri_label}")
            lines.append("")
        elif t == "schedule_meeting":
            title = (d.get("title") or "—").strip()
            participants = ", ".join(d.get("participants", []) or []) or "belgilanmagan"
            location = (d.get("location_or_link") or "belgilanmagan").strip()
            start = d.get("datetime_start", "—")
            try:
                dt = datetime.fromisoformat(start).astimezone(database.TZ)
                start_label = dt.strftime("%d-%m %H:%M")
            except (ValueError, TypeError):
                start_label = start
            lines.append("🤝 **Yangi uchrashuv**")
            lines.append(f"   {title}")
            lines.append(f"   🕐 Vaqt: {start_label}")
            lines.append(f"   👥 Ishtirokchilar: {participants}")
            lines.append(f"   📍 Joy: {location}")
            lines.append("")
    return "\n".join(lines).rstrip()


async def _process_and_reply(message: Message, user_text: str, state: "FSMContext | None" = None) -> None:
    """Send user_text to Claude (streaming), edit a progress message as the
    reply arrives, then attach action buttons once parsing completes.

    If `state` is supplied AND the user has `confirm_create_actions` enabled
    (default), any create_task / schedule_meeting actions in Claude's response
    are deferred: a preview + Tasdiq/Bekor prompt is shown, and execution
    happens only after the user confirms. This protects against mis-transcribed
    voice messages silently creating wrong items.

    Wrapped in a pending_actions row so:
      - a redelivered Telegram update doesn't double-process (UNIQUE update_id),
      - a crash mid-handler is recoverable / observable on next bot start."""

    if not user_text or not user_text.strip():
        await message.answer("Bo'sh xabar. Iltimos, matn yoki ovoz yuboring.")
        return

    update_id = getattr(getattr(message, "_update", None), "update_id", None)
    pending_id = await database.enqueue_pending_action(
        update_id=update_id,
        chat_id=message.chat.id if message.chat else None,
        message_id=message.message_id,
        user_text=user_text,
    )
    if pending_id is None:
        # Duplicate Telegram update — already handled, swallow silently.
        return

    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    progress_msg: Message | None = None
    last_edit_at = 0.0
    last_edit_text = ""
    loop = asyncio.get_event_loop()
    try:
        await database.mark_pending_in_progress(pending_id)
        final_response: dict | None = None

        async for kind, payload in claude_service.process_message_stream(user_text):
            if kind == "partial":
                text = (payload or "").strip()
                if not text:
                    continue
                # Throttle edits: at least N seconds AND N characters of growth
                # before issuing another edit_text call. Telegram's edit rate
                # limit is loose but spammy edits churn the chat for no UX win.
                now = loop.time()
                if (now - last_edit_at) < _STREAM_EDIT_MIN_INTERVAL_SEC:
                    continue
                if abs(len(text) - len(last_edit_text)) < _STREAM_EDIT_MIN_DELTA_CHARS:
                    continue
                if progress_msg is None:
                    progress_msg = await message.answer(text + " ▌")
                else:
                    try:
                        await progress_msg.edit_text(text + " ▌")
                    except TelegramBadRequest:
                        # "message is not modified" or similar — silently skip;
                        # the next edit will be different.
                        pass
                last_edit_at = now
                last_edit_text = text
            elif kind == "complete":
                final_response = payload  # dict envelope
                break

        if final_response is None:
            final_response = claude_service._FALLBACK_RESPONSE

        if final_response.get("needs_clarification"):
            question = (final_response.get("clarification_question")
                        or final_response.get("user_message")
                        or "Aniqlashtiring.")
            if progress_msg is not None:
                try:
                    await progress_msg.edit_text(question, reply_markup=single_back_keyboard())
                except TelegramBadRequest:
                    await message.answer(question, reply_markup=single_back_keyboard())
            else:
                await message.answer(question, reply_markup=single_back_keyboard())
            await database.complete_pending_action(pending_id)
            return

        # ── "Ko'rsat/ro'yxat" intent — render the REAL section from the DB ──
        # Claude's state block only holds today+overdue, so letting it enumerate
        # "all tasks" yields an incomplete list. Intercept show_* and render the
        # full section instead (reported: "barcha vazifalarni ko'rsat" → faqat bir qismi).
        show_action = next(
            (a for a in final_response.get("actions", []) if a.get("type") in _SHOW_ACTION_TYPES),
            None,
        )
        if show_action is not None:
            await database.complete_pending_action(pending_id)
            if progress_msg is not None:
                try:
                    await progress_msg.delete()
                except TelegramBadRequest:
                    pass
            await _render_show_action(message, state, show_action)
            return

        # ── Tasdiq qatlami ──
        # Bulk deletes ("barchasini o'chir") ALWAYS confirm — irreversible.
        # create_task / schedule_meeting confirm only if the setting is on
        # (default ON; /settings dan o'chirish mumkin).
        actions = final_response.get("actions", [])
        try:
            _settings = await database.get_settings()
        except Exception:
            _settings = {}
        bulk_deletes = [a for a in actions if a.get("type") in _BULK_DELETE_ACTION_TYPES]
        single_deletes = [a for a in actions if a.get("type") in _SINGLE_DELETE_ACTION_TYPES]
        # Deletes ALWAYS confirm (irreversible). Creates confirm only if the
        # confirm_create_actions setting is on.
        to_confirm = list(bulk_deletes) + list(single_deletes)
        if _settings.get("confirm_create_actions", True):
            to_confirm += [a for a in actions if a.get("type") in _DESTRUCTIVE_ACTION_TYPES]
        if state is not None and to_confirm:
            if True:
                preview = await _format_create_preview(to_confirm)
                confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Tasdiqlayman", callback_data="acts_confirm"),
                    InlineKeyboardButton(text="✕ Bekor qilish", callback_data="acts_cancel"),
                ]])
                # Remember the prior section state so cb_actions_confirm can
                # restore it and re-render the section after the deferred execute.
                try:
                    prior_state = await state.get_state()
                except Exception:
                    prior_state = None
                await state.set_state(CreateActionConfirmFSM.awaiting)
                await state.update_data(
                    pending_response=final_response,
                    _prior_section=prior_state,
                )
                if progress_msg is not None:
                    try:
                        await progress_msg.edit_text(
                            preview, parse_mode="Markdown", reply_markup=confirm_kb
                        )
                    except TelegramBadRequest:
                        await _safe_answer(message, preview,
                                            reply_markup=confirm_kb, parse_mode="Markdown")
                else:
                    await _safe_answer(message, preview,
                                        reply_markup=confirm_kb, parse_mode="Markdown")
                await database.complete_pending_action(pending_id)
                return

        ids_by_type = await _execute_actions(actions)
        keyboard = _build_keyboard(final_response.get("buttons", []), ids_by_type)
        if keyboard:
            keyboard = _append_back_row(keyboard)

        text = (final_response.get("user_message") or "").strip() or "✅"
        text += _failed_actions_note(ids_by_type)
        if progress_msg is not None:
            # Finalize the same message we've been editing — single chat bubble.
            try:
                await progress_msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
            except TelegramBadRequest:
                # Editing failed (deleted? markdown parse error?) — send fresh.
                await _safe_answer(message, text, reply_markup=keyboard, parse_mode="Markdown")
        else:
            await _safe_answer(message, text, reply_markup=keyboard, parse_mode="Markdown")
        await database.complete_pending_action(pending_id)
        # If the user is currently inside a section view (/tasks, /meetings,
        # /reminders, /today), re-render it with the new item visible.
        await _maybe_refresh_section(message, state, ids_by_type)
    except Exception as e:
        logger.exception("_process_and_reply failed for pending=%s", pending_id)
        await database.fail_pending_action(pending_id, f"{type(e).__name__}: {e}")
        # Tell the user instead of failing silently. Handled here (no re-raise);
        # the global error handler in bot.py is the fallback for everything else.
        try:
            await message.answer("⚠️ Texnik xato yuz berdi. Iltimos, qaytadan urinib ko'ring.")
        except Exception:
            logger.debug("Could not send error notice in _process_and_reply")
    finally:
        typing_task.cancel()


# ─────────────────────── COMMAND HANDLERS ───────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    # /start is the universal "I'm confused, reset" command — clear any FSM
    # state so a user stuck mid-flow can recover without finding a Back button.
    await state.clear()
    await message.answer(
        "**Yordamchi tayyor** 🤝\n\n"
        "Matn yoki ovoz orqali topshiriq yuborishingiz mumkin.\n"
        "Pastdagi tugmalardan foydalanib tezda harakat qiling.",
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Universal escape hatch — clear any FSM state and confirm to the user.
    Matches industry-standard /cancel convention."""
    current = await state.get_state()
    await state.clear()
    if current:
        await message.answer(
            "✕ Bekor qilindi. Asosiy menyu.",
            reply_markup=main_reply_keyboard(),
        )
    else:
        await message.answer(
            "Hech qaysi amal aktiv emas. Asosiy menyu.",
            reply_markup=main_reply_keyboard(),
        )


@router.message(Command("new"))
async def cmd_new_command(message: Message, state: FSMContext) -> None:
    """/new slash-command entry point. Delegates to the existing cmd_new()
    submenu so behavior matches both /new typed and the ➕ Yangi button."""
    await cmd_new(message, state)


@router.message(Command("backup"))
async def cmd_backup(message: Message) -> None:
    """Create an on-demand SQLite backup using the .backup API (consistent
    snapshot — works even while the bot is writing). Saves to data/backups/
    with a timestamped name. Sends a status reply with the file size and
    integrity-check result.

    For automated daily backups see DEPLOY.md cron section."""
    import os
    import sqlite3
    from datetime import datetime as _dt

    timestamp = _dt.now(database.TZ).strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(config.DATABASE_PATH).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"yordamchi-manual-{timestamp}.db"

    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        # sqlite3 .backup runs in a thread; use to_thread to keep the loop free.
        def _do_backup() -> tuple[int, str]:
            with sqlite3.connect(config.DATABASE_PATH) as src, sqlite3.connect(str(backup_path)) as dst:
                src.backup(dst)
            size = os.path.getsize(backup_path)
            # Integrity check on the backup file (not source)
            with sqlite3.connect(str(backup_path)) as check:
                cur = check.execute("PRAGMA integrity_check")
                result = cur.fetchone()[0]
            return size, result

        size, integrity = await asyncio.to_thread(_do_backup)
    except Exception as e:
        logger.exception("Backup failed")
        await message.answer(f"❌ Backup xato: {type(e).__name__}: {e}")
        return
    finally:
        typing_task.cancel()

    kb_size = size / 1024
    ok_mark = "✅" if integrity == "ok" else f"⚠️ {integrity}"
    await _safe_answer(
        message,
        f"💾 **Backup yaratildi**\n\n"
        f"📁 `{backup_path.name}`\n"
        f"📦 Hajmi: {kb_size:,.0f} KB\n"
        f"🔍 Integrity: {ok_mark}\n\n"
        f"_To'liq yo'l: `{backup_path}`_\n"
        f"_Avtomatik kunlik backup GCS'ga ham olinadi (DEPLOY.md)._",
        parse_mode="Markdown",
    )


@router.message(Command("delegations"))
async def cmd_delegations(message: Message) -> None:
    """Delegation tracker — show tasks assigned to others, sorted by how
    long they've been pending. Surfaces "stuck" delegations before they
    become problems."""
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """SELECT id, title, assignee, deadline, status, created_at,
                      julianday('now') - julianday(created_at) AS age_days
               FROM tasks
               WHERE status IN ('todo', 'in_progress')
                 AND assignee IS NOT NULL
                 AND TRIM(assignee) != ''
                 AND LOWER(assignee) NOT IN ('belgilanmagan', 'men', 'siz', 'o''zim', 'ozim', 'o''z', 'oz')
               ORDER BY age_days DESC
               LIMIT 20"""
        )
        rows = [dict(r) for r in await cur.fetchall()]

    if not rows:
        await _safe_answer(
            message,
            "👥 **DELEGATSIYALAR**\n\n_Boshqa kishilarga berilgan aktiv vazifa yo'q._",
            parse_mode="Markdown",
            reply_markup=single_back_keyboard(),
        )
        return

    lines = ["👥 **DELEGATSIYALAR**", "", f"_{len(rows)} ta aktiv delegatsiya:_", ""]
    for i, t in enumerate(rows[:12], 1):
        age = int(t["age_days"] or 0)
        age_label = "bugun" if age == 0 else f"{age} kun"
        deadline_label, _ = _format_deadline_short(t.get("deadline"))
        title = (t.get("title") or "—")[:60]
        lines.append(f"**{i}. {title}**")
        lines.append(f"   👤 {t['assignee']} · ⏳ {deadline_label} · 📅 {age_label} oldin")
        lines.append("")
    if len(rows) > 12:
        lines.append(f"_+{len(rows) - 12} ta yana_")

    await _safe_answer(
        message,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=single_back_keyboard(),
    )


@router.message(Command("diagnostics"))
async def cmd_diagnostics(message: Message) -> None:
    """Bot health snapshot — DB size, recent LLM cost, scheduler state, iCloud.
    Useful when something feels broken and you want a single view of internals."""
    import os
    import claude_service
    lines = ["🔍 **DIAGNOSTICS**", ""]

    # DB size
    try:
        db_bytes = os.path.getsize(config.DATABASE_PATH)
        db_kb = db_bytes / 1024
        lines.append(f"📦 DB: `{config.DATABASE_PATH}` — {db_kb:,.0f} KB")
    except OSError:
        lines.append(f"📦 DB: `{config.DATABASE_PATH}` — ❌ topilmadi")

    # Pending actions
    try:
        stuck = await database.list_stuck_pending_actions(stuck_after_minutes=5)
        lines.append(f"⏳ Stuck pending_actions: {len(stuck)}" if stuck else "⏳ Stuck pending_actions: 0 ✅")
    except Exception:
        lines.append("⏳ Stuck pending_actions: tekshirib bo'lmadi")

    # LLM cost + cache hit rate (last 7 days)
    try:
        breakdown = await database.llm_cost_breakdown(days=7)
        totals = breakdown["totals"]
        lines.append(
            f"💰 7-kunlik: ${totals['cost_usd']:.4f} · "
            f"{totals['calls']} chaqiruv · cache hit {totals['cache_hit_rate'] * 100:.0f}%"
        )
        if breakdown["by_model"]:
            lines.append("   Modellar:")
            for row in breakdown["by_model"][:5]:
                lines.append(f"     • {row['model']}: {row['calls']} (${float(row['cost_usd'] or 0):.4f})")
    except Exception as e:
        lines.append(f"💰 LLM cost: xato ({type(e).__name__})")

    # Circuit breaker
    if claude_service._circuit_is_open():
        remain = claude_service._circuit_open_until - __import__("time").time()
        lines.append(f"⚠️ Claude circuit OPEN ({remain:.0f}s qoldi)")
    else:
        lines.append("✅ Claude circuit closed")

    # Scheduler
    sched = scheduler_module.get_scheduler()
    if sched and sched.scheduler.running:
        jobs = sched.scheduler.get_jobs()
        lines.append(f"⏰ Scheduler: {len(jobs)} aktiv job")
    else:
        lines.append("⏰ Scheduler: ❌ ishlamayapti")

    # iCloud
    if config.ICLOUD_ENABLED:
        lines.append("☁️ iCloud: yoqilgan")
    else:
        lines.append("☁️ iCloud: o'chiq")

    # Redaction
    import redaction
    redaction_label = "o'chiq" if redaction.DISABLED else "yoqilgan"
    lines.append(f"🛡 Redaction: {redaction_label}")

    # Background tasks
    bg = len(_background_tasks)
    lines.append(f"🔧 Background tasks: {bg}")

    await _safe_answer(message, "\n".join(lines), parse_mode="Markdown")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "🤝 **Yordamchi — qo'llanma**\n\n"
        "**1. Boshqaruv**\n"
        "• `/cockpit` — Boshqaruv paneli: umumiy holat, top vazifalar, risklar va tavsiyalar.\n"
        "• `/plan` — murakkab vaziyat uchun aniq ish rejasi.\n\n"
        "**2. Vazifalar**\n"
        "• `/tasks` — aktiv, bugungi, muhim, o'tgan, bajarilgan va takroriy vazifalar.\n\n"
        "**3. Eslatmalar**\n"
        "• `/reminders` — alohida eslatmalar, snooze, takrorlash va bajarildi nazorati.\n\n"
        "**4. Notes (Inbox)**\n"
        "• `/notes` — qayta ishlanmagan note'lar inbox'i (GTD uslubi).\n"
        "• `/qayd <matn>` — tezkor note qo'shish.\n"
        "• Boshqa chatdan xabarni forward qiling — avto note'ga aylanadi.\n"
        "• Voice: _\"qayd qil: ...\"_ — ovozdan ham mumkin.\n\n"
        "**5. Uchrashuvlar**\n"
        "• `/meetings` — uchrashuvlar, tayyorgarlik brifi va action itemlar.\n\n"
        "**6. Natijalar**\n"
        "• `/stats` — KPI, deadline, delegatsiya, meeting va bot auditi.\n"
        "• Weekly/monthly report statistikadagi tugmalar orqali ochiladi.\n\n"
        "**7. Tizim**\n"
        "• `/settings` — bildirishnomalar, eslatmalar va kalendar holati.\n"
        "• `/help` — ushbu qo'llanma.\n\n"
        "**Avtomatik ishlaydigan funksiyalar**\n"
        "• 08:00 — kunlik brifing\n"
        "• 18:00 — kun yakuni\n"
        "• Uchrashuvdan oldin — prep brief\n"
        "• Uchrashuvdan keyin — action item eslatmasi\n"
        "• Deadline yaqinlashsa — muhim vazifa eslatmasi",
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )


UZ_WEEKDAYS_FULL = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba", "Yakshanba"]
UZ_MONTHS_FULL = ["yanvar", "fevral", "mart", "aprel", "may", "iyun",
                  "iyul", "avgust", "sentyabr", "oktyabr", "noyabr", "dekabr"]


async def _build_briefing_text() -> str:
    """Deterministic daily operating briefing."""
    now = datetime.now(database.TZ)

    today_tasks = await database.list_today_tasks()
    done_today = await database.list_tasks_done_today()
    today_meetings = await database.list_today_meetings()
    overdue = await database.list_overdue_tasks()

    def _key(t):
        p = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(t.get("priority", "P2"), 2)
        d = t.get("deadline") or "9999"
        return (p, d)

    today_tasks = sorted(today_tasks, key=_key)
    best_task = (overdue[:1] or today_tasks[:1] or [None])[0]
    urgent_count = sum(1 for t in today_tasks if t.get("priority") == "P0")
    missing_assignee = [t for t in today_tasks if not (t.get("assignee") or "").strip()]

    date_label_upper = f"{now.day}-{UZ_MONTHS_FULL[now.month - 1].upper()}"
    DIVIDER = "━" * 20

    if not today_tasks and not today_meetings and not overdue:
        return "\n".join([
            f"🗓 **BUGUN · {date_label_upper}**",
            "",
            "Bugun uchun aktiv vazifa yoki uchrashuv yo'q.",
            "",
            "_Kun boshida 1-2 ta muhim vazifani rejalashtiring._",
        ])

    def _muhimlik_emoji(priority: str) -> str:
        # ⚡ only for Shoshilinch (P0); 🔹 for everything else
        return "⚡" if priority == "P0" else "🔹"

    def _task_card(task: dict, prefix: str = "") -> list[str]:
        """One task card: title with priority badge + 3 detail lines.
        prefix: '' for the Eng muhim card; 'N. ' for numbered list items.
        Continuation lines are indented to match the prefix width.
        """
        title = (task.get("title") or "—").strip()
        priority = task.get("priority", "P2")
        badge = _PRIORITY_BADGE.get(priority, "⚪")
        muhimlik = _PRIORITY_LABEL_UZ.get(priority, "Rejadagi")
        assignee = (task.get("assignee") or "belgilanmagan").strip()
        muddat = _muddat_label(task.get("deadline"))
        indent = " " * len(prefix)
        return [
            f"{prefix}{badge} {title}",
            f"{indent}👤 Ijrochi: {assignee}",
            f"{indent}⏳ Muddat: {muddat}",
            f"{indent}{_muhimlik_emoji(priority)} Muhimlik: {muhimlik}",
        ]

    # Inbox count — surfaces "you have N notes waiting to be triaged" so the
    # principal doesn't forget to clear the GTD inbox.
    try:
        inbox_count = await database.count_notes_in_status("inbox")
    except Exception:
        inbox_count = 0

    lines: list[str] = [
        f"🗓 **BUGUN · {date_label_upper}**",
        "",
        "📌 **UMUMIY HOLAT**",
        "",
        f"**{len(today_tasks)}** ta vazifa  ·  **{len(done_today)}** ta yopildi",
        f"**{urgent_count}** ta shoshilinch  ·  **{len(overdue)}** ta muddati o'tgan",
    ]
    if inbox_count > 0:
        lines.append(f"📥 **{inbox_count}** ta note inbox'da kutmoqda")
    lines.append("")

    if best_task:
        lines.extend([DIVIDER, "", "⭐ **ENG MUHIM**", ""])
        lines.extend(_task_card(best_task))
        lines.append("")

    if today_tasks:
        lines.extend([DIVIDER, "", "📌 **VAZIFALAR**", ""])
        for i, task in enumerate(today_tasks[:8], 1):
            lines.extend(_task_card(task, prefix=f"{i}. "))
            lines.append("")
        if len(today_tasks) > 8:
            lines.append(f"_+{len(today_tasks) - 8} ta yana_")
            lines.append("")

    # UCHRASHUVLAR — bot uslubi: 4-qatorli kartochka (Variant A bilan mos)
    if today_meetings:
        lines.extend([DIVIDER, "", "🤝 **UCHRASHUVLAR**", ""])
        for i, meeting in enumerate(today_meetings[:3], 1):
            title = (meeting.get("title") or "—").strip()
            parts = meeting.get("participants") or []
            if not parts:
                plabel = "belgilanmagan"
            elif len(parts) <= 3:
                plabel = ", ".join(parts)
            else:
                plabel = f"{', '.join(parts[:3])} (+{len(parts) - 3} nafar)"
            location = (meeting.get("location_or_link") or "").strip() or "belgilanmagan"
            time_label = _meeting_time_label(meeting.get("datetime_start") or "")
            lines.extend([
                f"{i}.  {title}",
                "",
                f"      ⏰ Vaqt:             {time_label}",
                f"      👥 Ishtirokchilar:   {plabel}",
                f"      📍 Manzil:           {location}",
                "",
            ])
        if len(today_meetings) > 3:
            lines.append(f"_+{len(today_meetings) - 3} ta yana_")
            lines.append("")

    # TAVSIYALAR — 3 buckets: HOZIR / BUGUN / KUN OXIRI
    if best_task and best_task.get("priority") == "P0":
        name = (best_task.get("assignee") or "").strip()
        if name:
            hozir = f"{name} ijrosidagi shoshilinch vazifa bo'yicha status olish."
        else:
            urgent_title = _truncate((best_task.get("title") or "—").strip(), 50)
            hozir = f"Shoshilinch vazifaga ijrochi tayinlash: «{urgent_title}»."
    elif overdue:
        first_title = _truncate((overdue[0].get("title") or "—").strip(), 50)
        hozir = f"Eng kechikkan vazifani («{first_title}») yopish yoki yangi muddat belgilash."
    else:
        hozir = "Eng yuqori ustuvorlikdagi vazifani boshlash."

    if overdue:
        bugun_rec = (f"Muddati o'tgan {len(overdue)} ta vazifani yopish "
                     "yoki yangi aniq muddat belgilash.")
    elif missing_assignee:
        bugun_rec = (f"Ijrochisi belgilanmagan {len(missing_assignee)} ta vazifaga "
                     "mas'ul tayinlash.")
    else:
        bugun_rec = f"Bugungi {len(today_tasks)} ta vazifani ketma-ket yopib borish."

    kun_oxiri = ("Yopilgan, qolgan va kechikayotgan vazifalar bo'yicha "
                 "qisqa qayta hisobot chiqarish.")

    lines.extend([
        DIVIDER, "",
        "💡 **TAVSIYALAR**", "",
        "🔴 **HOZIR**",
        hozir, "",
        "⏳ **BUGUN**",
        bugun_rec, "",
        "🧾 **KUN OXIRI**",
        kun_oxiri,
    ])

    return "\n".join(lines).rstrip()


def _parse_dt_safe(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt.astimezone(database.TZ) if dt.tzinfo else database.TZ.localize(dt)
    except (TypeError, ValueError):
        return None





def _today_inline_keyboard(today_tasks: list[dict] | None = None) -> InlineKeyboardMarkup | None:
    """Bugun ekrani uchun INLINE klaviatura — faqat raqamli drill-down.

    Quick actions (➕ Yangi vazifa, 🌙 Kechki yakun, 📌 Vazifalar,
    🤝 Uchrashuvlar) endi pastdagi section reply kbd da bor —
    inline'dan olib tashlandi.
    """
    if not today_tasks:
        return None
    rows: list[list[InlineKeyboardButton]] = []
    nums = [
        InlineKeyboardButton(text=str(i + 1), callback_data=f"taskopen:{t['id']}")
        for i, t in enumerate(today_tasks[:8])
    ]
    for i in range(0, len(nums), 5):
        rows.append(nums[i:i + 5])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None



@router.message(Command("today"))
async def cmd_today(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.set_state(SectionFSM.in_today)
    # Bo'lim header + section reply kbd
    await message.answer(
        "📅 **BUGUN**", parse_mode="Markdown",
        reply_markup=today_section_reply_keyboard(),
    )
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        text = await _build_briefing_text()
        today_tasks = await database.list_today_tasks()
    finally:
        typing_task.cancel()

    def _key(t):
        p = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(t.get("priority", "P2"), 2)
        d = t.get("deadline") or "9999"
        return (p, d)

    sorted_today = sorted(today_tasks, key=_key)
    # Inline keyboard: faqat raqamli drill-down + Orqaga
    # (quick actions reply kbd ga ko'chdi)
    keyboard = _today_inline_keyboard(today_tasks=sorted_today)
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=keyboard)


@router.message(Command("briefing"))
async def cmd_briefing(message: Message, state: FSMContext | None = None) -> None:
    await cmd_today(message, state)



_PRIORITY_BADGE = {"P0": "🔴", "P1": "🟠", "P2": "🔵", "P3": "⚪"}
# User-facing priority names — replace technical P0/P1/P2/P3 in ALL displays.
_PRIORITY_LABEL_UZ = {
    "P0": "Shoshilinch",
    "P1": "Muhim",
    "P2": "Rejadagi",
    "P3": "Past ustuvorlik",
}


def _muddat_label(iso) -> str:
    """Sodda 'Muddat:' qiymati — list view'lar uchun.
    - overdue → "o'tgan"
    - bugun   → "Bugun HH:MM"
    - ertaga  → "Ertaga HH:MM"
    - boshqa  → "DD-MM, HH:MM"
    - yo'q    → "belgilanmagan"
    """
    if not iso:
        return "belgilanmagan"
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    now = datetime.now(database.TZ)
    if dt < now:
        return "o'tgan"
    if now.date() == dt.date():
        return f"Bugun {dt.strftime('%H:%M')}"
    if (now + timedelta(days=1)).date() == dt.date():
        return f"Ertaga {dt.strftime('%H:%M')}"
    return dt.strftime("%d-%m, %H:%M")
_STATUS_LABEL_UZ = {"todo": "Aktiv", "in_progress": "Jarayonda",
                    "blocked": "Toʻsilgan", "done": "Bajarildi", "cancelled": "Bekor qilingan"}
_STATUS_EMOJI = {"todo": "⏳", "in_progress": "🔄", "blocked": "⚠️", "done": "✅", "cancelled": "❌"}
_NUMBER_GLYPH = {1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤", 6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩"}
_SEP = "━" * 25



async def compute_risk_score() -> dict:
    """Compute a 0-100 risk score from current DB state.

    Components (each contributes 0-25 points, capped):
      • Overdue tasks (count × 5, max 25)
      • Tasks due within 24h × 4, max 20
      • Unassigned + due within 48h × 6, max 18
      • Tasks without deadline × 1.5, max 12
      • Urgent (P0) not done × 5, max 25

    Returns: {score: int, status: str, components: dict}
    """
    # Single SELECT with conditional aggregates — was 6 separate queries
    # opening 6 connections (N+1 anti-pattern). See database.risk_score_counts.
    counts = await database.risk_score_counts()

    components = {
        "overdue": min(25, counts["overdue"] * 5),
        "due_24h": min(20, counts["due_24h"] * 4),
        "unassigned_urgent": min(18, counts["unassigned_due_48h"] * 6),
        "no_deadline": min(12, int(counts["no_deadline"] * 1.5)),
        "urgent_open": min(25, counts["urgent_open"] * 5),
    }
    score = min(100, sum(components.values()))
    if score <= 30:
        status = "Past risk"
        emoji = "🟢"
    elif score <= 60:
        status = "Nazoratda"
        emoji = "🟡"
    elif score <= 80:
        status = "Yuqori risk"
        emoji = "🟠"
    else:
        status = "Kritik risk"
        emoji = "🔴"
    return {
        "score": score,
        "status": status,
        "emoji": emoji,
        "components": components,
        "counts": {
            "overdue": counts["overdue"],
            "due_24h": counts["due_24h"],
            "due_48h": counts["due_48h"],
            "no_deadline": counts["no_deadline"],
            "unassigned": counts["unassigned"],
            "urgent_open": counts["urgent_open"],
        },
    }


def _format_deadline_short(iso) -> tuple[str, bool]:
    """Return (human_label, is_overdue) — bugun/ertaga/o'tgan kabi tabiiy ko'rinish."""
    if not iso:
        return "Deadline yoʻq", False
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso), False
    now = datetime.now(database.TZ)
    overdue = dt < now
    today = now.date() == dt.date()
    tomorrow = (now + timedelta(days=1)).date() == dt.date()
    time_str = dt.strftime("%H:%M")
    if today:
        return f"Bugun {time_str}" + (" · MUDDATI OʻTDI" if overdue else ""), overdue
    if tomorrow:
        return f"Ertaga {time_str}", overdue
    label = dt.strftime("%d-%m, %H:%M")
    if overdue:
        label += " · oʻtgan"
    return label, overdue


# ─────────────────────── SETTINGS ───────────────────────


def _format_settings_summary(settings: dict) -> str:
    """Human-readable settings overview shown when entering /settings."""
    voice_status = (
        "AVTO - tasdiqsiz"
        if settings.get("voice_auto_confirm", True)
        else "tasdiq so'raladi"
    )
    create_confirm_status = (
        "tasdiq so'raladi"
        if settings.get("confirm_create_actions", True)
        else "tasdiqsiz yaratiladi"
    )
    return (
        "⚙️ **SOZLAMALAR**\n\n"
        f"🔔 Bildirishnomalar: {'yoqilgan' if settings.get('notifications_enabled', True) else 'oʻchirilgan'}\n"
        f"⏰ Ertalab brifing: `{settings.get('morning_briefing_time', '08:00')}`\n"
        f"🌙 Kechki yakun: `{settings.get('evening_summary_time', '18:00')}`\n"
        f"📞 Uchrashuv eslatmasi: `{settings.get('meeting_reminder_min', 15)} daq oldin`\n"
        f"📌 Vazifa eslatmasi: `{settings.get('task_reminder_hours', 2)} soat oldin`\n"
        f"🎙 Ovoz transkripti: `{voice_status}`\n"
        f"✅ Vazifa/uchrashuv yaratish: `{create_confirm_status}`\n\n"
        "_Pastdagi tugmalardan parametr tanlang._"
    )


@router.message(Command("settings"))
async def cmd_settings(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.set_state(SectionFSM.in_settings)
    settings = await database.get_settings()
    text = _format_settings_summary(settings)
    await message.answer(text, parse_mode="Markdown",
                         reply_markup=settings_section_reply_keyboard())


@router.callback_query(F.data == "setting:notifications_toggle")
async def cb_setting_notif(query: CallbackQuery) -> None:
    settings = await database.get_settings()
    new_val = not settings["notifications_enabled"]
    await database.set_setting("notifications_enabled", new_val)
    await query.answer(f"Bildirishnomalar {'yoqildi' if new_val else 'oʻchirildi'} ✅")
    settings["notifications_enabled"] = new_val
    try:
        await query.message.edit_reply_markup(reply_markup=settings_keyboard(settings))
    except Exception:
        pass


@router.callback_query(F.data == "setting:briefing_time")
async def cb_setting_briefing_time(query: CallbackQuery) -> None:
    await query.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"brieftime:{t}") for t in ("07:00", "07:30", "08:00")],
        [InlineKeyboardButton(text=t, callback_data=f"brieftime:{t}") for t in ("08:30", "09:00", "10:00")],
        [back_button("nav_settings")],
    ])
    await query.message.answer("Ertalab brifing vaqti:", reply_markup=kb)


@router.callback_query(F.data.startswith("brieftime:"))
async def cb_brief_time(query: CallbackQuery) -> None:
    new_time = query.data.removeprefix("brieftime:")
    await database.set_setting("morning_briefing_time", new_time)
    await _reschedule_briefings_live()
    await query.answer(f"Brifing: {new_time} ✅ (kuchga kirdi)")
    try:
        await query.message.delete()
    except Exception:
        pass
    await cmd_settings(query.message)


@router.callback_query(F.data == "setting:evening_time")
async def cb_setting_evening_time(query: CallbackQuery) -> None:
    await query.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=f"eveningtime:{t}") for t in ("17:00", "17:30", "18:00")],
        [InlineKeyboardButton(text=t, callback_data=f"eveningtime:{t}") for t in ("18:30", "19:00", "20:00")],
        [back_button("nav_settings")],
    ])
    await query.message.answer("Kechki yakun vaqti:", reply_markup=kb)


@router.callback_query(F.data.startswith("eveningtime:"))
async def cb_evening_time(query: CallbackQuery) -> None:
    new_time = query.data.removeprefix("eveningtime:")
    await database.set_setting("evening_summary_time", new_time)
    await _reschedule_briefings_live()
    await query.answer(f"Kechki yakun: {new_time} ✅ (kuchga kirdi)")
    try:
        await query.message.delete()
    except Exception:
        pass
    await cmd_settings(query.message)


async def _reschedule_briefings_live() -> None:
    """Ask the running scheduler to reload briefing times from settings."""
    sched = scheduler_module.get_scheduler()
    if sched is not None:
        try:
            await sched.apply_briefing_settings()
        except Exception:
            logger.exception("Failed to apply briefing settings live")


@router.callback_query(F.data == "setting:reminders")
async def cb_setting_reminders(query: CallbackQuery) -> None:
    settings = await database.get_settings()
    text = (
        "📲 **Eslatma parametrlari**\n\n"
        f"• Uchrashuv eslatmasi: `{settings['meeting_reminder_min']} daq` oldin\n"
        f"• Vazifa eslatmasi: `{settings['task_reminder_hours']} soat` oldin (Shoshilinch va Muhim uchun)\n\n"
        "_Standart qiymatlarni saqlash tavsiya etiladi._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="15 daq", callback_data="meetremind:15"),
         InlineKeyboardButton(text="30 daq", callback_data="meetremind:30"),
         InlineKeyboardButton(text="60 daq", callback_data="meetremind:60")],
        [InlineKeyboardButton(text="1 soat", callback_data="taskremind:1"),
         InlineKeyboardButton(text="2 soat", callback_data="taskremind:2"),
         InlineKeyboardButton(text="4 soat", callback_data="taskremind:4")],
        [back_button("nav_settings")],
    ])
    await query.answer()
    await query.message.answer(text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("meetremind:"))
async def cb_meet_remind(query: CallbackQuery) -> None:
    mins = _cb_int(query.data, default=15)
    if mins <= 0 or mins > 24 * 60:
        await query.answer("Noto'g'ri qiymat")
        return
    await database.set_setting("meeting_reminder_min", mins)
    await _apply_reminder_settings_live()
    await query.answer(f"Uchrashuv eslatmasi: {mins} daq oldin ✅")


@router.callback_query(F.data.startswith("taskremind:"))
async def cb_task_remind(query: CallbackQuery) -> None:
    hrs = _cb_int(query.data, default=2)
    if hrs <= 0 or hrs > 168:
        await query.answer("Noto'g'ri qiymat")
        return
    await database.set_setting("task_reminder_hours", hrs)
    await _apply_reminder_settings_live()
    await query.answer(f"Vazifa eslatmasi: {hrs} soat oldin ✅")


async def _apply_reminder_settings_live() -> None:
    """Ask the running scheduler to reload reminder lead times from settings."""
    sched = scheduler_module.get_scheduler()
    if sched is not None:
        try:
            await sched.apply_reminder_settings()
        except Exception:
            logger.exception("Failed to apply reminder settings live")


@router.callback_query(F.data == "setting:calendar")
async def cb_setting_calendar(query: CallbackQuery) -> None:
    await query.answer()
    await cmd_calendar(query.message)


@router.callback_query(F.data == "setting:voice_auto_toggle")
async def cb_setting_voice_auto_toggle(query: CallbackQuery) -> None:
    """Flip the voice_auto_confirm setting between AUTO and confirm-prompt mode."""
    settings = await database.get_settings()
    new_val = not settings.get("voice_auto_confirm", True)
    await database.set_setting("voice_auto_confirm", new_val)
    label = "AVTO — tasdiqsiz" if new_val else "Tasdiq so'raladi"
    await query.answer(f"Ovoz: {label} ✅")
    settings["voice_auto_confirm"] = new_val
    try:
        await query.message.edit_reply_markup(reply_markup=settings_keyboard(settings))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "setting:confirm_create_toggle")
async def cb_setting_confirm_create_toggle(query: CallbackQuery) -> None:
    """Flip the confirm_create_actions setting. When ON (default), the bot
    asks for confirmation before creating any task or meeting."""
    settings = await database.get_settings()
    new_val = not settings.get("confirm_create_actions", True)
    await database.set_setting("confirm_create_actions", new_val)
    label = "yoqildi (xavfsizroq)" if new_val else "o'chirildi (tezroq)"
    await query.answer(f"Yaratish tasdig'i {label} ✅")
    settings["confirm_create_actions"] = new_val
    try:
        await query.message.edit_reply_markup(reply_markup=settings_keyboard(settings))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "setting:quiet_hours")
async def cb_setting_quiet_hours(query: CallbackQuery) -> None:
    """Toggle quiet hours on/off + show the time window picker."""
    settings = await database.get_settings()
    qh_on = settings.get("quiet_hours_enabled", False)
    qh_start = settings.get("quiet_hours_start", "22:00")
    qh_end = settings.get("quiet_hours_end", "07:00")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=("🔕 Sukunatni O'CHIRISH" if qh_on else "🌙 Sukunatni YOQISH"),
            callback_data="quiet:toggle",
        )],
        [InlineKeyboardButton(text=f"⏰ Boshlanish: {qh_start}", callback_data="quiet:start")],
        [InlineKeyboardButton(text=f"⏰ Tugash: {qh_end}", callback_data="quiet:end")],
        [back_button("nav_settings")],
    ])
    state_label = "yoqilgan" if qh_on else "o'chiq"
    await query.answer()
    await query.message.answer(
        f"🌙 **Sukunat soatlari**\n\n"
        f"Holat: **{state_label}**\n"
        f"Vaqt: `{qh_start}` → `{qh_end}`\n\n"
        f"_Sukunat ichida brifing va eslatmalar yo'naltirilmaydi. "
        f"Faqat /diagnostics, /cancel va sizning xabarlaringizga javob qaytadi._",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.callback_query(F.data == "quiet:toggle")
async def cb_quiet_toggle(query: CallbackQuery) -> None:
    settings = await database.get_settings()
    new_val = not settings.get("quiet_hours_enabled", False)
    await database.set_setting("quiet_hours_enabled", new_val)
    label = "yoqildi" if new_val else "o'chirildi"
    await query.answer(f"Sukunat {label} ✅")
    try:
        await query.message.delete()
    except TelegramBadRequest:
        pass
    await cb_setting_quiet_hours(query)


@router.callback_query(F.data.in_({"quiet:start", "quiet:end"}))
async def cb_quiet_time_picker(query: CallbackQuery) -> None:
    which = "start" if query.data == "quiet:start" else "end"
    label = "Boshlanish" if which == "start" else "Tugash"
    presets = ["19:00", "20:00", "21:00", "22:00", "23:00"] if which == "start" else \
              ["06:00", "07:00", "08:00", "09:00", "10:00"]
    rows = [
        [InlineKeyboardButton(text=t, callback_data=f"qtime:{which}:{t}") for t in presets[:3]],
        [InlineKeyboardButton(text=t, callback_data=f"qtime:{which}:{t}") for t in presets[3:]],
        [back_button("nav_settings")],
    ]
    await query.answer()
    await query.message.answer(
        f"⏰ Sukunat {label} vaqti:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("qtime:"))
async def cb_quiet_set_time(query: CallbackQuery) -> None:
    parts = query.data.split(":", 3)  # qtime:start:HH:MM
    if len(parts) < 4:
        await query.answer()
        return
    which, hh, mm = parts[1], parts[2], parts[3]
    new_time = f"{hh}:{mm}"
    key = "quiet_hours_start" if which == "start" else "quiet_hours_end"
    await database.set_setting(key, new_time)
    await query.answer(f"Saqlandi: {new_time} ✅")
    try:
        await query.message.delete()
    except TelegramBadRequest:
        pass
    await cb_setting_quiet_hours(query)


# ─────────────────────── YANGI SUBMENU ───────────────────────


async def cmd_new(message: Message, state: FSMContext | None = None) -> None:
    """Yangi menu — section reply kbd ko'rinishida.
    Inline tugmalar olib tashlandi: reply kbd 5 ta turini taqdim etadi.
    """
    if state is not None:
        await state.set_state(SectionFSM.in_new)
    text = (
        "➕ **YANGI**\n\n"
        "Quyidagi turlardan birini tanlang:"
    )
    await message.answer(text, parse_mode="Markdown",
                         reply_markup=new_section_reply_keyboard())


@router.callback_query(F.data.startswith("new:"))
async def cb_new_type(query: CallbackQuery, state: FSMContext) -> None:
    kind = query.data.split(":", 1)[1]
    await query.answer()

    # 📝 Yangi vazifa → offer two paths: 🤖 Tezkor (natural) or 📋 Forma (guided FSM)
    if kind == "task":
        text = (
            "📝 **YANGI VAZIFA**\n" + _SEP + "\n\n"
            "Qaysi usul orqali yaratasiz?\n\n"
            "• 🤖 **Tezkor** — matn yoki ovoz yuboring, men tushunib yarataman.\n"
            "• 📝 **Forma** — bosqichma-bosqich (sarlavha → muddat → ijrochi)."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🤖 Tezkor", callback_data="new:task_quick"),
                InlineKeyboardButton(text="📝 Forma", callback_data="new:task_form"),
            ],
            [back_button("nav_new")],
        ])
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)
        return

    if kind == "task_form":
        await _newtask_start(query.message, state)
        return

    if kind == "task_quick":
        prompt = ("📝 **Yangi vazifa — tezkor**\n\nMatn yoki ovoz yuboring. Misol:\n"
                   "_\"Ertaga ertalab Aziz akaga marketing hisobotini yuborish\"_")
        await _safe_answer(query.message, prompt, parse_mode="Markdown",
                            reply_markup=single_back_keyboard("nav_new"))
        return

    if kind == "meeting":
        text = (
            "➕ **YANGI UCHRASHUV**\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "Uchrashuv haqida ma'lumot yuboring:\n\n"
            "Sarlavha, ishtirokchi, sana/vaqt, manzilni bir matn\n"
            "yoki ovoz xabarda yuborishingiz mumkin.\n\n"
            "_Misollar:_\n"
            "_• \"Ertaga soat 12:00 da Dinislam bilan biznes forum\"_\n"
            "_• \"Juma 15:00 da Olim aka bilan byudjet uchrashuvi\"_\n"
            "_• \"28-may ertalab 10:00 da jamoa stand-up\"_\n\n"
            "Bot ma'lumotni tushunib, iCloud kalendariga ham\n"
            "avtomatik sinxronlaydi."
        )
        await _safe_answer(
            query.message, text, parse_mode="Markdown",
            reply_markup=single_back_keyboard("nav_new", text="✕ Bekor"),
        )
        return

    if kind == "reminder":
        await _newreminder_start(query.message, state)
        return

    prompts = {
        "voice": ("🎙 **Ovozdan vazifa**\n\nMikrofon tugmasini bosib o'zbekcha gapiring. "
                  "Men transkripsiya qilib, vazifani tushunaman."),
        "polish": ("✏️ **Matn tahrirlash**\n\nXabaringizni yuboring va aytib qo'ying — "
                   "kimga, qanday tonda. Misol:\n_\"Aziz akaga rasmiy qil: ertaga hisobot tayyor\"_"),
    }
    await _safe_answer(
        query.message,
        prompts.get(kind, "Matn yoki ovoz yuboring."),
        parse_mode="Markdown",
        reply_markup=single_back_keyboard("nav_new"),
    )


def _format_task_card(t: dict, idx: int = None, show_status: bool = True) -> str:
    """Compact task card for the main task drill-down screen."""
    badge = _PRIORITY_BADGE.get(t.get("priority", "P2"), "🔵")
    priority_name = _PRIORITY_LABEL_UZ.get(t.get("priority", "P2"), "Rejadagi")
    status = t.get("status", "todo")
    status_uz = _STATUS_LABEL_UZ.get(status, status)
    deadline_label, _overdue = _format_deadline_short(t.get("deadline"))
    if not t.get("deadline"):
        deadline_label = "belgilanmagan"
    title = (t.get("title") or "—").strip()
    assignee = (t.get("assignee") or "").strip() or "belgilanmagan"

    num_prefix = f"{idx}. " if idx is not None else ""
    meta = [f"👤 {assignee}", f"⚡ {priority_name}"]
    if show_status and status != "todo":
        meta.append(status_uz)
    return "\n".join([
        f"{badge} {num_prefix}**{title}**",
        "",
        " · ".join(meta),
        f"⏰ Muddat: {deadline_label}",
    ])


def _format_task_detail_card(t: dict, idx: int = None) -> str:
    """Full task card for ⋯ Batafsil."""
    badge = _PRIORITY_BADGE.get(t.get("priority", "P2"), "🔵")
    priority_name = _PRIORITY_LABEL_UZ.get(t.get("priority", "P2"), "Rejadagi")
    status = t.get("status", "todo")
    status_uz = _STATUS_LABEL_UZ.get(status, status)
    status_emoji = _STATUS_EMOJI.get(status, "•")
    deadline_label, _overdue = _format_deadline_short(t.get("deadline"))
    if not t.get("deadline"):
        deadline_label = "belgilanmagan"
    title = (t.get("title") or "—").strip()
    assignee = (t.get("assignee") or "").strip() or "belgilanmagan"
    description = (t.get("description") or "").strip()
    tags = t.get("tags") or []
    num_prefix = f"{idx}. " if idx is not None else ""

    lines = [
        f"{badge} {num_prefix}**{title}**",
        "",
        f"👤 Ijrochi: {assignee}",
        f"⏰ Muddat: {deadline_label}",
        f"⚡ Ustuvorlik: {priority_name}",
        f"{status_emoji} Holat: {status_uz}",
    ]
    if description:
        lines.append(f"📝 Tavsif: {description}")
    if tags:
        lines.append(f"🏷 Teglar: {', '.join(tags)}")
    if t.get("recurrence_rule"):
        lines.append(f"🔁 Takroriy: {_format_recurrence_label(t.get('recurrence_rule'))}")
    return "\n".join(lines)


_MAX_TITLE_COMPACT = 50  # mobil ekranga sig'ish uchun


def _truncate(s: str, n: int = _MAX_TITLE_COMPACT) -> str:
    if not s:
        return "—"
    s = s.strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _format_recurrence_label(rule: str | None) -> str:
    labels = {
        "daily": "har kuni",
        "weekly": "har hafta",
        "monthly": "har oy",
        "quarterly": "har chorak",
        "yearly": "har yil",
    }
    return labels.get(rule or "", rule or "—")



_TASKS_PER_PAGE = 10




def _task_deadline_chip(task: dict) -> str:
    """Compact deadline label for list view."""
    if task.get("status") == "done":
        return "Bajarilgan"
    deadline = task.get("deadline")
    if not deadline:
        return "Muddatsiz"
    try:
        dt = datetime.fromisoformat(deadline).astimezone(database.TZ)
    except (ValueError, TypeError):
        return str(deadline)
    now = datetime.now(database.TZ)
    if dt < now:
        return "O'tgan"
    if dt.date() == now.date():
        return f"Bugun {dt.strftime('%H:%M')}"
    if (now + timedelta(days=1)).date() == dt.date():
        return f"Ertaga {dt.strftime('%H:%M')}"
    return dt.strftime("%d-%m %H:%M")


def _format_tasks_compact(
    tasks: list[dict],
    label: str,
    show_status: bool = False,
    stats: dict | None = None,
    page: int = 1,
) -> str:
    """Tasks screen — block-style cards with professional spacing.

    Icon palette (from prior design):
      📋 page · 📌 stats · ⏳ unfinished · ✅ done
      Per-task badge: 🔴 overdue/urgent · 🟠 important · 🟡 today · ⚪ routine · ✅ done
      Detail line icons: 👤 ijrochi · ⏳ muddat · ⚡/⭐/🔹/✅ muhimlik

    Spacing rules: 2 blank lines around dividers, 1 blank between items,
    1 blank between task title and its detail block, 6-space indent for details.
    """
    del show_status
    DIVIDER = "━" * 20

    per_page = _TASKS_PER_PAGE
    total = len(tasks)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_tasks = tasks[start : start + per_page]

    lines: list[str] = [
        "📌  **VAZIFALAR**",
        "",
        f"Ko'rinish · {label}",
        f"Natija · {total} ta",
    ]
    if total_pages > 1:
        lines.append(f"Sahifa · {page} / {total_pages}")

    if total == 0:
        lines.extend([
            "",
            DIVIDER,
            "",
            "Bu bo'limda hozircha vazifa yo'q.",
            "",
            "_Yangi vazifa qo'shish yoki boshqa filter tanlash mumkin._",
        ])
        return "\n".join(lines)

    lines.extend(["", DIVIDER, ""])

    # UMUMIY HOLAT — two stat rows, no blank between them (related data)
    if stats:
        lines.extend([
            "📌  **UMUMIY HOLAT**",
            "",
            f"Jami {stats['total']}   ·   Aktiv {stats['active']}   ·   Bajarilgan {stats['done']}",
            f"Shoshilinch {stats['urgent']}   ·   Muhim {stats['important']}   ·   O'tgan {stats['overdue']}   ·   🚧 To'silgan {stats.get('blocked', 0)}",
            "",
            DIVIDER,
            "",
        ])

    unfinished = [t for t in page_tasks if t.get("status") != "done"]
    done = [t for t in page_tasks if t.get("status") == "done"]

    def _task_badge(task: dict) -> str:
        """Per-task badge — done > blocked > overdue/urgent > important > today > routine."""
        if task.get("status") == "done":
            return "✅"
        if task.get("status") == "blocked":
            return "🚧"  # stuck — needs unblocking
        priority = task.get("priority", "P2")
        deadline = task.get("deadline")
        is_overdue = False
        is_today = False
        if deadline:
            try:
                dt = datetime.fromisoformat(deadline).astimezone(database.TZ)
                now = datetime.now(database.TZ)
                is_overdue = dt < now
                is_today = (not is_overdue) and dt.date() == now.date()
            except (ValueError, TypeError):
                pass
        if is_overdue or priority == "P0":
            return "🔴"
        if priority == "P1":
            return "🟠"
        if is_today:
            return "🟡"
        return "⚪"

    def _muhimlik_icon(priority: str) -> str:
        return {"P0": "⚡", "P1": "⭐", "P2": "🔹", "P3": "🔹"}.get(priority, "🔹")

    def _task_card_lines(task: dict, num: int) -> list[str]:
        """Render one unfinished task as a 5-line card (title, blank, 3 details)."""
        title = (task.get("title") or "—").strip()
        badge = _task_badge(task)
        assignee = ((task.get("assignee") or "Belgilanmagan").strip() or "Belgilanmagan")
        assignee = assignee[0].upper() + assignee[1:]
        deadline = _task_deadline_chip(task)
        muhimlik_name = _PRIORITY_LABEL_UZ.get(task.get("priority", "P2"), "Rejadagi")
        muhimlik_emoji = _muhimlik_icon(task.get("priority", "P2"))
        # Pad labels to a stable visual column. Labels: Ijrochi:(8) Muddat:(7) Muhimlik:(9)
        # → pad all to width 9 + 4 spaces of breathing room before the value.
        return [
            f"{num}.  {badge}  {title}",
            "",
            f"      👤  Ijrochi:     {assignee}",
            f"      ⏳  Muddat:      {deadline}",
            f"      {muhimlik_emoji}  Muhimlik:    {muhimlik_name}",
        ]

    abs_idx = start

    if unfinished:
        lines.extend([f"⏳  **BAJARILMAGAN**   ·   {len(unfinished)} ta", ""])
        cards: list[list[str]] = []
        for t in unfinished:
            abs_idx += 1
            cards.append(_task_card_lines(t, abs_idx))
        # Join cards with a single blank line between each.
        joined = []
        for i, card in enumerate(cards):
            if i:
                joined.append("")
            joined.extend(card)
        lines.extend(joined)
        lines.append("")

    if done:
        if unfinished:
            lines.extend([DIVIDER, ""])
        lines.extend([f"✅  **BAJARILGAN**   ·   {len(done)} ta", ""])
        done_cards: list[list[str]] = []
        for t in done:
            abs_idx += 1
            title = (t.get("title") or "—").strip()
            updated_at = t.get("updated_at")
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at).astimezone(database.TZ)
                    date_str = f"{dt.day}-{UZ_MONTHS_FULL[dt.month - 1]}"
                except (ValueError, TypeError):
                    date_str = "—"
            else:
                date_str = "—"
            assignee = ((t.get("assignee") or "Belgilanmagan").strip() or "Belgilanmagan")
            assignee = assignee[0].upper() + assignee[1:]
            done_cards.append([
                f"{abs_idx}.   {title}",
                f"        📅  Yopildi:    {date_str}",
                f"        👤  Ijrochi:    {assignee}",
            ])
        for i, card in enumerate(done_cards):
            if i:
                lines.append("")
            lines.extend(card)
        lines.append("")

    lines.extend([
        DIVIDER,
        "",
        "_Vazifa raqamini bosing — to'liq ma'lumot._",
    ])
    return "\n".join(lines).rstrip()


def tasks_compact_keyboard(
    tasks: list[dict],
    current_filter: str = "active",
    page: int = 1,
) -> InlineKeyboardMarkup:
    """Tasks screen inline keyboard — DRILL-DOWN va sahifalash uchun.

    Filter chiplari va Yangi/Qidirish tugmalari **olib tashlangan** —
    ular allaqachon pastdagi reply kbd (section)'da bor.
    """
    per_page = _TASKS_PER_PAGE
    total = len(tasks)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_tasks = tasks[start : start + per_page]

    rows: list[list[InlineKeyboardButton]] = []

    nums = [
        InlineKeyboardButton(text=str(start + i + 1), callback_data=f"taskopen:{t['id']}")
        for i, t in enumerate(page_tasks)
    ]
    if nums:
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i+5])

    if total_pages > 1:
        pag_row: list[InlineKeyboardButton] = []
        if page > 1:
            pag_row.append(InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"taskfilter:{current_filter}:{page-1}",
            ))
        if page < total_pages:
            pag_row.append(InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"taskfilter:{current_filter}:{page+1}",
            ))
        if pag_row:
            rows.append(pag_row)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


_REMINDERS_PER_PAGE = 10


def _reminder_status_label(status: str | None) -> str:
    return {
        "scheduled": "Rejalangan",
        "sent": "Yuborilgan",
        "done": "Bajarilgan",
        "cancelled": "Bekor qilingan",
    }.get(status or "scheduled", status or "Rejalangan")


def _reminder_status_icon(reminder: dict) -> str:
    status = reminder.get("status", "scheduled")
    if status == "done":
        return "✅"
    if status == "sent":
        return "📤"
    if status == "cancelled":
        return "❌"
    dt = _parse_dt_safe(reminder.get("remind_at"))
    if dt and dt < datetime.now(database.TZ):
        return "🔴"
    if dt and dt.date() == datetime.now(database.TZ).date():
        return "🟡"
    return "⏰"


def _reminder_time_chip(reminder: dict) -> str:
    dt = _parse_dt_safe(reminder.get("remind_at"))
    if not dt:
        return reminder.get("remind_at") or "—"
    now = datetime.now(database.TZ)
    if dt.date() == now.date():
        return f"Bugun {dt.strftime('%H:%M')}"
    if dt.date() == (now + timedelta(days=1)).date():
        return f"Ertaga {dt.strftime('%H:%M')}"
    return f"{dt.day}-{UZ_MONTHS_FULL[dt.month - 1]} {dt.strftime('%H:%M')}"


def _format_reminder_card(reminder: dict, idx: int | None = None) -> str:
    title = (reminder.get("title") or "—").strip()
    prefix = f"{idx}.  " if idx is not None else ""
    status = _reminder_status_label(reminder.get("status"))
    repeat = _format_recurrence_label(reminder.get("recurrence_rule")) if reminder.get("recurrence_rule") else "bir martalik"
    lines = [
        f"{prefix}{_reminder_status_icon(reminder)} **{title}**",
        "",
        f"⏰ Vaqt: {_reminder_time_chip(reminder)}",
        f"🔁 Takror: {repeat}",
        f"📌 Holat: {status}",
    ]
    note = (reminder.get("note") or "").strip()
    if note:
        lines.append(f"📝 Izoh: {note}")
    return "\n".join(lines)


def _format_reminders_compact(
    reminders: list[dict],
    label: str,
    stats: dict | None = None,
    page: int = 1,
) -> str:
    divider = "━" * 20
    per_page = _REMINDERS_PER_PAGE
    total = len(reminders)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = reminders[start:start + per_page]

    lines = [
        "⏰  **ESLATMALAR**",
        "",
        f"Ko'rinish · {label}",
        f"Natija · {total} ta",
    ]
    if total_pages > 1:
        lines.append(f"Sahifa · {page} / {total_pages}")
    if stats:
        lines.extend([
            "",
            divider,
            "",
            "📌  **UMUMIY HOLAT**",
            "",
            f"Rejalangan {stats['scheduled']}   ·   Bugun {stats['today']}   ·   O'tgan {stats['overdue']}",
            f"Yuborilgan {stats['sent']}   ·   Takroriy {stats['recurring']}",
        ])
    if not page_items:
        lines.extend([
            "",
            divider,
            "",
            "Bu bo'limda hozircha eslatma yo'q.",
            "",
            "_Yangi eslatma qo'shish uchun pastdagi tugmadan foydalaning._",
        ])
        return "\n".join(lines)

    lines.extend(["", divider, ""])
    for offset, reminder in enumerate(page_items, start=1):
        if offset > 1:
            lines.append("")
        lines.append(_format_reminder_card(reminder, idx=start + offset))
    lines.extend(["", divider, "", "_Eslatma raqamini bosing — boshqaruv ochiladi._"])
    return "\n".join(lines).rstrip()


def reminders_compact_keyboard(
    reminders: list[dict],
    current_filter: str = "upcoming",
    page: int = 1,
) -> InlineKeyboardMarkup | None:
    per_page = _REMINDERS_PER_PAGE
    total = len(reminders)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = reminders[start:start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    nums = [
        InlineKeyboardButton(text=str(start + i + 1), callback_data=f"remopen:{r['id']}")
        for i, r in enumerate(page_items)
    ]
    if nums:
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"remfilter:{current_filter}:{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"remfilter:{current_filter}:{page + 1}"))
        if nav:
            rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def reminder_detail_menu(reminder: dict) -> InlineKeyboardMarkup:
    rid = reminder["id"]
    if reminder.get("status") in {"done", "sent"}:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="↺ Qayta eslat", callback_data=f"remsnooze:{rid}:1d"),
                InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"remdel:{rid}"),
            ],
            [InlineKeyboardButton(text="⬅️ Ro'yxatga", callback_data="remfilter:upcoming")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Bajarildi", callback_data=f"remdone:{rid}"),
            InlineKeyboardButton(text="⏰ 15 daq", callback_data=f"remsnooze:{rid}:15m"),
        ],
        [
            InlineKeyboardButton(text="🕐 1 soat", callback_data=f"remsnooze:{rid}:1h"),
            InlineKeyboardButton(text="📅 Ertaga", callback_data=f"remsnooze:{rid}:1d"),
        ],
        [
            InlineKeyboardButton(text="✏️ Matn", callback_data=f"remedit:{rid}:title"),
            InlineKeyboardButton(text="📆 Vaqt", callback_data=f"remedit:{rid}:time"),
        ],
        [
            InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"remdel:{rid}"),
            InlineKeyboardButton(text="⬅️ Ro'yxatga", callback_data="remfilter:upcoming"),
        ],
    ])


async def _load_reminders_for_filter(filt: str) -> tuple[list[dict], str]:
    if filt == "today":
        return await database.list_today_reminders(limit=200), "Bugungi eslatmalar"
    if filt == "sent":
        return await database.list_reminders(status_in=["sent", "done"], limit=200), "Yuborilgan / bajarilgan"
    if filt == "all":
        return await database.list_reminders(limit=200), "Barchasi"
    return await database.list_reminders(status_in=["scheduled"], limit=200), "Keyingi eslatmalar"


async def _render_reminders_for_filter(
    message: Message,
    filt: str = "upcoming",
    page: int = 1,
    edit_existing: bool = False,
) -> None:
    reminders, label = await _load_reminders_for_filter(filt)
    stats = await database.reminders_overview()
    text = _format_reminders_compact(reminders, label, stats=stats, page=page)
    kb = reminders_compact_keyboard(reminders, current_filter=filt, page=page)
    if edit_existing:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


# ─────────────────────── NOTES (Qaydlar) RENDERING ───────────────────────

def _format_notes_compact(notes: list[dict], label: str,
                            inbox_count: int = 0, page: int = 1) -> str:
    """One-screen compact list — card per note with source badge + preview."""
    DIVIDER = "━" * 20
    head = [f"📝 **NOTES · {label.upper()}**", ""]
    head.append(f"📥 Inbox: **{inbox_count}** ta qayta ishlanmagan")
    head.append("")
    head.append(DIVIDER)
    head.append("")
    if not notes:
        head.append("_Hozircha bu bo'limda note yo'q._")
        head.append("")
        head.append("Tezkor qo'shish: `/qayd <matn>` yoki ovoz orqali "
                     "_\"qayd qil: ...\"_.")
        return "\n".join(head).rstrip()

    per_page = _NOTES_PER_PAGE
    total = len(notes)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = notes[start:start + per_page]

    lines: list[str] = list(head)
    for i, n in enumerate(page_items, start=start + 1):
        title = (n.get("title") or _derive_note_title_fallback(n.get("content", "")))
        badge = _NOTES_SOURCE_BADGE.get(n.get("source", "manual"), "📝")
        when = _short_local_date(n.get("created_at"))
        meta = f"   📅 {when} · {badge}"
        if n.get("source_chat"):
            meta += f" · _{n['source_chat']}_"
        preview = (n.get("content") or "").strip().replace("\n", " ")
        if len(preview) > 120:
            preview = preview[:117] + "…"
        lines.append(f"**{i}. {title[:70]}**")
        lines.append(meta)
        if preview and preview != title:
            lines.append(f"   _{_escape_markdown(preview)}_")
        lines.append("")
    if total_pages > 1:
        lines.append(f"_Sahifa {page}/{total_pages}_")
    return "\n".join(lines).rstrip()


def _derive_note_title_fallback(content: str) -> str:
    """Mirror of database._derive_title for display use only."""
    first = next((ln.strip() for ln in (content or "").splitlines() if ln.strip()), "")
    if not first:
        return "(bo'sh note)"
    return first if len(first) <= 60 else first[:59] + "…"


def _short_local_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso).astimezone(database.TZ)
        return dt.strftime("%d-%m %H:%M")
    except (TypeError, ValueError):
        return "—"


def notes_compact_keyboard(notes: list[dict], current_filter: str = "inbox",
                            page: int = 1) -> InlineKeyboardMarkup | None:
    """Inline numbered keyboard + pagination — matches reminders pattern."""
    per_page = _NOTES_PER_PAGE
    total = len(notes)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_items = notes[start:start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    nums = [
        InlineKeyboardButton(text=str(start + i + 1), callback_data=f"noteopen:{n['id']}")
        for i, n in enumerate(page_items)
    ]
    if nums:
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
    # Filter pills
    rows.append([
        InlineKeyboardButton(text="📥 Inbox", callback_data="notesfilter:inbox"),
        InlineKeyboardButton(text="⚙️ Ishlangan", callback_data="notesfilter:processed"),
        InlineKeyboardButton(text="📦 Arxiv", callback_data="notesfilter:archived"),
    ])
    if total_pages > 1:
        nav: list[InlineKeyboardButton] = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"notesfilter:{current_filter}:{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="➡️", callback_data=f"notesfilter:{current_filter}:{page + 1}"))
        if nav:
            rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def note_detail_menu(note: dict) -> InlineKeyboardMarkup:
    """Per-note action menu — Tahlil / Vazifaga / Eslatmaga / Arxiv / O'chir."""
    nid = note["id"]
    status = note.get("status", "inbox")
    rows = []
    # If already processed, expose Reopen instead of conversion actions.
    if status == "processed":
        rows.append([
            InlineKeyboardButton(text="🔁 Inbox'ga qaytar",
                                  callback_data=f"noterestore:{nid}"),
        ])
    elif status == "archived":
        rows.append([
            InlineKeyboardButton(text="🔁 Tiklash", callback_data=f"noterestore:{nid}"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🤖 Tahlil qil", callback_data=f"noteanalyze:{nid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="📝 Vazifaga", callback_data=f"notetotask:{nid}"),
            InlineKeyboardButton(text="⏰ Eslatmaga", callback_data=f"notetorem:{nid}"),
        ])
        rows.append([
            InlineKeyboardButton(text="📦 Arxiv", callback_data=f"notearchive:{nid}"),
            InlineKeyboardButton(text="🗑 O'chir", callback_data=f"notedelete:{nid}"),
        ])
    rows.append([
        InlineKeyboardButton(text="⬅️ Ro'yxatga",
                              callback_data=f"notesfilter:{status if status != 'archived' else 'archived'}"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_notes_for_filter(filt: str) -> tuple[list[dict], str]:
    label_map = {
        "inbox": "Inbox",
        "processed": "Qayta ishlangan",
        "archived": "Arxiv",
    }
    if filt not in label_map:
        filt = "inbox"
    notes = await database.list_notes(status=filt, limit=200)
    return notes, label_map[filt]


async def _render_notes_for_filter(message: Message, filt: str = "inbox",
                                     page: int = 1,
                                     edit_existing: bool = False) -> None:
    notes, label = await _load_notes_for_filter(filt)
    inbox_count = await database.count_notes_in_status("inbox")
    text = _format_notes_compact(notes, label, inbox_count=inbox_count, page=page)
    kb = notes_compact_keyboard(notes, current_filter=filt, page=page)
    if edit_existing:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


async def _compute_tasks_overview() -> dict:
    """Counts for the UMUMIY HOLAT block — stable across filters."""
    all_tasks = await database.list_tasks(limit=500)
    # "Active" = open work = todo + in_progress + blocked. Blocked is open work
    # that's stuck — it MUST stay visible (previously it only showed in Barchasi).
    active = [t for t in all_tasks if t.get("status") in ("todo", "in_progress", "blocked")]
    done = [t for t in all_tasks if t.get("status") == "done"]
    now = datetime.now(database.TZ)

    def _overdue(task: dict) -> bool:
        d = task.get("deadline")
        if not d:
            return False
        try:
            return datetime.fromisoformat(d).astimezone(database.TZ) < now
        except (ValueError, TypeError):
            return False

    overdue = [t for t in active if _overdue(t)]
    urgent = [t for t in active if t.get("priority") == "P0"]
    important = [t for t in active if t.get("priority") == "P1"]
    blocked = [t for t in active if t.get("status") == "blocked"]
    return {
        "total": len(all_tasks),
        "active": len(active),
        "done": len(done),
        "overdue": len(overdue),
        "urgent": len(urgent),
        "important": len(important),
        "blocked": len(blocked),
    }


async def _render_tasks_for_filter(message: Message, filt: str = "active",
                                    page: int = 1, edit_existing: bool = False) -> None:
    """Render the tasks screen. Pagination = 10 items per page.

    If edit_existing=True (called from a callback), edits the existing message
    instead of sending a new one — keeps the chat tidy.
    """
    if filt == "active":
        tasks = await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=200)
        label = "Aktiv vazifalar"
    elif filt == "today":
        tasks = await database.list_today_tasks()
        label = "Bugungi vazifalar"
    elif filt == "important":
        all_active = await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=200)
        tasks = [t for t in all_active if t.get("priority") in ("P0", "P1")]
        label = "Muhim vazifalar"
    elif filt == "urgent":
        all_active = await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=200)
        tasks = [t for t in all_active if t.get("priority") == "P0"]
        label = "Shoshilinch"
    elif filt == "overdue":
        tasks = await database.list_overdue_tasks()
        label = "Muddati o'tgan"
    elif filt == "done":
        tasks = await database.list_tasks(status_in=["done"], limit=200)
        label = "Bajarilgan"
    elif filt == "recurring":
        tasks = await database.list_recurring_tasks(limit=100)
        label = "Takroriy vazifalar"
    elif filt == "all":
        tasks = await database.list_tasks(limit=200)
        label = "Barchasi"
    else:
        tasks = await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=200)
        label = "Aktiv vazifalar"

    # `_format_tasks_compact` cards'ni "BAJARILMAGAN" → "BAJARILGAN" tartibida chiqaradi.
    # Tugma raqamlari esa `tasks_compact_keyboard`'da page_tasks ketma-ketligida tuziladi.
    # Mos kelishi uchun bu yerda done'larni oxiriga surib qo'yamiz (stable sort —
    # bir status ichida priorityyaki created_at tartibi saqlanadi).
    tasks.sort(key=lambda t: 1 if t.get("status") == "done" else 0)

    stats = await _compute_tasks_overview()
    text = _format_tasks_compact(tasks, label, stats=stats, page=page)
    kb = tasks_compact_keyboard(tasks, current_filter=filt, page=page)

    if edit_existing:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass  # message likely too old to edit; fall through to send new
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message, state: FSMContext | None = None) -> None:
    # Section FSM state — "Bugun"/"Barchasi" kabi noyob bo'lmagan labellar
    # uchun kontekst aniqlash. Reply kbd ni Vazifalar bo'limiga almashtirish.
    if state is not None:
        await state.set_state(SectionFSM.in_tasks)
    await message.answer(
        "📌 **VAZIFALAR**", parse_mode="Markdown",
        reply_markup=tasks_section_reply_keyboard(),
    )
    await _render_tasks_for_filter(message, "active")


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.set_state(SectionFSM.in_reminders)
    await message.answer(
        "⏰ **ESLATMALAR**", parse_mode="Markdown",
        reply_markup=reminders_section_reply_keyboard(),
    )
    await _render_reminders_for_filter(message, "upcoming")


@router.callback_query(F.data.startswith("remfilter:"))
async def cb_reminder_filter(query: CallbackQuery) -> None:
    parts = query.data.split(":")
    filt = parts[1] if len(parts) > 1 and parts[1] else "upcoming"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    await query.answer()
    await _render_reminders_for_filter(query.message, filt, page=page, edit_existing=True)


# ─────────────────────── NOTES — COMMANDS + CALLBACKS ───────────────────────


@router.message(Command("notes"))
@router.message(Command("qaydlar"))
async def cmd_notes(message: Message, state: FSMContext | None = None) -> None:
    """Notes bo'limi — Inbox / Ishlangan / Arxiv. Default: Inbox."""
    if state is not None:
        await state.set_state(SectionFSM.in_notes)
    await message.answer(
        "📝 **NOTES**", parse_mode="Markdown",
        reply_markup=notes_section_reply_keyboard(),
    )
    await _render_notes_for_filter(message, "inbox")


@router.callback_query(F.data.startswith("notesfilter:"))
async def cb_notes_filter(query: CallbackQuery) -> None:
    parts = query.data.split(":")
    filt = parts[1] if len(parts) > 1 and parts[1] else "inbox"
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    if filt not in ("inbox", "processed", "archived"):
        filt = "inbox"
    await query.answer()
    await _render_notes_for_filter(query.message, filt, page=page, edit_existing=True)


@router.message(Command("qayd"))
async def cmd_qayd(message: Message, state: FSMContext | None = None) -> None:
    """Tezkor qayd qo'shish. `/qayd <matn>` → darrov inbox'ga saqlaydi.
    `/qayd` (bo'sh) → keyingi xabar qaydga aylanadi (one-shot FSM)."""
    text = (message.text or "").removeprefix("/qayd").removeprefix("/qaydlar").strip()
    if text:
        nid = await database.create_note({
            "content": text, "source": "command",
        })
        await _note_capture_reply(message, nid, "/qayd buyrug'i orqali")
        await _maybe_refresh_section(message, state, {"note": [nid]})
        return
    # Empty body — enter one-shot FSM
    if state is not None:
        await state.set_state(NoteCaptureFSM.awaiting_text)
    await message.answer(
        "📝 **YANGI QAYD**\n\nMatn yoki ovoz yuboring — bot uni Inbox'ga saqlaydi.\n"
        "Bekor qilish: /cancel",
        parse_mode="Markdown",
        reply_markup=single_back_keyboard("nav_notes"),
    )


# All reply-keyboard button labels (collected from the *BTN* constants above) —
# used to stop a tapped navigation button from being captured as a note.
_RESERVED_LABELS = {v for k, v in dict(globals()).items()
                    if "BTN" in k and isinstance(v, str) and v}


def _is_note_noise(text: str) -> bool:
    """True if `text` is NOT a real note: a slash command, or a tapped
    reply-keyboard button label (e.g. '⬅️ Asosiy menyu'). Prevents junk notes."""
    t = (text or "").strip()
    return (not t) or t.startswith("/") or t in _RESERVED_LABELS


@router.message(StateFilter(NoteCaptureFSM.awaiting_text), F.text | F.voice)
async def handle_note_capture(message: Message, state: FSMContext) -> None:
    """One-shot capture: next text/voice after /qayd or '➕ Yangi note' button."""
    content = await _get_text_or_transcribe(message)
    if not content:
        return
    content = content.strip()
    if not content:
        await message.answer("Bo'sh xabar — note yaratilmadi.")
        return
    # A tapped nav button or command isn't a note — cancel capture, let it route.
    if _is_note_noise(content):
        await state.clear()
        await message.answer("✕ Note kiritish bekor qilindi.", reply_markup=main_reply_keyboard())
        return
    await state.clear()
    source = "voice" if message.voice else "manual"
    nid = await database.create_note({"content": content, "source": source})
    await _note_capture_reply(message, nid,
                                "ovoz orqali" if source == "voice" else "qo'lda")
    # Restore section if we were in one
    await _maybe_refresh_section(message, state, {"note": [nid]})


async def _note_capture_reply(message: Message, note_id: str, source_hint: str) -> None:
    """Compact confirmation right after a note is captured."""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📥 Inbox'ga", callback_data="notesfilter:inbox"),
        InlineKeyboardButton(text="🤖 Hozir tahlil", callback_data=f"noteanalyze:{note_id}"),
    ]])
    await message.answer(
        f"📝 **Note saqlandi** · `{note_id}`\n_Manba: {source_hint}_",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.callback_query(F.data == "nav_notes")
async def cb_nav_notes(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await cmd_notes(query.message, state)


def _html_escape(s: str) -> str:
    """Minimal HTML escape for Telegram parse_mode=HTML. The allowed tag set
    is small (b, i, u, s, code, pre, a, blockquote, tg-spoiler), so we only
    need to neutralise &, <, > in user-supplied text."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))


def _sanitize_forward_html(html: str) -> str:
    """Make a forwarded message's aiogram html_text safe to RE-SEND inside our
    own <blockquote expandable>. Two constructs make Telegram reject the whole
    message (and then we'd fall back to showing raw tags — the reported
    'meaning changed' bug):

      1. <tg-emoji> — a bot can't resend a premium/custom emoji it doesn't own.
         Keep the inner unicode fallback char, drop the wrapper.
      2. Nested <blockquote> — Telegram forbids blockquote-in-blockquote, and we
         already wrap the body in one. Flatten any inner blockquote tags.
    """
    import re as _re
    if not html:
        return html
    html = _re.sub(r'<tg-emoji\b[^>]*>(.*?)</tg-emoji>', r'\1', html, flags=_re.S)
    html = _re.sub(r'</?blockquote\b[^>]*>', '', html)
    return html


def _format_note_detail(note: dict) -> tuple[str, str]:
    """Full-content card shown when a note is opened via noteopen:{id}.
    Returns (html_text, parse_mode). Uses Telegram's native <blockquote>
    so forwarded content renders as a real quote bubble — vertical bar +
    distinct background — and original bold/italic/links survive intact."""
    badge = _NOTES_SOURCE_BADGE.get(note.get("source", "manual"), "📝")
    when = _short_local_date(note.get("created_at"))
    status_uz = {
        "inbox": "📥 Inbox",
        "processed": "⚙️ Qayta ishlangan",
        "archived": "📦 Arxiv",
    }.get(note.get("status"), note.get("status") or "—")

    title = (note.get("title") or "Note").strip()[:80]
    parts: list[str] = [
        f"📝 <b>{_html_escape(title)}</b>",
        "",
        f"📅 {_html_escape(when)} · {_html_escape(badge)}",
    ]
    if note.get("source_chat"):
        parts.append(f"💬 Chat: <i>{_html_escape(note['source_chat'])}</i>")
    if note.get("source_author"):
        parts.append(f"👤 Muallif: <i>{_html_escape(note['source_author'])}</i>")
    parts.append(f"🔖 Holat: {_html_escape(status_uz)}")
    if note.get("converted_to_type") and note.get("converted_to_id"):
        link_label = "Vazifa" if note["converted_to_type"] == "task" else "Eslatma"
        parts.append(
            f"🔗 {link_label}: <code>{_html_escape(note['converted_to_id'])}</code>"
        )
    tags = note.get("tags") or []
    if tags:
        parts.append("🏷 " + ", ".join(f"#{_html_escape(str(t))}" for t in tags[:8]))
    parts.append("")

    # Body: prefer content_html (original Telegram entities preserved) for
    # forwards; fall back to plain content. Render inside a native
    # <blockquote expandable> so long quotes can be collapsed.
    html_body = note.get("content_html")
    plain = (note.get("content") or "").strip()
    if html_body and (note.get("source") == "forward" or html_body.strip()):
        body = _sanitize_forward_html(html_body.strip())
    else:
        body = _html_escape(plain)
    # Hard cap: Telegram message limit is ~4096 chars including markup;
    # blockquote'ning expandable variant uzun matnlarni o'rab qo'yadi.
    if len(body) > 3000:
        body = body[:3000] + "\n\n<i>…(qisqartirildi)</i>"
    parts.append(f"<blockquote expandable>{body}</blockquote>")
    return "\n".join(parts), "HTML"


@router.callback_query(F.data.startswith("noteopen:"))
async def cb_note_open(query: CallbackQuery) -> None:
    nid = query.data.split(":", 1)[1]
    note = await database.get_note(nid)
    if not note:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer()
    text, parse_mode = _format_note_detail(note)
    await _safe_answer(
        query.message,
        text,
        parse_mode=parse_mode,
        reply_markup=note_detail_menu(note),
    )


@router.callback_query(F.data.startswith("notearchive:"))
async def cb_note_archive(query: CallbackQuery) -> None:
    nid = query.data.split(":", 1)[1]
    ok = await database.archive_note(nid)
    if not ok:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer("📦 Arxivga ko'chirildi ✅")
    note = await database.get_note(nid)
    if note:
        text, parse_mode = _format_note_detail(note)
        try:
            await query.message.edit_text(
                text, parse_mode=parse_mode,
                reply_markup=note_detail_menu(note),
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("noterestore:"))
async def cb_note_restore(query: CallbackQuery) -> None:
    """Restore an archived or processed note back to inbox."""
    nid = query.data.split(":", 1)[1]
    ok = await database.update_note(nid, {
        "status": "inbox",
        "converted_to_id": None,
        "converted_to_type": None,
    })
    if not ok:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer("📥 Inbox'ga qaytarildi ✅")
    note = await database.get_note(nid)
    if note:
        text, parse_mode = _format_note_detail(note)
        try:
            await query.message.edit_text(
                text, parse_mode=parse_mode,
                reply_markup=note_detail_menu(note),
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("notedelete:"))
async def cb_note_delete(query: CallbackQuery) -> None:
    """Two-tap delete: first tap shows confirm, second tap (notedelconfirm:)
    actually deletes. Matches the safety pattern used for task deletes."""
    nid = query.data.split(":", 1)[1]
    note = await database.get_note(nid)
    if not note:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer()
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Ha, o'chirilsin", callback_data=f"notedelconfirm:{nid}"),
        InlineKeyboardButton(text="✕ Bekor", callback_data=f"noteopen:{nid}"),
    ]])
    title = (note.get("title") or "note")[:60]
    await query.message.answer(
        f"⚠️ **«{title}»** note o'chirilsinmi?\nBu amalni qaytarib bo'lmaydi.",
        parse_mode="Markdown",
        reply_markup=confirm_kb,
    )


@router.callback_query(F.data.startswith("notedelconfirm:"))
async def cb_note_delete_confirm(query: CallbackQuery) -> None:
    nid = query.data.split(":", 1)[1]
    ok = await database.delete_note(nid)
    if not ok:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer("🗑 O'chirildi ✅")
    try:
        await query.message.edit_text("🗑 Note o'chirildi.")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("notetotask:"))
async def cb_note_to_task(query: CallbackQuery) -> None:
    """Convert a note straight into a task. Title = note.title, description =
    note.content, deadline=NULL (user can edit afterwards), priority=P2,
    tags include `qayd:{note_id}` for back-reference."""
    nid = query.data.split(":", 1)[1]
    note = await database.get_note(nid)
    if not note:
        await query.answer("Note topilmadi", show_alert=True)
        return
    title = (note.get("title") or note.get("content", "Note'dan vazifa")).strip()[:200]
    description = (note.get("content") or "").strip()
    if description == title:
        description = None
    tags = list(note.get("tags") or []) + [f"note:{nid}"]
    tid = await database.create_task({
        "title": title,
        "description": description,
        "priority": "P2",
        "status": "todo",
        "tags": tags,
        "source": "note",
    })
    await database.mark_note_processed(nid, "task", tid)
    await query.answer("📝 Vazifa yaratildi ✅")
    task = await database.get_task(tid)
    if task:
        await _safe_answer(
            query.message,
            "📝 **Note vazifaga aylantirildi**\n\n" + _format_task_card(task),
            parse_mode="Markdown",
            reply_markup=task_inline_actions(task),
        )


@router.callback_query(F.data.startswith("notetorem:"))
async def cb_note_to_reminder(query: CallbackQuery, state: FSMContext) -> None:
    """Convert a note into a reminder. We don't know the remind_at time, so
    start the NewReminderFSM pre-filled with the note's content as title."""
    nid = query.data.split(":", 1)[1]
    note = await database.get_note(nid)
    if not note:
        await query.answer("Note topilmadi", show_alert=True)
        return
    title = (note.get("title") or note.get("content", "Note'dan eslatma")).strip()[:200]
    # Hop into the existing new-reminder flow with title pre-set; the user
    # picks the time, and on submit we run mark_note_processed via a tag.
    await state.set_state(NewReminderFSM.awaiting_time)
    await state.update_data(
        title=title,
        from_note_id=nid,
    )
    await query.answer()
    await query.message.answer(
        f"⏰ **YANGI ESLATMA**\n\nMavzu: «{title[:60]}»\n\n"
        "Eslatma vaqtini tanlang yoki yozing:\n"
        "_Misol: «ertaga 14:00», «1 soatdan keyin»_",
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("noteanalyze:"))
async def cb_note_analyze(query: CallbackQuery, state: FSMContext) -> None:
    """Send the note content to Claude (fast model) for a 2-line summary +
    one concrete next-action suggestion. The suggested action then flows
    through the existing create-confirm gate."""
    nid = query.data.split(":", 1)[1]
    note = await database.get_note(nid)
    if not note:
        await query.answer("Note topilmadi", show_alert=True)
        return
    await query.answer("🤖 Tahlil qilinmoqda...")
    content = (note.get("content") or "").strip()
    if not content:
        await query.message.answer("Bo'sh note — tahlil qilish mumkin emas.")
        return
    typing = asyncio.create_task(_keep_typing(query.bot, query.message.chat.id))
    try:
        directive = (
            "[INTERNAL] analyze_note\n\n"
            "Quyidagi qayd matni — uni qisqa tahlil qiling:\n"
            "  1. 1-2 satr xulosa (mavzu nima haqida)\n"
            "  2. 1 ta aniq next-action taklif (vazifa yaratish kerakmi? "
            "     eslatma kerakmi? hech narsa kerakmasmi?)\n"
            "Agar action kerak deb topsangiz — javob ichiga create_task "
            "yoki create_reminder action qo'shing. user_message qisqa "
            "Uzbek xulosa bo'lsin."
            f"\n\nQAYD:\n{content}"
        )
        response = await claude_service.process_message(
            "", internal_directive=directive, complexity="fast",
        )
    finally:
        typing.cancel()
    text = (response.get("user_message") or "").strip() or "_Tahlil bo'sh._"
    actions = response.get("actions", [])
    destructive = [a for a in actions if a.get("type") in _DESTRUCTIVE_ACTION_TYPES]
    # Show the analysis, then ask to CONFIRM before creating anything (no longer
    # auto-creates silently). Reuses the standard acts_confirm pipeline; the
    # _note_id lets it mark this note processed once the action is executed.
    await _safe_answer(query.message, f"🤖 **Tahlil:**\n\n{text}", parse_mode="Markdown")
    if destructive:
        preview = await _format_create_preview(destructive)
        await state.set_state(CreateActionConfirmFSM.awaiting)
        await state.update_data(
            pending_response={"actions": destructive,
                              "user_message": "✅ Tayyor.", "buttons": []},
            _prior_section=None,
            _note_id=nid,
        )
        confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Yarataman", callback_data="acts_confirm"),
            InlineKeyboardButton(text="✕ Yo'q", callback_data="acts_cancel"),
        ]])
        await _safe_answer(query.message, preview, parse_mode="Markdown", reply_markup=confirm_kb)


@router.message(Command("recurring"))
async def cmd_recurring(message: Message) -> None:
    tasks = await database.list_recurring_tasks(limit=30)
    text = _format_tasks_compact(tasks, "Takroriy vazifalar")
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=tasks_compact_keyboard(tasks, "all"))


@router.callback_query(F.data.startswith("taskfilter:"))
async def cb_task_filter(query: CallbackQuery) -> None:
    # Format: taskfilter:<key>  or  taskfilter:<key>:<page>
    parts = query.data.split(":")
    filt = parts[1] if len(parts) > 1 else "active"
    try:
        page = int(parts[2]) if len(parts) > 2 else 1
    except ValueError:
        page = 1
    await query.answer()
    await _render_tasks_for_filter(query.message, filt, page=page, edit_existing=True)



@router.message(StateFilter(TaskSearchFSM.awaiting_query), F.text | F.voice)
async def handle_task_search(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    await state.clear()
    query = message.text.strip()
    if not query:
        await message.answer("Qidiruv so'zi bo'sh bo'lmasin.", reply_markup=single_back_keyboard("taskfilter:active"))
        return
    found = (await database.search_all(query, limit=30)).get("tasks", [])
    text = _format_tasks_compact(found, f"Qidiruv: {query}")
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=tasks_compact_keyboard(found, "search"))


@router.message(StateFilter(ReminderSearchFSM.awaiting_query), F.text | F.voice)
async def handle_reminder_search(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    await state.clear()
    query = message.text.strip()
    if not query:
        await message.answer("Qidiruv so'zi bo'sh bo'lmasin.")
        return
    found = await database.search_reminders(query, limit=50)
    text = _format_reminders_compact(found, f"Qidiruv: {query}", page=1)
    await _safe_answer(
        message,
        text,
        parse_mode="Markdown",
        reply_markup=reminders_compact_keyboard(found, "search"),
    )


@router.callback_query(F.data.startswith("remopen:"))
async def cb_reminder_open(query: CallbackQuery) -> None:
    rid = query.data.split(":", 1)[1]
    reminder = await database.get_reminder(rid)
    if not reminder:
        await query.answer("Eslatma topilmadi", show_alert=True)
        return
    await query.answer()
    text = _format_reminder_card(reminder)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=reminder_detail_menu(reminder))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=reminder_detail_menu(reminder))


def _compute_snooze_time(choice: str) -> str | None:
    now = datetime.now(database.TZ)
    if choice == "15m":
        return (now + timedelta(minutes=15)).replace(second=0, microsecond=0).isoformat()
    if choice == "1h":
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0).isoformat()
    if choice == "1d":
        return (now + timedelta(days=1)).replace(second=0, microsecond=0).isoformat()
    return None


@router.callback_query(F.data.startswith("remdone:"))
async def cb_reminder_done(query: CallbackQuery) -> None:
    rid = query.data.split(":", 1)[1]
    ok = await database.complete_reminder(rid)
    if not ok:
        await query.answer("Eslatma topilmadi", show_alert=True)
        return
    await query.answer("Bajarildi ✅")
    reminder = await database.get_reminder(rid)
    if reminder:
        try:
            await query.message.edit_text(
                "✅ **ESLATMA BAJARILDI**\n" + _SEP + "\n\n" + _format_reminder_card(reminder),
                parse_mode="Markdown",
                reply_markup=reminder_detail_menu(reminder),
            )
        except TelegramBadRequest:
            await _safe_answer(query.message, "✅ Eslatma bajarildi.")


@router.callback_query(F.data.startswith("remsnooze:"))
async def cb_reminder_snooze(query: CallbackQuery) -> None:
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Xato format", show_alert=True)
        return
    rid, choice = parts[1], parts[2]
    remind_at = _compute_snooze_time(choice)
    if not remind_at:
        await query.answer("Snooze vaqti noto'g'ri", show_alert=True)
        return
    ok = await database.snooze_reminder(rid, remind_at)
    if not ok:
        await query.answer("Eslatma topilmadi", show_alert=True)
        return
    reminder = await database.get_reminder(rid)
    label = _reminder_time_chip(reminder or {"remind_at": remind_at})
    await query.answer(f"Keyingi eslatma: {label} ✅")
    if reminder:
        try:
            await query.message.edit_text(
                "⏰ **ESLATMA KO'CHIRILDI**\n" + _SEP + "\n\n" + _format_reminder_card(reminder),
                parse_mode="Markdown",
                reply_markup=reminder_detail_menu(reminder),
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("remdel:"))
async def cb_reminder_delete(query: CallbackQuery) -> None:
    rid = query.data.split(":", 1)[1]
    ok = await database.cancel_reminder(rid)
    if not ok:
        await query.answer("Eslatma topilmadi", show_alert=True)
        return
    await query.answer("O'chirildi ✅")
    try:
        await query.message.edit_text("🗑 Eslatma o'chirildi.", reply_markup=single_back_keyboard("remfilter:upcoming", "⬅️ Ro'yxatga"))
    except TelegramBadRequest:
        await _safe_answer(query.message, "🗑 Eslatma o'chirildi.")


@router.callback_query(F.data.startswith("remedit:"))
async def cb_reminder_edit(query: CallbackQuery, state: FSMContext) -> None:
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Xato format", show_alert=True)
        return
    rid, field = parts[1], parts[2]
    reminder = await database.get_reminder(rid)
    if not reminder:
        await query.answer("Eslatma topilmadi", show_alert=True)
        return
    if field not in {"title", "time"}:
        await query.answer("Maydon noma'lum", show_alert=True)
        return
    await state.set_state(ReminderEditFSM.awaiting_value)
    await state.update_data(reminder_id=rid, field=field)
    await query.answer()
    if field == "title":
        prompt = "✏️ **Eslatma matni**\n\nYangi matnni yuboring."
    else:
        prompt = (
            "📆 **Eslatma vaqti**\n\n"
            "Yangi vaqtni yuboring: `bugun 17:00`, `ertaga 09:00`, `2 soat`."
        )
    await _safe_answer(
        query.message,
        prompt,
        parse_mode="Markdown",
        reply_markup=single_back_keyboard(f"remopen:{rid}", text="✕ Bekor"),
    )


@router.message(StateFilter(ReminderEditFSM.awaiting_value), F.text | F.voice)
async def handle_reminder_edit_value(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    data = await state.get_data()
    rid = data.get("reminder_id")
    field = data.get("field")
    await state.clear()
    raw = (message.text or "").strip()
    if not rid or field not in {"title", "time"}:
        await message.answer("Holat yo'qoldi — qayta urinib ko'ring.")
        return
    if not raw:
        await message.answer("Bo'sh qiymat — bekor qilindi.")
        return
    if field == "title":
        ok = await database.update_reminder(rid, {"title": raw[:220]})
    else:
        parsed, reason = await _parse_deadline_natural(raw)
        if not parsed:
            await _safe_answer(message, _deadline_error_message(reason, kind="time"),
                               parse_mode="Markdown")
            return
        ok = await database.update_reminder(rid, {"remind_at": parsed, "status": "scheduled", "sent_at": None})
    if not ok:
        await message.answer("Eslatma topilmadi.")
        return
    reminder = await database.get_reminder(rid)
    await _safe_answer(
        message,
        "✅ Saqlandi\n\n" + _format_reminder_card(reminder),
        parse_mode="Markdown",
        reply_markup=reminder_detail_menu(reminder),
    )


@router.callback_query(F.data.startswith("taskopen:"))
async def cb_task_open(query: CallbackQuery) -> None:
    """Drill-down: show single task's full card + all action buttons."""
    tid = query.data.split(":", 1)[1]
    task = await database.get_task(tid)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer()
    text = _format_task_card(task)
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_task_card_kb_with_back(task))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                           reply_markup=_task_card_kb_with_back(task))


def _format_meeting_card(m: dict, show_date: bool = False) -> str:
    """Single meeting drill-down — Variant A (bot uslubi).

    Tartibi:
      🤝 UCHRASHUV (sarlavha)
      {title}
      ─── divider ───
      📌 MA'LUMOTLAR
      ⏰ Vaqt / 👥 Ishtirokchilar / 📍 Manzil / 📅 Kun tartibi
      📝 Bayonnoma / ☁️ iCloud
      ─── divider ───
      _izoh_
    """
    DIVIDER = "━" * 20

    # Time range label
    try:
        time_start = _meeting_time_label(m.get("datetime_start") or "", with_past_marker=True)
    except (ValueError, TypeError):
        time_start = m.get("datetime_start") or "—"
    end_iso = m.get("datetime_end")
    end_clock = ""
    if end_iso:
        try:
            end_dt = datetime.fromisoformat(end_iso).astimezone(database.TZ)
            end_clock = end_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
    time_label = f"{time_start} – {end_clock}" if end_clock else time_start

    title = (m.get("title") or "—").strip()
    if m.get("completed_at"):
        title = "✅ " + title  # attended/done marker
    parts = m.get("participants") or []
    if not parts:
        plabel = "belgilanmagan"
    elif len(parts) <= 3:
        plabel = ", ".join(parts)
    else:
        plabel = f"{', '.join(parts[:3])} (+{len(parts) - 3} nafar)"
    location = (m.get("location_or_link") or "").strip() or "belgilanmagan"
    agenda = (m.get("agenda") or "").strip() or "belgilanmagan"
    # Truncate long agenda inline; full text visible via Tahrirlash → Kun tartibi.
    if len(agenda) > 80:
        agenda = agenda[:77] + "…"

    # Protocol: stored in follow_up_actions (JSON list of strings, see Phase 7).
    fu = m.get("follow_up_actions") or []
    has_protocol = (isinstance(fu, list) and bool(fu)) or (isinstance(fu, str) and bool(fu.strip()))
    protocol_status = "tayyor" if has_protocol else "yo'q"

    # iCloud sync indicator: only meaningful when iCloud is enabled for this deployment.
    icloud_row: list[str] = []
    if config.ICLOUD_ENABLED:
        icloud_status = "sinxronlangan" if (m.get("icloud_uid") or "").strip() else "kutilmoqda"
        icloud_row = [f"      ☁️ iCloud:           {icloud_status}"]

    lines = [
        "🤝 **UCHRASHUV**",
        "",
        title,
        "",
        DIVIDER,
        "",
        "📌 **MA'LUMOTLAR**",
        "",
        f"      ⏰ Vaqt:             {time_label}",
        f"      👥 Ishtirokchilar:   {plabel}",
        f"      📍 Manzil:           {location}",
        f"      📅 Kun tartibi:      {agenda}",
        f"      📝 Bayonnoma:        {protocol_status}",
        *icloud_row,
        "",
        DIVIDER,
        "",
        "_Quyidagi tugmalardan birini tanlang._",
    ]
    return "\n".join(lines)


_MEETINGS_PER_PAGE = 10


def _meeting_time_label(iso_str: str, *, with_past_marker: bool = True) -> str:
    """Render 'Bugun 14:00' / 'Ertaga 10:00' / '29-may 16:00' / '23-may 11:00 · o'tgan'."""
    try:
        dt = datetime.fromisoformat(iso_str).astimezone(database.TZ)
    except (ValueError, TypeError):
        return iso_str or "—"
    now = datetime.now(database.TZ)
    same_day = dt.date() == now.date()
    tomorrow = dt.date() == (now + timedelta(days=1)).date()
    time_str = dt.strftime("%H:%M")
    if same_day:
        base = f"Bugun {time_str}"
    elif tomorrow:
        base = f"Ertaga {time_str}"
    else:
        base = f"{dt.day}-{UZ_MONTHS_FULL[dt.month - 1]} {time_str}"
    if with_past_marker and dt < now and not same_day:
        base += " · o'tgan"
    return base


async def _compute_meetings_overview() -> dict:
    """Counts used by 📌 UMUMIY HOLAT in the meetings screen.
    Stats are derived from the full DB, independent of the current filter,
    so the user sees a stable picture regardless of which chip is active.
    """
    now = datetime.now(database.TZ)
    today_start = database.TZ.localize(datetime.combine(now.date(), datetime.min.time()))
    today_end = today_start + timedelta(days=1)
    tomorrow_end = today_end + timedelta(days=1)
    week_end = today_start + timedelta(days=7)
    today = await database.list_meetings_in_window(today_start.isoformat(), today_end.isoformat())
    tomorrow = await database.list_meetings_in_window(today_end.isoformat(), tomorrow_end.isoformat())
    week = await database.list_meetings_in_window(today_start.isoformat(), week_end.isoformat())
    return {
        "today": len(today),
        "tomorrow": len(tomorrow),
        "week": len(week),
        "next_meeting": today[0] if today else (week[0] if week else None),
    }


def _format_meetings_compact(
    meetings: list[dict],
    label: str,
    stats: dict | None = None,
    page: int = 1,
) -> str:
    """Meetings screen — Vazifalar uslubidagi block-stil.

    Ikonkalar: 🤝 sahifa · 📌 stats · ⚡ eng yaqin · 📅 ro'yxat
                ⏰ vaqt · 👥 ishtirokchilar · 📍 manzil
    Bot uslubi: divider atrofida 1 bo'sh qator, item orasida 1 bo'sh qator,
                detal blok 6 ta probel indent, qiymat 12-ustunda.
    """
    DIVIDER = "━" * 20

    per_page = _MEETINGS_PER_PAGE
    total = len(meetings)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_meetings = meetings[start : start + per_page]

    lines: list[str] = [
        "🤝 **UCHRASHUVLAR**",
        "",
        f"Ko'rinish · {label}",
        f"Natija · {total} ta",
    ]
    if total_pages > 1:
        lines.append(f"Sahifa · {page} / {total_pages}")

    if total == 0:
        lines.extend([
            "",
            DIVIDER,
            "",
            "Bu bo'limda hozircha uchrashuv yo'q.",
            "",
            "➕ _Yangi uchrashuv yaratish yoki boshqa filter tanlash mumkin._",
        ])
        return "\n".join(lines)

    lines.extend(["", DIVIDER, ""])

    # UMUMIY HOLAT — stats independent of the current filter
    if stats:
        lines.extend([
            "📌 **UMUMIY HOLAT**",
            "",
            f"Jami {total}   ·   Bugun {stats['today']}   ·   Ertaga {stats['tomorrow']}   ·   Hafta {stats['week']}",
            "",
            DIVIDER,
            "",
        ])

    def _participants_label(m: dict) -> str:
        parts = m.get("participants") or []
        if not parts:
            return "belgilanmagan"
        if len(parts) <= 3:
            return ", ".join(parts)
        return f"{', '.join(parts[:3])} (+{len(parts) - 3} nafar)"

    def _meeting_card(m: dict, num: int | None) -> list[str]:
        """One meeting card with 1-line title + 3-line detail block."""
        title = (m.get("title") or "—").strip()
        if m.get("completed_at"):
            title = "✅ " + title  # attended/done marker (seen in O'tgan)
        prefix = f"{num}.  " if num is not None else ""
        return [
            f"{prefix}{title}",
            "",
            f"      ⏰ Vaqt:             {_meeting_time_label(m.get('datetime_start') or '')}",
            f"      👥 Ishtirokchilar:   {_participants_label(m)}",
            f"      📍 Manzil:           {(m.get('location_or_link') or '').strip() or 'belgilanmagan'}",
        ]

    # ENG YAQIN — surface the next upcoming meeting (only on page 1, only if non-past filter)
    next_meeting = stats.get("next_meeting") if stats else None
    if page == 1 and next_meeting:
        lines.append("⚡ **ENG YAQIN**")
        lines.append("")
        lines.extend(_meeting_card_pop(next_meeting))
        lines.extend(["", DIVIDER, ""])

    lines.extend(["📅 **UCHRASHUVLAR**", ""])
    cards = [_meeting_card(m, start + i + 1) for i, m in enumerate(page_meetings)]
    joined = []
    for i, card in enumerate(cards):
        if i:
            joined.append("")
        joined.extend(card)
    lines.extend(joined)
    lines.append("")

    lines.extend([
        DIVIDER,
        "",
        "_Uchrashuv raqamini bosing — bayonnoma va boshqa amallar._",
    ])
    return "\n".join(lines).rstrip()


def _meeting_card_pop(m: dict) -> list[str]:
    """Same shape as a list card but without a leading number (used in ENG YAQIN)."""
    title = (m.get("title") or "—").strip()
    parts = m.get("participants") or []
    if not parts:
        plabel = "belgilanmagan"
    elif len(parts) <= 3:
        plabel = ", ".join(parts)
    else:
        plabel = f"{', '.join(parts[:3])} (+{len(parts) - 3} nafar)"
    return [
        title,
        "",
        f"      ⏰ Vaqt:             {_meeting_time_label(m.get('datetime_start') or '')}",
        f"      👥 Ishtirokchilar:   {plabel}",
        f"      📍 Manzil:           {(m.get('location_or_link') or '').strip() or 'belgilanmagan'}",
    ]


def meetings_filter_keyboard(
    current: str = "week",
    meetings: list[dict] | None = None,
    page: int = 1,
    total_pages: int = 1,
) -> InlineKeyboardMarkup:
    """Meetings inline keyboard — DRILL-DOWN va sahifalash uchun.

    Filter chiplari, Yangi va Qidiruv tugmalari **olib tashlangan** —
    ular pastdagi reply kbd (section)'da bor.
    """
    rows: list[list[InlineKeyboardButton]] = []

    per_page = _MEETINGS_PER_PAGE
    visible = (meetings or [])[(page - 1) * per_page : page * per_page]
    if visible:
        nums = [
            InlineKeyboardButton(text=str((page - 1) * per_page + i + 1),
                                  callback_data=f"meetingopen:{m['id']}")
            for i, m in enumerate(visible)
        ]
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])

    if total_pages > 1:
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"meetingfilter:{current}:page:{page - 1}",
            ))
        if page < total_pages:
            nav.append(InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"meetingfilter:{current}:page:{page + 1}",
            ))
        if nav:
            rows.append(nav)

    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _render_meetings_for_filter(message: Message, filt: str = "week",
                                       edit_existing: bool = False,
                                       page: int = 1) -> None:
    now = datetime.now(database.TZ)
    today_start = database.TZ.localize(datetime.combine(now.date(), datetime.min.time()))

    if filt == "today":
        meetings = await database.list_today_meetings()
        label = "Bugungi"
    elif filt == "tomorrow":
        start = (today_start + timedelta(days=1)).isoformat()
        end = (today_start + timedelta(days=2)).isoformat()
        meetings = await database.list_meetings_in_window(start, end)
        label = "Ertangi"
    elif filt == "all":
        # Start from the beginning of today (not `now`) so a meeting scheduled
        # earlier today still shows — otherwise it silently drops out the moment
        # its start time passes, which reads as "my meeting disappeared".
        meetings = await database.list_meetings_in_window(
            today_start.isoformat(), (today_start + timedelta(days=30)).isoformat()
        )
        label = "Barchasi"
    elif filt == "past":
        start = (today_start - timedelta(days=7)).isoformat()
        end = now.isoformat()
        past = await database.list_meetings_in_window(start, end)
        # Newest past meetings first for relevance
        meetings = list(reversed(past))
        label = "O'tgan"
    else:  # week (default)
        # Window starts at the beginning of today (not `now`) so meetings earlier
        # today remain visible — the reported "voice shows it, the button doesn't"
        # bug came from `now` excluding an already-started meeting.
        meetings = await database.list_meetings_in_window(
            today_start.isoformat(), (today_start + timedelta(days=7)).isoformat()
        )
        label = "Haftalik"
        filt = "week"

    # Completed (attended) meetings leave the active/upcoming views — they stay
    # visible only under "O'tgan" (marked with ✅).
    if filt != "past":
        meetings = [m for m in meetings if not m.get("completed_at")]

    stats = await _compute_meetings_overview()
    total_pages = max(1, (len(meetings) + _MEETINGS_PER_PAGE - 1) // _MEETINGS_PER_PAGE)
    page = max(1, min(page, total_pages))

    text = _format_meetings_compact(meetings, label, stats=stats, page=page)
    kb = meetings_filter_keyboard(filt, meetings, page=page, total_pages=total_pages)

    if edit_existing:
        try:
            await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


@router.message(Command("meetings"))
async def cmd_meetings(message: Message, state: FSMContext | None = None) -> None:
    if state is not None:
        await state.set_state(SectionFSM.in_meetings)
    await message.answer(
        "🤝 **UCHRASHUVLAR**", parse_mode="Markdown",
        reply_markup=meetings_section_reply_keyboard(),
    )
    await _render_meetings_for_filter(message, filt="week")


@router.callback_query(F.data.startswith("meetingfilter:"))
async def cb_meeting_filter(query: CallbackQuery) -> None:
    parts = query.data.split(":")
    filt = parts[1]
    page = 1
    if len(parts) >= 4 and parts[2] == "page":
        try:
            page = max(1, int(parts[3]))
        except ValueError:
            page = 1
    await query.answer()
    await _render_meetings_for_filter(query.message, filt=filt, edit_existing=True, page=page)


@router.callback_query(F.data.startswith("meetingopen:"))
async def cb_meeting_open(query: CallbackQuery) -> None:
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer()
    text = _format_meeting_card(meeting, show_date=True)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=meeting_inline_actions(meeting))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=meeting_inline_actions(meeting))


async def _rerender_meeting_card(query: CallbackQuery, mid: str) -> None:
    meeting = await database.get_meeting(mid)
    if not meeting:
        return
    text = _format_meeting_card(meeting, show_date=True)
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=meeting_inline_actions(meeting))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("meeting_done:"))
async def cb_meeting_done(query: CallbackQuery) -> None:
    """✅ Bo'ldi — mark the meeting attended. It leaves Bugun/Haftalik and shows
    with a ✅ under O'tgan."""
    mid = query.data.split(":", 1)[1]
    if not await database.complete_meeting(mid):
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer("✅ Bo'ldi deb belgilandi")
    await _rerender_meeting_card(query, mid)


@router.callback_query(F.data.startswith("meeting_undone:"))
async def cb_meeting_undone(query: CallbackQuery) -> None:
    """↺ Undo the 'done' mark — returns the meeting to the active views."""
    mid = query.data.split(":", 1)[1]
    if not await database.uncomplete_meeting(mid):
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer("↺ Faol ro'yxatga qaytarildi")
    await _rerender_meeting_card(query, mid)


@router.message(StateFilter(MeetingSearchFSM.awaiting_query), F.text | F.voice)
async def handle_meeting_search(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    await state.clear()
    query = message.text.strip()
    if not query:
        await message.answer(
            "Qidiruv so'zi bo'sh bo'lmasin.",
            reply_markup=single_back_keyboard("meetingfilter:week"),
        )
        return
    found = (await database.search_all(query, limit=30)).get("meetings", [])
    text = _format_meetings_compact(found, f"Qidiruv: {query}")
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=meetings_filter_keyboard("search", found))


async def _generate_meeting_prep(meeting: dict) -> str:
    active = await database.list_tasks(status_in=["todo", "in_progress"], limit=20)
    task_lines = "\n".join(
        f"- {t['title']} ({t['priority']}, {t.get('deadline') or 'deadline yoq'})"
        for t in active[:10]
    )
    directive = (
        "[INTERNAL] meeting_prep_brief\n\n"
        "Generate a concise executive prep brief in O'zbek lotin. Include: "
        "1) meeting objective, 2) agenda, 3) decisions needed, 4) questions to ask, "
        "5) related open tasks. Use Markdown, max 18 lines.\n\n"
        f"MEETING:\n{meeting}\n\nACTIVE TASKS:\n{task_lines}"
    )
    response = await claude_service.process_message("", internal_directive=directive)
    text = (response.get("user_message") or "").strip()
    if text:
        return text
    agenda = meeting.get("agenda") or "Agenda kiritilmagan."
    prep = meeting.get("prep_notes") or "Oldingi materiallarni ko'rib chiqing."
    return f"🧠 **Prep brief: {meeting['title']}**\n\n• Agenda: {agenda}\n• Tayyorgarlik: {prep}"


@router.callback_query(F.data.startswith("meeting_prep:"))
async def cb_meeting_prep(query: CallbackQuery) -> None:
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer("Prep tayyorlanmoqda...")
    typing_task = asyncio.create_task(_keep_typing(query.bot, query.message.chat.id))
    try:
        text = await _generate_meeting_prep(meeting)
    finally:
        typing_task.cancel()
    await database.mark_meeting_prep_sent(mid)
    await _safe_answer(
        query.message,
        text,
        parse_mode="Markdown",
        reply_markup=single_back_keyboard("nav_meetings"),
    )


@router.callback_query(F.data.startswith("meeting_followup:"))
async def cb_meeting_followup(query: CallbackQuery, state: FSMContext) -> None:
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await state.set_state(MeetingFollowupFSM.awaiting_notes)
    await state.update_data(meeting_id=mid)
    await query.answer()
    await query.message.answer(
        "📝 **Action items chiqarish**\n\n"
        "Uchrashuvdan keyingi yozuv yoki ovozni yuboring. Men kim-nima-qachon formatida "
        "vazifalarni ajratib, keraklisini task qilib yarataman.",
        parse_mode="Markdown",
        reply_markup=single_back_keyboard("nav_meetings"),
    )


async def _run_meeting_followup_extraction(message: Message, meeting: dict, notes: str) -> None:
    directive = (
        "[INTERNAL] meeting_followup_extract\n\n"
        "Extract concrete action items from the principal's meeting notes. "
        "Create tasks only for items owned by the principal or clearly requiring tracking. "
        "For delegated items, set assignee. Use due dates if mentioned; otherwise deadline=null. "
        "Return JSON envelope with actions=[create_task...] and a short Uzbek confirmation.\n\n"
        f"MEETING:\n{meeting}\n\nNOTES:\n{notes}"
    )
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        response = await claude_service.process_message(notes, internal_directive=directive)
    finally:
        typing_task.cancel()

    ids_by_type = await _execute_actions(response.get("actions", []))
    created = ids_by_type.get("task", [])
    await database.update_meeting(
        meeting["id"],
        {
            "follow_up_actions": created,
            "followup_sent_at": database.now_iso(),
        },
    )
    text = (response.get("user_message") or "").strip()
    if not text:
        text = f"✅ {len(created)} ta action item yaratildi." if created else "Action item topilmadi."
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=tasks_compact_keyboard([]))


@router.message(StateFilter(MeetingFollowupFSM.awaiting_notes), F.text | F.voice)
async def handle_meeting_followup_text(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    data = await state.get_data()
    await state.clear()
    meeting = await database.get_meeting(data.get("meeting_id", ""))
    if not meeting:
        await message.answer("Uchrashuv topilmadi.")
        return
    await _run_meeting_followup_extraction(message, meeting, message.text)


@router.message(StateFilter(MeetingFollowupFSM.awaiting_notes), F.voice)
async def handle_meeting_followup_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    meeting = await database.get_meeting(data.get("meeting_id", ""))
    if not meeting:
        await message.answer("Uchrashuv topilmadi.")
        return
    if message.voice.file_size and message.voice.file_size > voice_service.MAX_AUDIO_BYTES:
        await message.answer("Ovoz juda katta. Iltimos, qisqaroq yuboring.")
        return
    file = await bot.get_file(message.voice.file_id)
    audio_io = await bot.download_file(file.file_path)
    audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
    transcript = await voice_service.transcribe(audio_bytes, filename="meeting-followup.ogg", language="uz")
    if not transcript:
        await message.answer("Ovozni o'qiy olmadim. Matn bilan qayta yuboring.")
        return
    await message.answer(f"_🎙 Tushundim:_ {_escape_markdown(transcript[:500])}", parse_mode="Markdown")
    await _run_meeting_followup_extraction(message, meeting, transcript)


@router.message(Command("calendar"))
async def cmd_calendar(message: Message) -> None:
    if not config.ICLOUD_ENABLED:
        await message.answer(
            "📅 iCloud kalendar **sozlanmagan**.\n\n"
            "Sozlash uchun:\n"
            "1. https://account.apple.com → Sign-In and Security\n"
            "2. App-Specific Passwords → Generate Password\n"
            "3. `.env` faylda `APPLE_ID` va `APPLE_APP_SPECIFIC_PASSWORD` to'ldiring\n"
            "4. Botni qayta ishga tushiring",
            parse_mode="Markdown",
            reply_markup=single_back_keyboard("nav_settings"),
        )
        return
    typing = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        ok, msg = await asyncio.to_thread(calendar_service.test_connection)
    finally:
        typing.cancel()
    emoji = "✅" if ok else "❌"
    await message.answer(
        f"{emoji} **iCloud kalendar**\n\n{msg}",
        parse_mode="Markdown",
        reply_markup=single_back_keyboard("nav_settings"),
    )


def _ascii_bar(value: int, max_value: int, width: int = 10) -> str:
    """Return a `width`-cell unicode bar showing value/max ratio."""
    if max_value <= 0:
        return "░" * width
    filled = round((value / max_value) * width)
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def _period_from_text(text: str | None) -> tuple[int, str]:
    raw = (text or "").split(maxsplit=1)
    arg = raw[1].strip().lower() if len(raw) > 1 else "week"
    aliases = {
        "today": (1, "Bugun"),
        "bugun": (1, "Bugun"),
        "day": (1, "Bugun"),
        "week": (7, "Oxirgi 7 kun"),
        "hafta": (7, "Oxirgi 7 kun"),
        "weekly": (7, "Oxirgi 7 kun"),
        "month": (30, "Oxirgi 30 kun"),
        "oy": (30, "Oxirgi 30 kun"),
        "monthly": (30, "Oxirgi 30 kun"),
        "30": (30, "Oxirgi 30 kun"),
        "7": (7, "Oxirgi 7 kun"),
        "1": (1, "Bugun"),
    }
    return aliases.get(arg, aliases["week"])


def _risk_label(score: int) -> tuple[str, str]:
    if score >= 75:
        return "🔴", "Yuqori"
    if score >= 45:
        return "🟡", "O'rta"
    return "🟢", "Nazoratda"


def _risk_bar(score: int, width: int = 10) -> str:
    """Visual risk bar: filled red squares + empty white squares.
    Score is clamped to 0-100 and proportionally mapped to `width` cells.
    """
    score = max(0, min(100, int(score or 0)))
    filled = round(score / 100 * width)
    return "🟥" * filled + "⬜" * (width - filled)


def _stats_period_keyboard(active_days: int) -> InlineKeyboardMarkup:
    items = [(1, "Bugun"), (7, "7 kun"), (30, "30 kun")]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=("● " if days == active_days else "") + label,
                callback_data=f"stats_period:{days}",
            )
            for days, label in items
        ],
        [
            InlineKeyboardButton(text="📄 Weekly report", callback_data="report_period:7"),
            InlineKeyboardButton(text="📄 Monthly report", callback_data="report_period:30"),
        ],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="stats_back")],
    ])


def _report_keyboard(days: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"stats_period:{days}")],
        [
            InlineKeyboardButton(text="📊 7 kun", callback_data="report_period:7"),
            InlineKeyboardButton(text="📈 30 kun", callback_data="report_period:30"),
        ],
    ])



def _format_uzs_amount(usd: float) -> str:
    # Approximate display rate; keeps stats readable without calling an FX API.
    uzs = round((usd or 0.0) * 12_600 / 1000) * 1000
    return f"{uzs:,.0f}".replace(",", " ")


def _format_stats_dashboard(stats: dict, label: str) -> str:
    tasks = stats["tasks"]
    meetings = stats["meetings"]
    by_p = tasks["by_priority"]
    risk_emoji, risk_label = _risk_label(stats["risk_score"])
    completion = tasks["completion_rate_pct"]
    comp_emoji = "🟢" if completion >= 70 else "🟡" if completion >= 40 else "🔴"
    total_priority = max(1, sum(by_p.get(p, 0) for p in ("P0", "P1", "P2", "P3")))
    priority_labels = {
        "P0": ("🔴", "Shoshilinch"),
        "P1": ("🟠", "Muhim"),
        "P2": ("🔵", "Rejadagi"),
        "P3": ("⚪", "Oddiy"),
    }
    llm_cost = float(stats["llm"]["cost"] or 0.0)

    lines = [
        f"📊 **PROFESSIONAL STATS** · {label}",
        _SEP,
        "",
        "📍 **KPI**",
        f"• Yaratildi: **{tasks['created']} ta**",
        f"• Bajarildi: **{tasks['done']} ta**",
        f"• Aktiv: **{tasks['active']} ta**",
        f"• Bajarilish: {comp_emoji} **{completion}%**",
        f"• Risk: {risk_emoji} **{stats['risk_score']}/100** — {risk_label}",
        "",
        "⏱ **Deadline**",
        f"• Bugun / 24 soat: **{tasks['due_24h']} ta**",
        f"• 48 soat: **{tasks['due_48h']} ta**",
        f"• 7 kun: **{tasks['due_7d']} ta**",
        f"• Deadline yo'q: **{tasks['no_deadline']} ta**",
        "",
        "⚡ **PRIORITET YUKLAMASI**",
    ]
    for p in ("P0", "P1", "P2", "P3"):
        n = by_p.get(p, 0)
        emoji, label_text = priority_labels[p]
        lines.append(f"{emoji} {label_text:<14} {_ascii_bar(n, total_priority, 10)}  **{n} ta**")

    lines.extend([
        "",
        "👥 **Delegatsiya**",
    ])
    if stats["delegation"]:
        for row in stats["delegation"][:4]:
            lines.append(f"• {row['assignee']}: **{row['total']} ta** ochiq · **{row['overdue'] or 0} ta** o'tgan")
    else:
        lines.append("• Ochiq delegatsiya yo'q")

    lines.extend([
        "",
        "🤝 **Meeting**",
        f"• Uchrashuv: **{meetings['count']} ta**",
        f"• Vaqt: **{meetings['hours']} soat**",
        f"• Action items: **{meetings['action_items']} ta**",
        "",
        "🤖 **Bot Audit**",
        f"• Chaqiriqlar: **{stats['llm']['calls']} ta**",
        f"• Bot xarajati: **${llm_cost:.2f} ≈ {_format_uzs_amount(llm_cost)} so'm**",
    ])

    if tasks["risk_tasks"]:
        lines.extend(["", "🚨 **Risklar**"])
        for task in tasks["risk_tasks"][:4]:
            deadline, _ = _format_deadline_short(task.get("deadline"))
            lines.append(f"{_PRIORITY_BADGE.get(task.get('priority'), '🔵')} {_truncate(task['title'], 44)} — {deadline}")

    return "\n".join(lines)


def _format_executive_report(stats: dict, label: str) -> str:
    tasks = stats["tasks"]
    meetings = stats["meetings"]
    risk_score = stats["risk_score"]
    risk_emoji, risk_label = _risk_label(risk_score)
    by_p = tasks["by_priority"]

    # KPI denominator: total work that "touched" this period = done + still-active.
    total_pool = tasks["done"] + tasks["active"]
    yopildi_value = f"**{tasks['done']} / {total_pool}**" if total_pool else f"**{tasks['done']}**"

    recommendations: list[str] = []
    if tasks["overdue"]:
        recommendations.append("O'tgan vazifalarni bugun yopish yoki deadline yangilash.")
    if by_p.get("P0", 0) + by_p.get("P1", 0) >= 5:
        recommendations.append("Shoshilinch va Muhim ro'yxatini qayta saralash, haqiqatan shoshilinch bo'lmaganlarini Rejadagi ga tushirish.")
    if tasks["no_deadline"]:
        recommendations.append("Deadline'siz vazifalarga muddat belgilash.")
    if meetings["count"] and meetings["followup_count"] < meetings["count"]:
        recommendations.append("Uchrashuvlardan keyingi action itemlarni qayd qilish.")
    if not recommendations:
        recommendations.append("Yuklama nazoratda. Top-3 vazifani ketma-ket yopib boring.")

    blocks: list[str] = []

    # Header
    blocks.append(f"📄 **EXECUTIVE REPORT**\n{label}")

    # Risk header + bar
    blocks.append(
        f"{risk_emoji} **Risk: {risk_label.upper()} — {risk_score}/100**\n"
        f"{_risk_bar(risk_score)}"
    )

    # KPI summary
    blocks.append(
        "📌 **Asosiy KPI**\n\n"
        f"✅ Yopildi: {yopildi_value}\n"
        f"📈 Bajarilish: **{tasks['completion_rate_pct']}%**\n"
        f"⏳ Aktiv: **{tasks['active']}**\n"
        f"⚠️ O'tgan: **{tasks['overdue']}**"
    )

    # Immediate attention — overdue tasks
    overdue_tasks = tasks.get("overdue_tasks") or []
    if overdue_tasks:
        section = ["🚨 **Darhol e'tibor**", ""]
        items: list[str] = []
        for i, task in enumerate(overdue_tasks[:3], 1):
            title = _truncate((task.get("title") or "—").strip(), 60)
            deadline_label, _ = _format_deadline_short(task.get("deadline"))
            items.append(f"{i}. {title}\n   Deadline: {deadline_label}")
        section.append("\n\n".join(items))
        blocks.append("\n".join(section))

    # Delegation
    if stats.get("delegation"):
        section = ["👥 **Delegatsiya**", ""]
        for row in stats["delegation"][:4]:
            section.append(f"• {row['assignee']}: **{row['total']}** ochiq · **{row['overdue'] or 0}** o'tgan")
        blocks.append("\n".join(section))

    # Meetings
    if meetings["count"]:
        blocks.append(
            "🤝 **Uchrashuvlar**\n\n"
            f"• Soni: **{meetings['count']}** · Vaqt: **{meetings['hours']} soat**\n"
            f"• Prep: **{meetings['prep_count']}/{meetings['count']}** · "
            f"Follow-up: **{meetings['followup_count']}/{meetings['count']}**\n"
            f"• Action items: **{meetings['action_items']}**"
        )

    # Next steps
    section = ["➡️ **Keyingi qadamlar**", ""]
    items = [f"{i}. {item}" for i, item in enumerate(recommendations[:4], 1)]
    section.append("\n\n".join(items))
    blocks.append("\n".join(section))

    return "\n\n".join(blocks)


class PlanFSM(StatesGroup):
    awaiting_situation = State()


@router.message(Command("plan"))
async def cmd_plan(message: Message, state: FSMContext) -> None:
    """Executive planning mode — user describes situation (or empty=auto from DB), bot returns a structured strategic plan."""
    # If args were passed inline (e.g., `/plan bugun 5 ta vazifa bor...`), use them directly
    args = message.text.split(maxsplit=1)
    if len(args) > 1 and args[1].strip():
        await _run_planning_session(message, args[1].strip())
        return

    await state.set_state(PlanFSM.awaiting_situation)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Hozirgi holatdan reja", callback_data="plan_auto")],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="plan_cancel")],
    ])
    await message.answer(
        "🎯 **Executive Planning**\n\n"
        "📊 **Hozirgi holatdan reja** — hech narsa yozmasdan, bazadagi real "
        "vazifa, uchrashuv va muddatlaringizdan avtomatik reja tuzaman.\n\n"
        "✍️ Yoki vaziyatni o'zingiz yozing (matn/ovoz): qaysi ishlar, qancha vaqt, "
        "uchrashuvlar, kimga bog'liq.\n\n"
        "_Reja: ustuvorliklar, vaqt taqsimoti, yuboriladigan xabarlar, "
        "eskalatsiya, xavflar va tavsiyalar._",
        parse_mode="Markdown",
        reply_markup=kb,
    )


@router.message(StateFilter(PlanFSM.awaiting_situation), F.text | F.voice)
async def handle_plan_situation_text(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    await state.clear()
    await _run_planning_session(message, message.text)


@router.message(StateFilter(PlanFSM.awaiting_situation), F.voice)
async def handle_plan_situation_voice(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    # Reuse voice handler logic
    if message.voice.file_size and message.voice.file_size > voice_service.MAX_AUDIO_BYTES:
        await message.answer("Ovoz juda katta. Iltimos, qisqaroq yuboring.")
        return
    await message.bot.send_chat_action(message.chat.id, "typing")
    file = await bot.get_file(message.voice.file_id)
    audio_io = await bot.download_file(file.file_path)
    audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
    transcript = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
    if not transcript:
        await message.answer("Ovozni o'qiy olmadim. Matn bilan qayta yuboring.")
        return
    await message.answer(f"_🎙 Tushundim:_ {_escape_markdown(transcript[:200])}…", parse_mode="Markdown")
    await _run_planning_session(message, transcript)


@router.callback_query(F.data == "plan_cancel")
async def cb_plan_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Bekor qilindi")
    try:
        await query.message.edit_text(
            "Reja yaratish bekor qilindi.",
            reply_markup=single_back_keyboard(),
        )
    except Exception:
        pass


@router.callback_query(F.data == "plan_auto")
async def cb_plan_auto(query: CallbackQuery, state: FSMContext) -> None:
    """📊 Auto-plan — build the plan straight from the DB state (no manual
    situation). Claude reads the CURRENT PRINCIPAL STATE block (real tasks,
    meetings, overdue, deadlines) and produces the plan from it."""
    await state.clear()
    await query.answer("Bazadan reja tuzaman…")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await _run_planning_session(query.message, "")


_PLAN_DIRECTIVE = """[INTERNAL] executive_plan

Act as the principal's senior Chief of Staff / strategy advisor — NOT a to-do
organizer. Lead with strategy: start with a STRATEGIK FOKUS block (Maqsad / Eng
muhim bitta narsa / Leverage), then the structured plan (45_planning.md). Apply
leverage (80-20), critical-path sequencing, explicit trade-offs, second-order
risks, aggressive delegation, and say what to DROP today (XAVF & TRADE-OFF section).

Produce a FULL executive planning document in O'zbek (lotin), structured per 45_planning.md (clean emoji sections, no tables).

DATA SOURCE:
- If the principal's message describes a situation, plan around THAT.
- If the message is EMPTY, build the plan from the CURRENT PRINCIPAL STATE block —
  the REAL active tasks, today/this-week meetings, overdue and blocked items, and
  deadlines. Prioritize them, time-block around fixed meetings, flag conflicts.
  NEVER invent tasks or meetings that aren't in the state block. If the state is
  empty, say so briefly and suggest adding tasks — don't fabricate a fake day.

FORMAT — Telegram-friendly, NO markdown tables (they render as raw pipes on mobile):
- Use emoji + section headers + "━━━" dividers + short lines.
- Tasks: "🔴 1. <title> — <P-level>\\n   👤 <mas'ul> · ⏰ <deadline>" (one per block).
- Time plan: "  HH:MM–HH:MM  <ish>" lines (not a table).
- Status icons: 🔴 P0 / 🔴 Fixed / 🟠 P1 / 🔵 P2 / ⚪ P3.

Key reminders:
- Telegram messages (section D) MUST be rasmiy (formal).
- Flag time conflicts; recommend delegation when windows are tight.
- 3 clarifying questions in section J ONLY.

Output: user_message = full plan (Telegram-friendly, NO tables); actions=[].
"""


async def _run_planning_session(message: Message, situation: str) -> None:
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        response = await claude_service.process_message(
            situation, internal_directive=_PLAN_DIRECTIVE,
        )
    finally:
        typing_task.cancel()

    plan_text = response.get("user_message", "")
    if not plan_text:
        await message.answer("Reja yaratib boʻlmadi. Qaytadan urinib koʻring.")
        return

    plan_id = await database.save_plan(situation, plan_text)

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Qabul qilaman", callback_data=f"plan_accept:{plan_id}"),
        InlineKeyboardButton(text="📌 Vazifalar yaratish", callback_data=f"plan_tasks:{plan_id}"),
    ], [
        back_button(),
    ]])
    await _safe_answer(message, plan_text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("plan_accept:"))
async def cb_plan_accept(query: CallbackQuery) -> None:
    plan_id = query.data.split(":", 1)[1]
    await database.mark_plan_accepted(plan_id)
    await query.answer("Reja qabul qilindi ✅ 2 kun ichida qanday o'tganini so'rayman.")
    try:
        await query.message.edit_reply_markup(reply_markup=single_back_keyboard())
    except Exception:
        pass


@router.callback_query(F.data.startswith("plan_tasks:"))
async def cb_plan_tasks(query: CallbackQuery, state: FSMContext) -> None:
    """Extract the principal's own tasks from a saved plan, then show a PREVIEW
    and require confirmation before creating them (reuses the standard
    acts_confirm pipeline — so nothing is created silently)."""
    plan_id = query.data.split(":", 1)[1]
    await query.answer("Vazifalarni ajrataman…")

    plans = await database.list_recent_plans(limit=50)
    plan = next((p for p in plans if p["id"] == plan_id), None)
    if not plan:
        await query.answer("Reja topilmadi", show_alert=True)
        return

    extract_directive = (
        "[INTERNAL] extract_tasks_from_plan\n\n"
        "Extract EVERY actionable task the principal must do himself (NOT delegated "
        "ones — skip items marked 'Topshiring' / Mas'ul ≠ Siz).\n\n"
        # 6000 (was 3000) so tasks deep in a long plan aren't dropped.
        f"PLAN:\n{plan['output_text'][:6000]}\n\n"
        "Output JSON envelope with actions=[create_task...]. Each task: title (imperative), "
        "priority (P0/P1/P2/P3 — map from Status), deadline (ISO 8601 Asia/Tashkent or null). "
        "user_message: one short Uzbek line."
    )
    response = await claude_service.process_message("", internal_directive=extract_directive)
    actions = [a for a in response.get("actions", []) if a.get("type") == "create_task"]
    if not actions:
        await query.message.answer("Rejadan bajariladigan (o'zingizning) vazifa topilmadi.")
        return

    # Confirm before creating — route through the existing acts_confirm flow.
    preview = await _format_create_preview(actions)
    await state.set_state(CreateActionConfirmFSM.awaiting)
    await state.update_data(
        pending_response={"actions": actions,
                          "user_message": f"📌 Rejadan {len(actions)} ta vazifa yaratildi.",
                          "buttons": []},
        _prior_section=None,
    )
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Tasdiqlayman", callback_data="acts_confirm"),
        InlineKeyboardButton(text="✕ Bekor qilish", callback_data="acts_cancel"),
    ]])
    await _safe_answer(query.message, preview, parse_mode="Markdown", reply_markup=confirm_kb)



@router.message(Command("stats"))
async def cmd_stats(message: Message, state: FSMContext | None = None) -> None:
    """Professional stats dashboard with KPI, risks, delegation, meetings, and audit.
    Section reply kbd bilan ishlaydi — davrlar pastdagi reply tugmalardan tanlanadi.
    """
    if state is not None:
        await state.set_state(SectionFSM.in_stats)
    days, label = _period_from_text(message.text)
    await message.answer(
        "📊 **STATISTIKA**", parse_mode="Markdown",
        reply_markup=stats_section_reply_keyboard(),
    )
    stats = await database.executive_stats(days=days)
    await _safe_answer(
        message,
        _format_stats_dashboard(stats, label),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("stats_period:"))
async def cb_stats_period(query: CallbackQuery) -> None:
    days = _cb_int(query.data, default=7)
    if days not in (1, 7, 30):
        days = 7
    label = {1: "Bugun", 7: "Oxirgi 7 kun", 30: "Oxirgi 30 kun"}.get(days, "Oxirgi 7 kun")
    await query.answer()
    stats = await database.executive_stats(days=days)
    text = _format_stats_dashboard(stats, label)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=_stats_period_keyboard(days))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=_stats_period_keyboard(days))


@router.message(Command("report"))
async def cmd_report(message: Message) -> None:
    days, label = _period_from_text(message.text.replace("/report", "/stats", 1) if message.text else None)
    stats = await database.executive_stats(days=days)
    await _safe_answer(
        message,
        _format_executive_report(stats, label),
        parse_mode="Markdown",
        reply_markup=_report_keyboard(days),
    )


@router.callback_query(F.data.startswith("report_period:"))
async def cb_report_period(query: CallbackQuery) -> None:
    days = _cb_int(query.data, default=7)
    if days not in (7, 30):
        days = 7
    label = {7: "Oxirgi 7 kun", 30: "Oxirgi 30 kun"}.get(days, "Oxirgi 7 kun")
    await query.answer()
    stats = await database.executive_stats(days=days)
    text = _format_executive_report(stats, label)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=_report_keyboard(days))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=_report_keyboard(days))


@router.callback_query(F.data == "stats_back")
async def cb_stats_back(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    await state.clear()
    try:
        await query.message.delete()
    except Exception:
        pass
    await _restore_main_keyboard(query.message)


@router.message(Command("team"))
async def cmd_team(message: Message, state: FSMContext | None = None) -> None:
    """👥 Ijrochilar paneli — section reply kbd bilan."""
    if state is not None:
        await state.set_state(SectionFSM.in_team)
    await message.answer(
        "👥 **IJROCHILAR**", parse_mode="Markdown",
        reply_markup=team_section_reply_keyboard(),
    )
    await _render_team_panel(message)


@router.message(Command("risks"))
async def cmd_risks(message: Message, state: FSMContext | None = None) -> None:
    """🚨 Risklar paneli — section reply kbd bilan."""
    if state is not None:
        await state.set_state(SectionFSM.in_risks)
    await message.answer(
        "🚨 **RISKLAR**", parse_mode="Markdown",
        reply_markup=risks_section_reply_keyboard(),
    )
    await _render_risks_panel(message)


@router.message(Command("search"))
async def cmd_search(message: Message, state: FSMContext | None = None) -> None:
    """🔍 Qidiruv — section reply kbd bilan. Scope tanlanmasa, default `all`."""
    if state is not None:
        await state.set_state(SectionFSM.in_search)
        await state.update_data(scope="all")
    text = (
        "🔍 **QIDIRUV**\n\n"
        "Scope tanlang yoki to'g'ridan-to'g'ri so'z yuboring.\n\n"
        "_Default: hammasi (vazifa + uchrashuv + kontakt)._"
    )
    await message.answer(text, parse_mode="Markdown",
                         reply_markup=search_section_reply_keyboard())


# ─────────────────────── IJROCHILAR PANELI (Batch 4) ───────────────────────

import base64 as _b64_team  # local alias to avoid top-level dependency


def _encode_assignee(name: str) -> str:
    """Encode an assignee name for safe inclusion in callback_data (≤64 bytes total)."""
    return _b64_team.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_assignee(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    return _b64_team.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")


def _load_band(active: int) -> tuple[str, str]:
    """Categorise an assignee's active-task load. Returns (label, emoji)."""
    if active >= _ASSIGNEE_OVERLOAD:
        return "yuqori", "🔴"
    if active >= 3:
        return "o'rta", "🟠"
    if active >= 1:
        return "past", "🟢"
    return "bo'sh", "⚪"


def _team_recommendation(loads: dict, top_loaded: dict | None,
                          unassigned_n: int, total_overdue: int) -> str:
    """Heuristic recommendation for the team panel."""
    if not loads or (len(loads) == 1 and "belgilanmagan" in loads):
        return ("Hozircha ijrochilar belgilanmagan. Yangi vazifa yaratishda 👤 Ijrochi tugmasidan "
                "foydalanib mas'ul tayinlang.")
    if unassigned_n >= 5:
        return (f"**{unassigned_n} ta vazifa ijrochisiz**. Avval ularga mas'ul belgilang — "
                "javobgarliksiz vazifa amalga oshmaydi.")
    if top_loaded and top_loaded["active"] >= _ASSIGNEE_OVERLOAD:
        # Find someone less loaded
        light = [
            d for n, d in loads.items()
            if n not in ("belgilanmagan", top_loaded["name"]) and d["active"] < 3
        ]
        if light:
            target = min(light, key=lambda d: d["active"])
            return (f"**{top_loaded['name']}** ortiqcha yuklangan ({top_loaded['active']} ta). "
                    f"Ba'zi vazifalarni **{target['name']}** ga ko'chirib ko'ring "
                    f"(hozir {target['active']} ta).")
        return (f"**{top_loaded['name']}** ortiqcha yuklangan ({top_loaded['active']} ta). "
                "Yangi vazifa berishni vaqtincha to'xtating yoki muddatlarni cho'zing.")
    if total_overdue >= 3:
        return (f"Jamoada **{total_overdue} ta muddati o'tgan** vazifa bor. Har bir ijrochi bilan "
                "alohida ishlab, muddatlarni qayta belgilang.")
    return ("Jamoa yuklamasi muvozanatda. Yangi vazifa berishda eng kam yuklangan ijrochini tanlang.")


def _format_assignees_overview(loads: dict, unassigned_count: int) -> tuple[str, list[dict]]:
    """Ijrochilar paneli — Vazifalar/Uchrashuvlar uslubidagi block-stil.

    Ikonkalar (oldingi paletadan):
      👥 sahifa · 📌 stats · ⚠️ diqqat · 💡 tavsiya
    Detal qator (har ijrochi uchun) ikonkalari:
      📊 Aktiv · ⚡ Yuklama · ⚡/⭐/⏰ holat · 📅 keyingi muddat
    Spacing: divider atrofida 1 bo'sh qator, item orasida 1 bo'sh qator,
    detal blok 6 ta probel indent.
    """
    DIVIDER = "━" * 20

    real = [d for name, d in loads.items() if name != "belgilanmagan"]
    real.sort(key=lambda d: (-d["active"], -d["urgent"], -d["overdue"]))

    busy = [d for d in real if d["active"] > 0]
    overloaded = [d for d in real if d["active"] >= _ASSIGNEE_OVERLOAD]
    total_overdue = sum(d["overdue"] for d in real)

    lines = [
        "👥 **IJROCHILAR**",
        "",
        f"Jami {len(real)} nafar  ·  Yuklangan {len(busy)}  ·  Ortiqcha {len(overloaded)}",
    ]

    if not real:
        lines.extend([
            "",
            DIVIDER,
            "",
            "Bu bo'limda hozircha ijrochilar yo'q.",
            "",
            "_Yangi vazifa yaratishda ijrochi tayinlang._",
        ])
        return "\n".join(lines), []

    if unassigned_count > 0:
        lines.append(f"Ijrochisiz vazifa  ·  {unassigned_count} ta")

    lines.extend(["", DIVIDER, "", "📌 **RO'YXAT**", ""])

    def _next_deadline_label(next_dl: str | None) -> str:
        if not next_dl:
            return "yo'q"
        try:
            dl_dt = datetime.fromisoformat(next_dl)
        except (ValueError, TypeError):
            return "—"
        now = datetime.now(database.TZ)
        if dl_dt.date() == now.date():
            return f"Bugun {dl_dt.strftime('%H:%M')}"
        if dl_dt.date() == (now + timedelta(days=1)).date():
            return f"Ertaga {dl_dt.strftime('%H:%M')}"
        return dl_dt.strftime("%d-%m, %H:%M")

    items: list[str] = []
    for i, d in enumerate(real[:10], start=1):
        band, _badge = _load_band(d["active"])
        dl_label = _next_deadline_label(d.get("next_deadline"))
        block = [
            f"{i}.  {d['name']}",
            "",
            f"      📊 Aktiv:           {d['active']} ta",
            f"      ⚡ Yuklama:         {band}",
        ]
        # Selective signal lines — only render if non-zero
        if d.get("urgent"):
            block.append(f"      ⚡ Shoshilinch:     {d['urgent']} ta")
        if d.get("important"):
            block.append(f"      ⭐ Muhim:           {d['important']} ta")
        if d.get("overdue"):
            block.append(f"      ⏰ O'tgan:          {d['overdue']} ta")
        block.append(f"      📅 Keyingi muddat:  {dl_label}")
        items.append("\n".join(block))

    lines.append("\n\n".join(items))
    lines.append("")

    if len(real) > 10:
        lines.append(f"_+{len(real) - 10} ta yana_")
        lines.append("")

    # ⚠️ DIQQAT — surface specific anomalies (kept as bullets for quick scanning)
    flags = []
    for d in real[:10]:
        if d["active"] >= _ASSIGNEE_OVERLOAD:
            flags.append(f"  • **{d['name']}** — yuklamasi yuqori ({d['active']} ta)")
        if d["overdue"] >= 2:
            flags.append(f"  • **{d['name']}** — {d['overdue']} ta muddati o'tgan")
    if unassigned_count >= 3:
        flags.append(f"  • Belgilanmagan — {unassigned_count} ta vazifa")
    if flags:
        lines.extend([DIVIDER, "", "⚠️ **DIQQAT**", ""])
        lines.extend(flags[:5])
        lines.append("")

    top_loaded = real[0] if real else None
    rec = _team_recommendation(loads, top_loaded, unassigned_count, total_overdue)
    lines.extend([DIVIDER, "", "💡 **TAVSIYA**", "", rec])

    return "\n".join(lines), real[:10]


async def _render_team_panel(message: Message) -> None:
    """Ijrochilar paneli — overview + drill-down + reassign shortcuts."""
    loads = await database.assignee_load_map()
    unassigned = await database.list_unassigned_tasks(limit=500)

    text, ordered = _format_assignees_overview(loads, len(unassigned))

    # Build keyboard: number drill-downs for top assignees, then actions, then back.
    rows: list[list[InlineKeyboardButton]] = []
    if ordered:
        nums = [
            InlineKeyboardButton(text=str(i + 1),
                                  callback_data=f"team:open:{_encode_assignee(d['name'])}")
            for i, d in enumerate(ordered)
        ]
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])

    # Tezkor amal va navigatsiya tugmalari endi pastdagi section reply kbd da —
    # inline'da faqat raqamli drill-down qoladi.
    reply_markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=reply_markup)


def _format_assignee_profile(profile: dict) -> str:
    """Render a single assignee's profile card."""
    DIVIDER = "━" * 20
    name = profile["name"]
    band, _badge = _load_band(profile["active"])

    lines = [
        "👤 **IJROCHI**",
        "",
        name,
        "",
        DIVIDER,
        "",
        "📌 **STATISTIKA**",
        "",
        f"      📊 Aktiv:           {profile['active']} ta",
        f"      ✅ Bajarilgan:      {profile['completed']} ta",
        f"      📈 Bajarilish:      {profile['completion_rate']}%",
        f"      ⏰ O'tgan:          {profile['overdue']} ta",
        f"      ⚡ Shoshilinch:     {profile['urgent']} ta",
        f"      ⭐ Muhim:           {profile['important']} ta",
    ]
    if profile.get("avg_closing_hours"):
        lines.append(f"      ⌛ O'rtacha:        {profile['avg_closing_hours']} soat")
    lines.append("")

    tasks = profile.get("tasks", [])
    if tasks:
        lines.extend([DIVIDER, "", f"📋 **AKTIV VAZIFALAR**   ·   {len(tasks)} ta", ""])
        items: list[str] = []
        for i, t in enumerate(tasks, start=1):
            title = (t.get("title") or "—").strip()
            muhimlik = _PRIORITY_LABEL_UZ.get(t.get("priority", "P2"), "Rejadagi")
            block = [
                f"{i}.  {title}",
                "",
                f"      ⏳ Muddat:          {_muddat_label(t.get('deadline'))}",
                f"      🔹 Muhimlik:        {muhimlik}",
            ]
            items.append("\n".join(block))
        lines.append("\n\n".join(items))
        lines.append("")
    else:
        lines.extend([DIVIDER, "", "_Hozircha aktiv vazifa yo'q._", ""])

    # Yuklama signali
    lines.extend([DIVIDER, "", "⚡ **YUKLAMA**", ""])
    if band == "yuqori":
        lines.append(f"Yuqori — {profile['active']}+ aktiv vazifa.")
        lines.append("_Yangi vazifa berishni cheklang yoki muddatlarni cho'zing._")
    elif band == "o'rta":
        lines.append(f"O'rta — {profile['active']} ta aktiv.")
        lines.append("_Yuklamani kuzatib turing, muvozanatda._")
    elif band == "past":
        lines.append(f"Past — {profile['active']} ta aktiv.")
        lines.append("_Qo'shimcha vazifa qabul qila oladi._")
    else:
        lines.append("Bo'sh — aktiv vazifa yo'q.")
        lines.append("_Yangi vazifalar uchun ochiq._")

    return "\n".join(lines)


@router.callback_query(F.data.startswith("team:open:"))
async def cb_team_open(query: CallbackQuery) -> None:
    """Drill-down: show one assignee's full profile."""
    token = query.data.split(":", 2)[2]
    try:
        name = _decode_assignee(token)
    except Exception:
        await query.answer("Ijrochini tahlil qilib bo'lmadi", show_alert=True)
        return
    profile = await database.assignee_profile(name)
    if not profile:
        await query.answer("Ijrochi topilmadi", show_alert=True)
        return
    await query.answer()

    # Build per-task drill-down buttons (numbered) + reassign + back
    rows: list[list[InlineKeyboardButton]] = []
    tasks = profile.get("tasks", [])
    if tasks:
        nums = [
            InlineKeyboardButton(text=str(i + 1), callback_data=f"taskopen:{t['id']}")
            for i, t in enumerate(tasks)
        ]
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
    rows.append([
        InlineKeyboardButton(text="🔄 Qayta taqsimlash",
                              callback_data=f"team:reassign_one:{token}"),
        InlineKeyboardButton(text="⬅️ Jamoa", callback_data="team:refresh"),
    ])

    text = _format_assignee_profile(profile)
    try:
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "team:refresh")
async def cb_team_refresh(query: CallbackQuery) -> None:
    await query.answer("Yangilanmoqda...")
    try:
        await query.message.delete()
    except Exception:
        pass
    await _render_team_panel(query.message)





@router.callback_query(F.data.startswith("team:reassign_one:"))
async def cb_team_reassign_one(query: CallbackQuery) -> None:
    """Show one assignee's tasks for picking which to reassign."""
    token = query.data.split(":", 2)[2]
    try:
        name = _decode_assignee(token)
    except Exception:
        await query.answer("Ijrochini tahlil qilib bo'lmadi", show_alert=True)
        return
    profile = await database.assignee_profile(name)
    if not profile or not profile.get("tasks"):
        await query.answer("Ijrochida aktiv vazifa yo'q", show_alert=True)
        return
    await query.answer()
    tasks = profile["tasks"][:10]
    lines = [
        f"🔄 **QAYTA TAQSIMLASH** — {name}",
        _SEP,
        "",
        "Qaysi vazifani boshqa ijrochiga ko'chirmoqchisiz?",
        "",
    ]
    nums: list[InlineKeyboardButton] = []
    for i, t in enumerate(tasks, start=1):
        badge = _PRIORITY_BADGE.get(t.get("priority"), "🔵")
        title = (t.get("title") or "—").strip()
        dl_label, _ovd = _format_deadline_short(t.get("deadline"))
        lines.append(f"**{i}. {badge} {title}**")
        lines.append(f"   ⏰ {dl_label}")
        lines.append("")
        nums.append(InlineKeyboardButton(text=str(i), callback_data=f"set_assignee:{t['id']}"))
    grid = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    grid.append([InlineKeyboardButton(text="⬅️ Profil", callback_data=f"team:open:{token}")])
    await _safe_answer(
        query.message, "\n".join(lines).rstrip(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=grid),
    )


# ─────────────────────── RISKLAR PANELI (Batch 3) ───────────────────────

# Threshold: an assignee with this many active tasks is considered "overloaded".
_ASSIGNEE_OVERLOAD = 5


async def _classify_risks() -> dict:
    """Group active tasks into yuqori / o'rta / past risk buckets per the spec.

    Each task lands in only ONE bucket — the highest-risk one that matches.
    Returns: {high: [(task, reason)], medium: [...], low: [...]}.
    """
    now = datetime.now(database.TZ)
    horizon_24 = (now + timedelta(hours=24)).isoformat()
    horizon_48 = (now + timedelta(hours=48)).isoformat()
    horizon_3d = (now + timedelta(days=3)).isoformat()
    now_iso_str = now.isoformat()

    active = await database.list_tasks(status_in=["todo", "in_progress"], limit=500)
    loads = await database.assignee_load_map()
    overloaded = {
        name for name, d in loads.items()
        if name != "belgilanmagan" and d["active"] >= _ASSIGNEE_OVERLOAD
    }

    high: list[tuple[dict, str]] = []
    medium: list[tuple[dict, str]] = []
    low: list[tuple[dict, str]] = []

    for t in active:
        deadline = t.get("deadline")
        priority = t.get("priority", "P2")
        assignee = (t.get("assignee") or "").strip()
        is_unassigned = (not assignee) or assignee.lower() == "belgilanmagan"
        status = t.get("status", "todo")

        # HIGH (first match wins)
        if deadline and deadline < now_iso_str:
            high.append((t, "Deadline o'tdi"))
            continue
        if priority == "P0":
            high.append((t, "Shoshilinch va hali bajarilmagan"))
            continue
        if deadline and deadline <= horizon_24:
            high.append((t, "Deadline 24 soat ichida"))
            continue
        if is_unassigned and deadline and deadline <= horizon_48:
            high.append((t, "Ijrochi yo'q + muddat 48 soatda"))
            continue

        # MEDIUM
        if deadline and deadline <= horizon_48:
            medium.append((t, "Deadline 48 soat ichida"))
            continue
        if priority == "P1" and status == "todo":
            medium.append((t, "Muhim, jarayonda emas"))
            continue
        if (not is_unassigned) and assignee in overloaded:
            medium.append((t, f"Ijrochi yuklamasi yuqori ({assignee})"))
            continue

        # LOW: deadline + assignee + in progress + > 3 days away
        if (deadline and not is_unassigned and status == "in_progress"
                and deadline > horizon_3d):
            low.append((t, "Rejada, jarayonda"))
            continue

    # Order each bucket: P0 first, then nearest deadline first (None last).
    pri_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

    def _sort_key(item):
        t, _ = item
        return (
            pri_rank.get(t.get("priority", "P2"), 2),
            t.get("deadline") or "9999",
        )

    high.sort(key=_sort_key)
    medium.sort(key=_sort_key)
    low.sort(key=_sort_key)

    return {"high": high, "medium": medium, "low": low}


def _risks_recommendation(high: list, medium: list, low: list, risk: dict) -> str:
    """Heuristic system recommendation for the risks panel."""
    overdue_n = sum(1 for _, r in high if "Deadline o'tdi" in r)
    unassigned_n = sum(1 for _, r in high if "Ijrochi yo'q" in r)

    if not high and not medium:
        return ("Hozir kritik risk yo'q. Past riskdagi vazifalarni rejada davom ettiring "
                "va keyingi vazifalarga muddat belgilashga vaqt ajrating.")
    if overdue_n >= 1:
        return (f"**{overdue_n} ta vazifa muddati o'tgan**. Avval shularni yoping yoki "
                "realistik yangi muddat tayinlang — eski deadline'lar yangi vazifalarni ham buzadi.")
    if unassigned_n >= 1:
        return (f"**{unassigned_n} ta yuqori riskli vazifa ijrochisiz**. Mas'ul belgilamasdan "
                "ular kechikadi. Avval [👤 Ijrochi tayinlash] tugmasidan foydalaning.")
    if len(high) >= 5:
        return (f"**{len(high)} ta yuqori risk** to'planib qoldi. Bir kunda hammasini yopish "
                "qiyin — eng yaqin deadline'dan boshlang yoki ba'zilarini delegatsiya qiling.")
    if risk["score"] >= 70:
        return ("Risk darajasi yuqori. Bugun faqat yuqori risk vazifalariga e'tibor qarating, "
                "yangi vazifalar yaratishni vaqtincha to'xtating.")
    if len(medium) >= 5:
        return (f"**{len(medium)} ta o'rta risk** kuzatuvda. Yuqori bo'lib ketmasligi uchun "
                "shu hafta ichida boshlang yoki muddatlarni qayta ko'rib chiqing.")
    return ("Yuqori risklarni bugun yoki ertaga yoping. O'rta risklarga 24-48 soat ichida "
            "vaqt ajrating — proaktiv yondashish kechikishni oldini oladi.")


async def _render_risks_panel(message: Message) -> None:
    """Risklar paneli — comprehensive risk classification + action shortcuts."""
    buckets = await _classify_risks()
    high = buckets["high"]
    medium = buckets["medium"]
    low = buckets["low"]
    risk = await compute_risk_score()
    now = datetime.now(database.TZ)

    lines = [
        f"🚨 **RISKLAR PANELI** · {now.strftime('%d-%m %H:%M')}",
        _SEP,
        "",
        f"Risk score: {risk['emoji']} **{risk['score']}/100** — {risk['status']}",
        f"Jami risklar: 🔴 {len(high)} · 🟠 {len(medium)} · ⚪ {len(low)}",
        "",
    ]

    def fmt_block(tasks: list, badge: str, label: str, limit: int) -> list[str]:
        out = [f"{badge} **{label.upper()}** ({len(tasks)} ta)", ""]
        if not tasks:
            out.append("_Hozircha bunday vazifa yo'q._")
            out.append("")
            return out
        for i, (t, reason) in enumerate(tasks[:limit], start=1):
            badge_p = _PRIORITY_BADGE.get(t.get("priority", "P2"), "🔵")
            title = (t.get("title") or "—").strip()
            deadline_label, _ovd = _format_deadline_short(t.get("deadline"))
            assignee = (t.get("assignee") or "").strip() or "belgilanmagan"
            out.append(f"**{i}. {badge_p} {title}**")
            out.append(f"• Sabab: _{reason}_")
            out.append(f"• Muddat: {deadline_label}")
            out.append(f"• Ijrochi: {assignee}")
            out.append("")
        if len(tasks) > limit:
            out.append(f"_+{len(tasks) - limit} ta yana ko'rsatilmadi_")
            out.append("")
        return out

    lines.extend(fmt_block(high, "🔴", "Yuqori risk", limit=5))
    lines.extend(fmt_block(medium, "🟠", "O'rta risk", limit=5))
    lines.extend(fmt_block(low, "⚪", "Past risk", limit=3))

    lines.extend([
        "🤖 **TIZIM TAVSIYASI**",
        _risks_recommendation(high, medium, low, risk),
    ])

    # Build keyboard: numbered drill-down for HIGH tasks (open task card),
    # then global action shortcuts, then back.
    rows: list[list[InlineKeyboardButton]] = []
    if high:
        nums = [
            InlineKeyboardButton(text=str(i + 1), callback_data=f"taskopen:{t['id']}")
            for i, (t, _) in enumerate(high[:5])
        ]
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])

    # Object-specific tezkor amallar (Ijrochi tayinlash, Eslatma, Muddat) —
    # inline'da qoladi (kontekstli). 🔄 Yangilash va ⬅️ Cockpit reply kbd'da bor.
    rows.extend([
        [
            InlineKeyboardButton(text="👤 Ijrochi tayinlash", callback_data="risks_assign"),
            InlineKeyboardButton(text="⏰ Eslatma qo'yish", callback_data="risks_remind"),
        ],
        [
            InlineKeyboardButton(text="📅 Muddatni o'zgartirish", callback_data="risks_deadline"),
        ],
    ])

    await _safe_answer(
        message,
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


def _risks_sublist(title_line: str, tasks: list[dict],
                   callback_prefix: str, empty_msg: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build a numbered sublist for one of the risk-action shortcuts.

    callback_prefix: prefix for each numbered button's callback_data, e.g.
                     "set_assignee", "taskopen", "editfield". The task id is appended.
                     For "editfield", we append ":deadline" too.
    """
    if not tasks:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Risklar", callback_data="risks_refresh")],
        ])
        return empty_msg, kb

    lines = [title_line, _SEP, ""]
    nums: list[InlineKeyboardButton] = []
    for i, t in enumerate(tasks[:10], start=1):
        badge = _PRIORITY_BADGE.get(t.get("priority", "P2"), "🔵")
        title = (t.get("title") or "—").strip()
        deadline_label, _ovd = _format_deadline_short(t.get("deadline"))
        assignee = (t.get("assignee") or "").strip() or "belgilanmagan"
        lines.append(f"**{i}. {badge} {title}**")
        lines.append(f"• Muddat: {deadline_label}")
        lines.append(f"• Ijrochi: {assignee}")
        lines.append("")

        if callback_prefix == "editfield":
            cb = f"editfield:{t['id']}:deadline"
        else:
            cb = f"{callback_prefix}:{t['id']}"
        nums.append(InlineKeyboardButton(text=str(i), callback_data=cb))

    grid = [nums[i:i + 5] for i in range(0, len(nums), 5)]
    grid.append([InlineKeyboardButton(text="⬅️ Risklar", callback_data="risks_refresh")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(inline_keyboard=grid)


@router.callback_query(F.data == "risks_refresh")
async def cb_risks_refresh(query: CallbackQuery) -> None:
    await query.answer("Yangilanmoqda...")
    try:
        await query.message.delete()
    except Exception:
        pass
    await _render_risks_panel(query.message)



@router.callback_query(F.data == "risks_assign")
async def cb_risks_assign(query: CallbackQuery) -> None:
    """Shortcut: list unassigned risky tasks → tap a number → set_assignee flow."""
    await query.answer()
    candidates = await database.list_unassigned_tasks(limit=10)
    text, kb = _risks_sublist(
        title_line="👤 **Ijrochi tayinlash kerak**",
        tasks=candidates,
        callback_prefix="set_assignee",
        empty_msg="✅ Ijrochisiz vazifa qolmagan.",
    )
    await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "risks_remind")
async def cb_risks_remind(query: CallbackQuery) -> None:
    """Shortcut: list high-risk tasks → tap a number → open task card (reminder there)."""
    await query.answer()
    buckets = await _classify_risks()
    candidates = [t for t, _ in buckets["high"][:10]]
    text, kb = _risks_sublist(
        title_line="⏰ **Eslatma qo'yish uchun vazifani tanlang**",
        tasks=candidates,
        callback_prefix="taskopen",
        empty_msg="✅ Hozir yuqori risk-vazifa yo'q.",
    )
    await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "risks_deadline")
async def cb_risks_deadline(query: CallbackQuery) -> None:
    """Shortcut: list overdue + 24h tasks → tap a number → opens deadline picker."""
    await query.answer()
    buckets = await _classify_risks()
    candidates = [t for t, _ in buckets["high"][:10]]
    text, kb = _risks_sublist(
        title_line="📅 **Muddat o'zgartirish kerak**",
        tasks=candidates,
        callback_prefix="editfield",  # → editfield:<id>:deadline
        empty_msg="✅ Muddat o'zgartirish talab qilinadigan vazifa yo'q.",
    )
    await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


# ─────────────────────── /search (Batch 5) ───────────────────────


async def _render_search_prompt(message: Message, state: FSMContext = None) -> None:
    """Global search across tasks + meetings + contacts."""
    if state is not None:
        await state.set_state(GlobalSearchFSM.awaiting_query)
    text = (
        "🔍 **QIDIRUV**\n" + _SEP + "\n\n"
        "Sarlavha, tavsif, ijrochi, ishtirokchi yoki teg bo'yicha so'z yuboring.\n\n"
        "_Misol: «marketing», «Aziz aka», «shoshilinch»._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="search_cancel")],
    ])
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data == "search_cancel")
async def cb_search_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Bekor qilindi")
    try:
        await query.message.delete()
    except Exception:
        pass


@router.message(StateFilter(GlobalSearchFSM.awaiting_query), F.text | F.voice)
async def handle_global_search(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    await state.clear()
    q = (message.text or "").strip()
    if not q or q.startswith("/"):
        await _safe_answer(
            message,
            "Qidiruv so'zi bo'sh bo'lmasin. `/search` yana yuboring.",
            parse_mode="Markdown", reply_markup=main_reply_keyboard(),
        )
        return

    results = await database.search_all(q, limit=20)
    tasks = results.get("tasks", [])
    reminders = results.get("reminders", [])
    meetings = results.get("meetings", [])

    lines = [
        f"🔍 **QIDIRUV NATIJASI** · «{q}»",
        _SEP,
        "",
        f"Topildi: **{len(tasks)} vazifa** · **{len(reminders)} eslatma** · **{len(meetings)} uchrashuv**",
        "",
    ]

    if not tasks and not reminders and not meetings:
        lines.extend([
            "_Hech narsa topilmadi._",
            "",
            "Boshqa so'z yoki ism bilan urinib ko'ring.",
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Qayta qidirish", callback_data="search_again")],
            [InlineKeyboardButton(text="⬅️ Boshqaruv paneli", callback_data="search_back")],
        ])
        await _safe_answer(message, "\n".join(lines), parse_mode="Markdown", reply_markup=kb)
        return

    rows: list[list[InlineKeyboardButton]] = []

    if tasks:
        lines.append("📌 **VAZIFALAR**")
        lines.append("")
        nums: list[InlineKeyboardButton] = []
        for i, ttask in enumerate(tasks[:8], start=1):
            badge = _PRIORITY_BADGE.get(ttask.get("priority", "P2"), "🔵")
            title = (ttask.get("title") or "—").strip()
            dl_label, _ovd = _format_deadline_short(ttask.get("deadline"))
            assignee = (ttask.get("assignee") or "").strip() or "belgilanmagan"
            status_uz = _STATUS_LABEL_UZ.get(ttask.get("status", "todo"), ttask.get("status", "todo"))
            lines.append(f"**{i}. {badge} {title}**")
            lines.append(f"   ⏰ {dl_label} · 👤 {assignee} · 📊 {status_uz}")
            lines.append("")
            nums.append(InlineKeyboardButton(text=str(i), callback_data=f"taskopen:{ttask['id']}"))
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
        if len(tasks) > 8:
            lines.append(f"_+{len(tasks) - 8} ta vazifa yana ko'rsatilmadi_")
            lines.append("")

    if reminders:
        lines.append("⏰ **ESLATMALAR**")
        lines.append("")
        nums: list[InlineKeyboardButton] = []
        for i, reminder in enumerate(reminders[:6], start=1):
            title = (reminder.get("title") or "—").strip()
            lines.append(f"**R{i}. {_reminder_status_icon(reminder)} {title}**")
            lines.append(f"   ⏰ {_reminder_time_chip(reminder)} · 📌 {_reminder_status_label(reminder.get('status'))}")
            lines.append("")
            nums.append(InlineKeyboardButton(text=f"R{i}", callback_data=f"remopen:{reminder['id']}"))
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
        if len(reminders) > 6:
            lines.append(f"_+{len(reminders) - 6} ta eslatma yana ko'rsatilmadi_")
            lines.append("")

    if meetings:
        lines.append("🤝 **UCHRASHUVLAR**")
        lines.append("")
        nums: list[InlineKeyboardButton] = []
        for i, m in enumerate(meetings[:5], start=1):
            try:
                dt = datetime.fromisoformat(m["datetime_start"]).astimezone(database.TZ)
                when_label = dt.strftime("%d-%m %H:%M")
            except (ValueError, TypeError):
                when_label = m.get("datetime_start") or "—"
            title = (m.get("title") or "—").strip()
            participants = ", ".join(m.get("participants", [])) or "—"
            lines.append(f"**M{i}. {title}**")
            lines.append(f"   🕐 {when_label} · 👥 {participants}")
            lines.append("")
            nums.append(InlineKeyboardButton(
                text=f"M{i}", callback_data=f"meetingopen:{m['id']}"))
        for i in range(0, len(nums), 5):
            rows.append(nums[i:i + 5])
        if len(meetings) > 5:
            lines.append(f"_+{len(meetings) - 5} ta uchrashuv yana ko'rsatilmadi_")
            lines.append("")

    rows.append([
        InlineKeyboardButton(text="🔄 Qayta qidirish", callback_data="search_again"),
        InlineKeyboardButton(text="⬅️ Boshqaruv paneli", callback_data="search_back"),
    ])

    await _safe_answer(
        message, "\n".join(lines).rstrip(),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data == "search_again")
async def cb_search_again(query: CallbackQuery, state: FSMContext) -> None:
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    await _render_search_prompt(query.message, state)


@router.callback_query(F.data == "search_back")
async def cb_search_back(query: CallbackQuery) -> None:
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass
    await cmd_cockpit(query.message)


# ─────────────────────── YANGI VAZIFA — guided FSM (Batch 5) ───────────────────────


_NEWTASK_PRIORITY_MAP = {"P0": "Shoshilinch", "P1": "Muhim", "P2": "Rejadagi", "P3": "Oddiy"}


def _newtask_summary(data: dict) -> str:
    """Render the running summary shown above each form step."""
    title = data.get("title") or "_(kiritilmagan)_"
    pri = data.get("priority")
    pri_label = (f"{_PRIORITY_BADGE.get(pri, '🔵')} {_PRIORITY_LABEL_UZ.get(pri, '—')}"
                 if pri else "_(tanlanmagan)_")
    deadline = data.get("deadline")
    if deadline:
        dl_label, _ovd = _format_deadline_short(deadline)
    else:
        dl_label = "_(o'tkazib yuborilgan)_"
    assignee = data.get("assignee") or "_(o'zim)_"
    return (
        f"📝 Sarlavha: {title}\n"
        f"⚡ Ustuvorlik: {pri_label}\n"
        f"📅 Muddat: {dl_label}\n"
        f"👤 Ijrochi: {assignee}"
    )


def _newtask_priority_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 Shoshilinch", callback_data="newtask:pri:P0"),
            InlineKeyboardButton(text="🟠 Muhim",      callback_data="newtask:pri:P1"),
        ],
        [
            InlineKeyboardButton(text="🔵 Rejadagi",   callback_data="newtask:pri:P2"),
            InlineKeyboardButton(text="⚪ Oddiy",      callback_data="newtask:pri:P3"),
        ],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel")],
    ])


def _newtask_deadline_kb() -> InlineKeyboardMarkup:
    """Deadline step 1 — pick a DAY. The time is chosen on the next screen."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Bugun", callback_data="newtask:dl:today"),
            InlineKeyboardButton(text="📅 Ertaga", callback_data="newtask:dl:tomorrow"),
            InlineKeyboardButton(text="📅 Indin", callback_data="newtask:dl:indin"),
        ],
        [
            InlineKeyboardButton(text="📅 +3 kun", callback_data="newtask:dl:plus3"),
            InlineKeyboardButton(text="📅 Hafta oxiri", callback_data="newtask:dl:weekend"),
        ],
        [
            InlineKeyboardButton(text="✏️ Qo'lda", callback_data="newtask:dl:manual"),
            InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="newtask:dl:skip"),
        ],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel")],
    ])


_NEWTASK_TIME_SLOTS = ["09:00", "12:00", "14:00", "17:00", "18:00"]


def _newtask_time_kb() -> InlineKeyboardMarkup:
    """Deadline step 2 — pick a TIME for the already-chosen day. Time callbacks
    use 'HHMM' (no colon) so the ':' delimiter split stays unambiguous."""
    slots = [
        InlineKeyboardButton(text=t, callback_data=f"newtask:tm:{t.replace(':', '')}")
        for t in _NEWTASK_TIME_SLOTS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        slots[0:3],
        slots[3:5] + [InlineKeyboardButton(text="⌨️ Boshqa", callback_data="newtask:tm:custom")],
        [
            InlineKeyboardButton(text="⬅️ Orqaga", callback_data="newtask:dl:back"),
            InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel"),
        ],
    ])


def _newtask_assignee_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👤 Men", callback_data="newtask:as:me"),
            InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="newtask:as:skip"),
        ],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel")],
    ])


def _newtask_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlash", callback_data="newtask:confirm"),
            InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel"),
        ],
    ])


async def _newtask_start(message: Message, state: FSMContext) -> None:
    """Entry to the guided new-task form."""
    await state.set_state(NewTaskFSM.awaiting_title)
    await state.update_data(
        title=None, priority=None, deadline=None, assignee=None,
    )
    text = (
        "➕ **YANGI VAZIFA — Forma**\n" + _SEP + "\n\n"
        "1️⃣ **Sarlavha** kiriting:\n"
        "_Masalan: «Marketing strategiyasini Aziz akaga yuborish»_"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel")],
    ])
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


@router.message(StateFilter(NewTaskFSM.awaiting_title), F.text | F.voice)
async def newtask_title(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    title = (message.text or "").strip()
    if not title or title.startswith("/"):
        await message.answer("Sarlavha bo'sh bo'lmasin. Iltimos, qaytadan yuboring.")
        return
    if len(title) > 200:
        title = title[:200]
    await state.update_data(title=title)
    await state.set_state(NewTaskFSM.awaiting_priority)
    data = await state.get_data()
    await _safe_answer(
        message,
        f"{_newtask_summary(data)}\n" + _SEP + "\n\n"
        "2️⃣ **Ustuvorlik** tanlang:",
        parse_mode="Markdown",
        reply_markup=_newtask_priority_kb(),
    )


@router.callback_query(F.data.startswith("newtask:pri:"))
async def newtask_priority(query: CallbackQuery, state: FSMContext) -> None:
    pri = query.data.split(":", 2)[2]
    if pri not in {"P0", "P1", "P2", "P3"}:
        await query.answer("Noto'g'ri ustuvorlik", show_alert=True)
        return
    await state.update_data(priority=pri)
    await state.set_state(NewTaskFSM.awaiting_deadline)
    data = await state.get_data()
    await query.answer()
    text = (
        f"{_newtask_summary(data)}\n" + _SEP + "\n\n"
        "3️⃣ **Muddat** tanlang yoki o'tkazib yuboring:"
    )
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_newtask_deadline_kb())
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=_newtask_deadline_kb())


def _newtask_compute_day(key: str) -> tuple[str | None, str | None]:
    """Convert a day-preset key into (iso_date 'YYYY-MM-DD', human_label).
    Returns (None, None) for an unknown key."""
    now = datetime.now(database.TZ)
    if key == "today":
        target, label = now, "Bugun"
    elif key == "tomorrow":
        target, label = now + timedelta(days=1), "Ertaga"
    elif key == "indin":
        target, label = now + timedelta(days=2), "Indin"
    elif key == "plus3":
        target, label = now + timedelta(days=3), "+3 kun"
    elif key == "weekend":
        days_until_sat = (5 - now.weekday()) % 7 or 7
        target, label = now + timedelta(days=days_until_sat), "Shanba"
    else:
        return None, None
    return target.strftime("%Y-%m-%d"), label


def _combine_day_time(day_iso: str | None, hh: int, mm: int) -> str | None:
    """Combine a 'YYYY-MM-DD' day with HH:MM into a TZ-aware ISO timestamp.
    Returns None on malformed input or an impossible time (e.g. hour 25)."""
    if not day_iso:
        return None
    try:
        y, mo, d = map(int, day_iso.split("-"))
        return database.TZ.localize(datetime(y, mo, d, hh, mm)).isoformat()
    except (ValueError, TypeError):
        return None


async def _newtask_show_assignee(message: Message, data: dict, *, edit: bool) -> None:
    """Render the assignee step. edit=True edits the bot's message (callback
    path); edit=False sends a fresh message (user-typed path)."""
    text = (
        f"{_newtask_summary(data)}\n" + _SEP + "\n\n"
        "4️⃣ **Ijrochi** (ixtiyoriy):\n\n"
        "Pastdagi tugmalardan biri yoki ism yuboring "
        "(masalan: «Komilov Javohir»)."
    )
    if edit:
        try:
            await message.edit_text(text, parse_mode="Markdown",
                                    reply_markup=_newtask_assignee_kb())
            return
        except TelegramBadRequest:
            pass
    await _safe_answer(message, text, parse_mode="Markdown",
                       reply_markup=_newtask_assignee_kb())


@router.callback_query(F.data.startswith("newtask:dl:"))
async def newtask_deadline(query: CallbackQuery, state: FSMContext) -> None:
    preset = query.data.split(":", 2)[2]
    if preset == "manual":
        await state.set_state(NewTaskFSM.awaiting_deadline_manual)
        await query.answer()
        text = (
            "📅 **Muddatni qo'lda kiriting**\n\n"
            "Formatlar:\n"
            "• `2026-05-25 14:30`\n"
            "• `25-05 14:30` (joriy yil)\n"
            "• `ertaga 09:00` / `bugun 17:00`\n"
            "• `juma 12:00` (yaqin payshanba/juma...)\n"
            "• `2 soat` / `15 daqiqa`"
        )
        try:
            await query.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="newtask:dl:back")],
                    [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newtask:cancel")],
                ]),
            )
        except TelegramBadRequest:
            await _safe_answer(query.message, text, parse_mode="Markdown")
        return

    if preset == "back":
        # Return to step 1 — the day picker.
        await state.set_state(NewTaskFSM.awaiting_deadline)
        data = await state.get_data()
        await query.answer()
        try:
            await query.message.edit_text(
                f"{_newtask_summary(data)}\n" + _SEP + "\n\n3️⃣ **Muddat** — kunni tanlang:",
                parse_mode="Markdown", reply_markup=_newtask_deadline_kb(),
            )
        except TelegramBadRequest:
            pass
        return

    if preset == "skip":
        await state.update_data(deadline=None)
        await state.set_state(NewTaskFSM.awaiting_assignee)
        await query.answer()
        await _newtask_show_assignee(query.message, await state.get_data(), edit=True)
        return

    # A day was chosen → remember it and show step 2 (the time picker).
    day_iso, label = _newtask_compute_day(preset)
    if not day_iso:
        await query.answer("Noto'g'ri kun preseti", show_alert=True)
        return
    await state.update_data(_dl_day_iso=day_iso, _dl_day_label=label)
    await state.set_state(NewTaskFSM.awaiting_deadline_time)
    await query.answer()
    text = f"🕐 **{label}** — soatni tanlang yoki `HH:MM` yozing:"
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_newtask_time_kb())
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=_newtask_time_kb())


@router.callback_query(F.data.startswith("newtask:tm:"))
async def newtask_deadline_time(query: CallbackQuery, state: FSMContext) -> None:
    """Step 2: a time slot (or 'custom') was tapped. Combine with the day chosen
    in step 1 and advance to the assignee step."""
    choice = query.data.split(":", 2)[2]
    data = await state.get_data()

    if choice == "custom":
        await query.answer()
        label = data.get("_dl_day_label", "")
        text = f"⌨️ **{label}** uchun soatni yozing — masalan `15:30`:"
        try:
            await query.message.edit_text(text, parse_mode="Markdown",
                                           reply_markup=_newtask_time_kb())
        except TelegramBadRequest:
            await _safe_answer(query.message, text, parse_mode="Markdown")
        return

    try:  # choice is 'HHMM'
        hh, mm = int(choice[:2]), int(choice[2:])
    except (ValueError, IndexError):
        await query.answer("Noto'g'ri vaqt", show_alert=True)
        return
    deadline = _combine_day_time(data.get("_dl_day_iso"), hh, mm)
    if not deadline:
        await query.answer("Vaqtni hisoblab bo'lmadi", show_alert=True)
        return
    await state.update_data(deadline=deadline)
    await state.set_state(NewTaskFSM.awaiting_assignee)
    await query.answer()
    await _newtask_show_assignee(query.message, await state.get_data(), edit=True)


@router.message(StateFilter(NewTaskFSM.awaiting_deadline_time), F.text | F.voice)
async def newtask_deadline_time_text(message: Message, state: FSMContext) -> None:
    """User typed instead of tapping a time slot. Accept a bare 'HH:MM' (combined
    with the day chosen in step 1) or a full natural-language date as fallback."""
    txt = await _get_text_or_transcribe(message, bot=message.bot)
    if txt is None:
        return
    raw = (message.text or "").strip()
    data = await state.get_data()

    # 1) Bare time: "15:30", "15.30", "15 30", or "9" → combine with chosen day.
    import re as _re
    deadline = None
    m = _re.match(r"^(\d{1,2})(?:[:.\s](\d{2}))?$", raw)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2) or 0)
        deadline = _combine_day_time(data.get("_dl_day_iso"), hh, mm)
    # 2) Otherwise try a full natural-language date/time.
    if not deadline:
        parsed, _reason = await _parse_deadline_natural(raw)
        deadline = parsed
    if not deadline:
        await _safe_answer(
            message,
            "🕐 Soatni `HH:MM` ko'rinishida yuboring (masalan `15:30`), "
            "yoki tugmalardan tanlang.",
            parse_mode="Markdown", reply_markup=_newtask_time_kb(),
        )
        return
    await state.update_data(deadline=deadline)
    await state.set_state(NewTaskFSM.awaiting_assignee)
    await _newtask_show_assignee(message, await state.get_data(), edit=False)


@router.message(StateFilter(NewTaskFSM.awaiting_deadline), F.text | F.voice)
async def newtask_deadline_typed(message: Message, state: FSMContext) -> None:
    """At the day picker the user can also just TYPE a full date/time instead of
    tapping a day — parse it directly and jump to the assignee step."""
    txt = await _get_text_or_transcribe(message, bot=message.bot)
    if txt is None:
        return
    parsed, reason = await _parse_deadline_natural((message.text or "").strip())
    if not parsed:
        await _safe_answer(
            message,
            _deadline_error_message(reason, kind="deadline") + "\n\nYoki pastdagi tugmalardan tanlang.",
            parse_mode="Markdown", reply_markup=_newtask_deadline_kb(),
        )
        return
    await state.update_data(deadline=parsed)
    await state.set_state(NewTaskFSM.awaiting_assignee)
    await _newtask_show_assignee(message, await state.get_data(), edit=False)


@router.message(StateFilter(NewTaskFSM.awaiting_deadline_manual), F.text | F.voice)
async def newtask_deadline_manual(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    deadline_iso, reason = await _parse_deadline_natural((message.text or "").strip())
    if not deadline_iso:
        await _safe_answer(
            message,
            _deadline_error_message(reason, kind="deadline") + "\n\nYoki pastdagi presetlardan tanlang.",
            parse_mode="Markdown",
            reply_markup=_newtask_deadline_kb(),
        )
        await state.set_state(NewTaskFSM.awaiting_deadline)
        return
    await state.update_data(deadline=deadline_iso)
    await state.set_state(NewTaskFSM.awaiting_assignee)
    data = await state.get_data()
    await _safe_answer(
        message,
        f"{_newtask_summary(data)}\n" + _SEP + "\n\n"
        "4️⃣ **Ijrochi** (ixtiyoriy):\n\n"
        "Pastdagi tugmalardan biri yoki ism yuboring.",
        parse_mode="Markdown",
        reply_markup=_newtask_assignee_kb(),
    )


@router.callback_query(F.data.startswith("newtask:as:"))
async def newtask_assignee_btn(query: CallbackQuery, state: FSMContext) -> None:
    choice = query.data.split(":", 2)[2]
    if choice == "me":
        await state.update_data(assignee="Men")
    elif choice == "skip":
        await state.update_data(assignee=None)
    await _newtask_show_confirm(query.message, state, edit=True)
    await query.answer()


@router.message(StateFilter(NewTaskFSM.awaiting_assignee), F.text | F.voice)
async def newtask_assignee_text(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    name = (message.text or "").strip()
    if not name or name.startswith("/"):
        await message.answer("Ism bo'sh bo'lmasin. Qaytadan yuboring yoki tugma bosing.",
                              reply_markup=_newtask_assignee_kb())
        return
    if len(name) > 80:
        name = name[:80]
    await state.update_data(assignee=name)
    await _newtask_show_confirm(message, state, edit=False)


async def _newtask_show_confirm(message: Message, state: FSMContext, edit: bool) -> None:
    await state.set_state(NewTaskFSM.awaiting_confirm)
    data = await state.get_data()
    text = (
        "5️⃣ **TASDIQLASH**\n" + _SEP + "\n\n"
        f"{_newtask_summary(data)}\n\n"
        "_Pastdagi tugma orqali yarating yoki bekor qiling._"
    )
    if edit:
        try:
            await message.edit_text(text, parse_mode="Markdown",
                                     reply_markup=_newtask_confirm_kb())
            return
        except TelegramBadRequest:
            pass
    await _safe_answer(message, text, parse_mode="Markdown",
                        reply_markup=_newtask_confirm_kb())


@router.callback_query(F.data == "newtask:confirm")
async def newtask_confirm(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    title = data.get("title")
    if not title:
        await query.answer("Sarlavha kiritilmagan", show_alert=True)
        return
    payload = {
        "title": title,
        "priority": data.get("priority") or "P2",
        "status": "todo",
    }
    if data.get("deadline"):
        payload["deadline"] = data["deadline"]
    if data.get("assignee"):
        payload["assignee"] = data["assignee"]

    tid = await database.create_task(payload)
    await state.clear()
    await query.answer("✅ Vazifa yaratildi")

    task = await database.get_task(tid)
    if not task:
        await _safe_answer(query.message, "✅ Vazifa yaratildi, lekin ko'rsatib bo'lmadi.",
                            reply_markup=main_reply_keyboard())
        return
    text = "✅ **VAZIFA YARATILDI**\n" + _SEP + "\n\n" + _format_task_card(task)
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_task_card_kb_with_back(task))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=_task_card_kb_with_back(task))


@router.callback_query(F.data == "newtask:cancel")
async def newtask_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Bekor qilindi")
    # edit_text faqat inline kbd qabul qiladi — ReplyKeyboardMarkup'ni alohida
    # `_restore_main_keyboard` orqali jo'natamiz.
    try:
        await query.message.edit_text("↩ Yangi vazifa yaratish bekor qilindi.")
    except TelegramBadRequest:
        pass
    await _restore_main_keyboard(query.message)


# ─────────────────────── YANGI ESLATMA — guided FSM ───────────────────────

_NEWREMINDER_REPEAT_LABELS = {
    "once": "bir martalik",
    "daily": "har kuni",
    "weekly": "har hafta",
    "monthly": "har oy",
}


def _newreminder_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏰ 15 daq", callback_data="newrem:time:15m"),
            InlineKeyboardButton(text="🕐 1 soat", callback_data="newrem:time:1h"),
        ],
        [
            InlineKeyboardButton(text="📅 Bugun 17:00", callback_data="newrem:time:today17"),
            InlineKeyboardButton(text="📅 Ertaga 09:00", callback_data="newrem:time:tomorrow9"),
        ],
        [
            InlineKeyboardButton(text="✏️ Qo'lda kiritish", callback_data="newrem:time:manual"),
            InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newrem:cancel"),
        ],
    ])


def _newreminder_repeat_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Bir martalik", callback_data="newrem:repeat:once"),
            InlineKeyboardButton(text="Har kuni", callback_data="newrem:repeat:daily"),
        ],
        [
            InlineKeyboardButton(text="Har hafta", callback_data="newrem:repeat:weekly"),
            InlineKeyboardButton(text="Har oy", callback_data="newrem:repeat:monthly"),
        ],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newrem:cancel")],
    ])


def _newreminder_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Saqlash", callback_data="newrem:confirm"),
            InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newrem:cancel"),
        ],
    ])


def _newreminder_summary(data: dict) -> str:
    title = data.get("title") or "_(kiritilmagan)_"
    remind_at = data.get("remind_at")
    time_label = _reminder_time_chip({"remind_at": remind_at}) if remind_at else "_(tanlanmagan)_"
    repeat = _NEWREMINDER_REPEAT_LABELS.get(data.get("recurrence_rule") or "once", "bir martalik")
    return "\n".join([
        f"📝 Matn: {title}",
        f"⏰ Vaqt: {time_label}",
        f"🔁 Takror: {repeat}",
    ])


def _newreminder_compute_time(preset: str) -> str | None:
    now = datetime.now(database.TZ)
    if preset == "15m":
        return (now + timedelta(minutes=15)).replace(second=0, microsecond=0).isoformat()
    if preset == "1h":
        return (now + timedelta(hours=1)).replace(second=0, microsecond=0).isoformat()
    if preset == "today17":
        target = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.isoformat()
    if preset == "tomorrow9":
        return (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    return None


async def _newreminder_start(message: Message, state: FSMContext) -> None:
    await state.set_state(NewReminderFSM.awaiting_title)
    await state.update_data(title=None, remind_at=None, recurrence_rule=None)
    await _safe_answer(
        message,
        "⏰ **YANGI ESLATMA**\n" + _SEP + "\n\n"
        "1️⃣ Nimani eslatay?\n"
        "_Masalan: «Aziz akaga qo'ng'iroq qilish»_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newrem:cancel")],
        ]),
    )


@router.message(StateFilter(NewReminderFSM.awaiting_title), F.text | F.voice)
async def newreminder_title(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    title = (message.text or "").strip()
    if not title or title.startswith("/"):
        await message.answer("Eslatma matni bo'sh bo'lmasin. Qaytadan yuboring.")
        return
    if len(title) > 220:
        title = title[:220]
    await state.update_data(title=title)
    await state.set_state(NewReminderFSM.awaiting_time)
    data = await state.get_data()
    await _safe_answer(
        message,
        f"{_newreminder_summary(data)}\n" + _SEP + "\n\n"
        "2️⃣ Qachon eslatay?",
        parse_mode="Markdown",
        reply_markup=_newreminder_time_kb(),
    )


@router.callback_query(F.data.startswith("newrem:time:"))
async def newreminder_time(query: CallbackQuery, state: FSMContext) -> None:
    preset = query.data.split(":", 2)[2]
    if preset == "manual":
        await state.set_state(NewReminderFSM.awaiting_time_manual)
        await query.answer()
        text = (
            "📆 **Eslatma vaqtini kiriting**\n\n"
            "Formatlar:\n"
            "• `2026-05-25 14:30`\n"
            "• `25-05 14:30`\n"
            "• `bugun 17:00`, `ertaga 09:00`\n"
            "• `15 daqiqa`, `2 soat`"
        )
        try:
            await query.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="newrem:time:back")],
                    [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="newrem:cancel")],
                ]),
            )
        except TelegramBadRequest:
            await _safe_answer(query.message, text, parse_mode="Markdown")
        return
    if preset == "back":
        await state.set_state(NewReminderFSM.awaiting_time)
        data = await state.get_data()
        await query.answer()
        try:
            await query.message.edit_text(
                f"{_newreminder_summary(data)}\n" + _SEP + "\n\n2️⃣ Qachon eslatay?",
                parse_mode="Markdown",
                reply_markup=_newreminder_time_kb(),
            )
        except TelegramBadRequest:
            pass
        return
    remind_at = _newreminder_compute_time(preset)
    if not remind_at:
        await query.answer("Vaqt preseti noto'g'ri", show_alert=True)
        return
    await state.update_data(remind_at=remind_at)
    await state.set_state(NewReminderFSM.awaiting_repeat)
    data = await state.get_data()
    await query.answer()
    try:
        await query.message.edit_text(
            f"{_newreminder_summary(data)}\n" + _SEP + "\n\n3️⃣ Takrorlansinmi?",
            parse_mode="Markdown",
            reply_markup=_newreminder_repeat_kb(),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, f"{_newreminder_summary(data)}\n\n3️⃣ Takrorlansinmi?",
                           parse_mode="Markdown", reply_markup=_newreminder_repeat_kb())


@router.message(StateFilter(NewReminderFSM.awaiting_time_manual), F.text | F.voice)
async def newreminder_time_manual(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    remind_at, reason = await _parse_deadline_natural((message.text or "").strip())
    if not remind_at:
        await _safe_answer(
            message,
            _deadline_error_message(reason, kind="time"),
            parse_mode="Markdown",
            reply_markup=_newreminder_time_kb(),
        )
        await state.set_state(NewReminderFSM.awaiting_time)
        return
    await state.update_data(remind_at=remind_at)
    await state.set_state(NewReminderFSM.awaiting_repeat)
    data = await state.get_data()
    await _safe_answer(
        message,
        f"{_newreminder_summary(data)}\n" + _SEP + "\n\n3️⃣ Takrorlansinmi?",
        parse_mode="Markdown",
        reply_markup=_newreminder_repeat_kb(),
    )


@router.callback_query(F.data.startswith("newrem:repeat:"))
async def newreminder_repeat(query: CallbackQuery, state: FSMContext) -> None:
    repeat = query.data.split(":", 2)[2]
    if repeat not in _NEWREMINDER_REPEAT_LABELS:
        await query.answer("Takrorlash turi noto'g'ri", show_alert=True)
        return
    await state.update_data(recurrence_rule=None if repeat == "once" else repeat)
    await state.set_state(NewReminderFSM.awaiting_confirm)
    data = await state.get_data()
    await query.answer()
    try:
        await query.message.edit_text(
            "4️⃣ **TASDIQLASH**\n" + _SEP + "\n\n"
            f"{_newreminder_summary(data)}",
            parse_mode="Markdown",
            reply_markup=_newreminder_confirm_kb(),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, _newreminder_summary(data), parse_mode="Markdown",
                           reply_markup=_newreminder_confirm_kb())


@router.callback_query(F.data == "newrem:confirm")
async def newreminder_confirm(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("title") or not data.get("remind_at"):
        await query.answer("Ma'lumot to'liq emas", show_alert=True)
        return
    rid = await database.create_reminder({
        "title": data["title"],
        "remind_at": data["remind_at"],
        "recurrence_rule": data.get("recurrence_rule"),
        "source": "manual_form",
    })
    # If this reminder was started from a note (📝 Qaydlar → Eslatmaga), mark the
    # note processed so it leaves the inbox. Without this the note lingered as
    # unprocessed even after a successful conversion.
    from_note = data.get("from_note_id")
    if from_note:
        await database.mark_note_processed(from_note, "reminder", rid)
    await state.clear()
    reminder = await database.get_reminder(rid)
    await query.answer("⏰ Eslatma saqlandi")
    text = "✅ **ESLATMA SAQLANDI**\n" + _SEP + "\n\n" + _format_reminder_card(reminder)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=reminder_detail_menu(reminder))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=reminder_detail_menu(reminder))


@router.callback_query(F.data == "newrem:cancel")
async def newreminder_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("Bekor qilindi")
    try:
        await query.message.edit_text("↩ Yangi eslatma yaratish bekor qilindi.")
    except TelegramBadRequest:
        await _safe_answer(query.message, "↩ Bekor qilindi.")


_PRINCIPAL_NAME_SHORT = "Maqsud aka"

_UZ_WEEKDAYS = [
    "Dushanba", "Seshanba", "Chorshanba", "Payshanba",
    "Juma", "Shanba", "Yakshanba",
]


def _greeting_for_hour(hour: int) -> str:
    """Kontekstga qarab dinamik salomlashish matni."""
    if 6 <= hour < 11:
        return f"Xayrli tong, {_PRINCIPAL_NAME_SHORT}. Ish kuni boshlanmoqda."
    if 11 <= hour < 16:
        return "Xayrli kun. Kunning yarmi yopildi."
    if 16 <= hour < 20:
        return "Xayrli kech. Kun yakunlanmoqda."
    return "Kech vaqt. Ertangi rejani ko'rib chiqing."


def _date_label(now: datetime) -> str:
    """`Dushanba, 25-may · 14:23`"""
    weekday = _UZ_WEEKDAYS[now.weekday()]
    month = UZ_MONTHS_FULL[now.month - 1]
    return f"{weekday}, {now.day}-{month} · {now.strftime('%H:%M')}"


def _pick_top_task(active: list[dict], overdue: list[dict], unassigned: list[dict],
                    today: list[dict]) -> dict | None:
    """ENG MUHIM tanlash mantig'i (spec'dagi tartibda)."""
    # 1) P0 ochiq + o'tgan
    p0_overdue = [t for t in overdue if t.get("priority") == "P0"]
    if p0_overdue:
        return p0_overdue[0]
    # 2) P0 ochiq
    p0_open = sorted(
        [t for t in active if t.get("priority") == "P0"],
        key=lambda t: t.get("deadline") or "9999",
    )
    if p0_open:
        return p0_open[0]
    # 3) P1 ochiq + bugun deadline
    today_ids = {t["id"] for t in today}
    p1_today = [t for t in active if t.get("priority") == "P1" and t["id"] in today_ids]
    if p1_today:
        return sorted(p1_today, key=lambda t: t.get("deadline") or "9999")[0]
    # 4) Ijrochisiz P0/P1
    p01_unassigned = [t for t in unassigned if t.get("priority") in ("P0", "P1")]
    if p01_unassigned:
        return p01_unassigned[0]
    # 5) yo'q
    return None


def _top_task_status(task: dict, now: datetime) -> str:
    """Top task uchun `Shoshilinch / O'tgan / Bugun` label."""
    parts: list[str] = []
    priority = task.get("priority", "P2")
    if priority == "P0":
        parts.append("Shoshilinch")
    elif priority == "P1":
        parts.append("Muhim")
    deadline = task.get("deadline")
    if deadline:
        try:
            dt = datetime.fromisoformat(deadline).astimezone(database.TZ)
            if dt < now:
                hours = int((now - dt).total_seconds() / 3600)
                parts.append(f"muddati o'tgan ({hours} soat)")
            elif dt.date() == now.date():
                parts.append("bugun")
        except (ValueError, TypeError):
            pass
    return ", ".join(parts) or "—"


def _format_top_task_deadline(deadline_iso: str | None, now: datetime) -> str:
    """ENG MUHIM bloki uchun deadline label: aniq vaqt + farq."""
    if not deadline_iso:
        return "belgilanmagan"
    try:
        dt = datetime.fromisoformat(deadline_iso).astimezone(database.TZ)
    except (ValueError, TypeError):
        return str(deadline_iso)
    if dt < now:
        delta = now - dt
        if delta.total_seconds() < 24 * 3600:
            hours = int(delta.total_seconds() / 3600)
            label = f"{hours} soat kechikkan"
        else:
            days = delta.days
            label = f"{days} kun kechikkan"
        return f"{dt.strftime('%d-%m %H:%M')} · {label}"
    if dt.date() == now.date():
        return f"Bugun {dt.strftime('%H:%M')}"
    if (now + timedelta(days=1)).date() == dt.date():
        return f"Ertaga {dt.strftime('%H:%M')}"
    return dt.strftime("%d-%m %H:%M")


async def _cockpit_anomalies(now: datetime, loads: dict) -> list[str]:
    """Anomaliya signali — odatdagidan og'ish bo'lgan pattern'larni topish.

    Topiladigan signallar:
    1. Aktiv vazifasi bor ijrochi 3+ kun hech narsa yopmagan
    2. So'nggi haftada Shoshilinch vazifalar avvalgi haftadan 2x+ ko'p
    3. Ijrochisiz vazifalar chegaradan oshib ketdi
    """
    import aiosqlite
    anomalies: list[str] = []
    cutoff_3d = (now - timedelta(days=3)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()
    cutoff_14d = (now - timedelta(days=14)).isoformat()

    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row

        # 1. Ijrochilar bo'sh turishi (active va done = 0)
        for name, info in loads.items():
            if name == "belgilanmagan" or info.get("active", 0) < 2:
                continue
            cur = await db.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE assignee = ? AND status = 'done' AND updated_at >= ?",
                (name, cutoff_3d),
            )
            closed = (await cur.fetchone())["n"]
            if closed == 0:
                anomalies.append(
                    f"**{name}** so'nggi 3 kunda 0 ta vazifa yopdi ({info['active']} ta aktiv)"
                )

        # 2. P0 vazifalar spike
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE priority = 'P0' AND created_at >= ?",
            (cutoff_7d,),
        )
        p0_recent = (await cur.fetchone())["n"]
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE priority = 'P0' AND created_at >= ? AND created_at < ?",
            (cutoff_14d, cutoff_7d),
        )
        p0_prior = (await cur.fetchone())["n"]
        if p0_recent >= 3 and p0_recent >= 2 * max(1, p0_prior):
            ratio = round(p0_recent / max(1, p0_prior), 1)
            anomalies.append(
                f"So'nggi haftada **{p0_recent} ta yangi Shoshilinch** vazifa — avvalgi haftadan {ratio}x ko'p"
            )

        # 3. Eskalatsiya: 3+ overdue va 3+ P0 — eskirayotgan vaziyat
        cur = await db.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE status IN ('todo','in_progress') "
            "AND priority = 'P0' AND deadline IS NOT NULL AND deadline < ?",
            (now.isoformat(),),
        )
        p0_overdue = (await cur.fetchone())["n"]
        if p0_overdue >= 2:
            anomalies.append(
                f"**{p0_overdue} ta Shoshilinch vazifa muddati o'tgan** — kritik eskalatsiya"
            )

    return anomalies[:4]  # max 4 signal


def _cockpit_time_budget(today_meetings: list, today_tasks: list, now: datetime) -> dict:
    """Bugungi vaqt taqsimoti — uchrashuvlar, deadline'lar, zich oraliqlar."""
    # Uchrashuv soatlari
    meeting_hours = 0.0
    for m in today_meetings:
        try:
            start = datetime.fromisoformat(m["datetime_start"])
            end_iso = m.get("datetime_end")
            if end_iso:
                end = datetime.fromisoformat(end_iso)
            else:
                end = start + timedelta(hours=1)  # default 1 soat
            meeting_hours += max(0, (end - start).total_seconds() / 3600)
        except (ValueError, TypeError):
            meeting_hours += 1.0

    # Deadline'larni soat bo'yicha tasniflash (bugun yopilishi kerak bo'lganlar)
    deadline_buckets: dict[int, int] = {}
    for t in today_tasks:
        deadline = t.get("deadline")
        if not deadline:
            continue
        try:
            dt = datetime.fromisoformat(deadline).astimezone(database.TZ)
            if dt.date() == now.date():
                deadline_buckets[dt.hour] = deadline_buckets.get(dt.hour, 0) + 1
        except (ValueError, TypeError):
            pass

    # Eng zich oraliq (3 soatlik window)
    peak_window = None
    peak_count = 0
    if deadline_buckets:
        sorted_hours = sorted(deadline_buckets.keys())
        for h in sorted_hours:
            window_count = sum(deadline_buckets.get(h + i, 0) for i in range(3))
            if window_count > peak_count:
                peak_count = window_count
                peak_window = (h, h + 2)

    return {
        "meeting_hours": round(meeting_hours, 1),
        "meeting_count": len(today_meetings),
        "deadline_total": sum(deadline_buckets.values()),
        "peak_window": peak_window,
        "peak_count": peak_count,
    }


def _cockpit_recommendation(signals: dict) -> str:
    """Spec asosida tavsiya — prioritet bo'yicha qoidalar.
    Risk skor olib tashlanganligi sababli endi to'g'ridan-to'g'ri counts asosida ishlaydi.
    """
    overdue_n = signals["counts"]["overdue"]
    unassigned_n = signals["counts"]["unassigned"]
    p0_open = signals["by_priority"].get("P0", 0)
    no_dl_n = signals["counts"]["no_deadline"]
    top_loaded = signals.get("top_loaded")
    next_title = signals.get("next_task_title")
    overload_threshold = 4

    # Kritik holat: ko'p o'tgan + ko'p shoshilinch — vaziyat boshqaruvdan chiqqan
    if overdue_n >= 5 and p0_open >= 2:
        return ("Vaziyat kritik. Avval **muddati o'tgan** vazifalarni yoping yoki "
                "yangi deadline qo'ying. Keyin Shoshilinch ro'yxatini ko'ring.")
    if overdue_n >= 3:
        first_title = signals.get("oldest_overdue_title") or "vazifa"
        return (f"**{overdue_n} ta vazifa muddati o'tgan**. Eng eskisini "
                f"(«{first_title}») bugun yoping yoki delegatsiya qiling.")
    if p0_open >= 3:
        return (f"Bir vaqtning o'zida **{p0_open} ta Shoshilinch** vazifa ochiq. "
                "Haqiqatdan hammasi shoshilinchmi? Ba'zilarini Muhim ga ko'chiring.")
    if unassigned_n >= 5:
        return (f"**{unassigned_n} ta vazifaga ijrochi tayinlanmagan**. Avval "
                "mas'ul belgilang — javobgarliksiz vazifa kechikadi.")
    if no_dl_n >= 5:
        return (f"**{no_dl_n} ta vazifada deadline yo'q**. Muddatsiz vazifalar "
                "amalga oshmaydi. Har biriga sana belgilang.")
    if top_loaded and top_loaded.get("active", 0) >= overload_threshold:
        return (f"**{top_loaded['name']}** ortiqcha yuklangan ({top_loaded['active']} ta). "
                "Yangi vazifa berishni vaqtincha to'xtating yoki muddatlarni cho'zing.")
    if next_title:
        return f"Vaziyat nazoratda. Bugun **{_truncate(next_title, 50)}** bilan boshlang."
    return "Kun rejasini ko'rib chiqing va prioritetlarni belgilang."


async def _cockpit_compute_signals(now: datetime) -> dict:
    """Barcha DB chaqiruvlarni parallel bajarib, signallarni jamlaydi."""
    # Parallel
    (active, overdue, today, today_meetings, unassigned, no_deadline,
     risk, loads, upcoming) = await asyncio.gather(
        database.list_tasks(status_in=["todo", "in_progress"], limit=500),
        database.list_overdue_tasks(),
        database.list_today_tasks(),
        database.list_today_meetings(),
        database.list_unassigned_tasks(limit=200),
        database.list_tasks_without_deadline(limit=200),
        compute_risk_score(),
        database.assignee_load_map(),
        database.list_upcoming_meetings(within_minutes=1440 * 2),  # 2 kun
    )
    # Anomaliya va vaqt bo'limi — loads va today ma'lumotlariga bog'liq, ketma-ket
    anomalies = await _cockpit_anomalies(now, loads)
    time_budget = _cockpit_time_budget(today_meetings, today, now)
    by_p = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for t in active:
        p = t.get("priority", "P2")
        if p in by_p:
            by_p[p] += 1

    # 24h/48h deadline
    due_24 = [t for t in active if (t.get("deadline") or "") and
              t["deadline"] <= (now + timedelta(hours=24)).isoformat() and
              t["deadline"] >= now.isoformat()]
    due_48 = [t for t in active if (t.get("deadline") or "") and
              t["deadline"] <= (now + timedelta(hours=48)).isoformat() and
              t["deadline"] >= now.isoformat()]

    # Team load — eng yuklangan va eng bo'sh
    real_assignees = [d for n, d in loads.items() if n != "belgilanmagan" and d["active"] > 0]
    top_loaded = max(real_assignees, key=lambda d: d["active"], default=None)
    lightest = min(real_assignees, key=lambda d: d["active"], default=None) if real_assignees else None

    top_task = _pick_top_task(active, overdue, unassigned, today)

    # Next task (top_task'dan boshqa — recommendation uchun)
    next_task = None
    for t in sorted(active, key=lambda t: (
        {"P0": 0, "P1": 1, "P2": 2, "P3": 3}.get(t.get("priority", "P2"), 2),
        t.get("deadline") or "9999",
    )):
        if top_task and t["id"] == top_task["id"]:
            continue
        next_task = t
        break

    # Eng yaqin uchrashuv (bugun yoki ertaga)
    next_meeting = None
    for m in upcoming:
        try:
            dt = datetime.fromisoformat(m["datetime_start"]).astimezone(database.TZ)
            if dt >= now and (dt - now).total_seconds() <= 48 * 3600:
                next_meeting = m
                break
        except (ValueError, TypeError):
            continue

    return {
        "now": now,
        "risk": risk,
        "by_priority": by_p,
        "counts": {
            "active": len(active),
            "today": len(today),
            "overdue": len(overdue),
            "unassigned": len(unassigned),
            "no_deadline": len(no_deadline),
            "due_24h": len(due_24),
            "due_48h": len(due_48),
        },
        "top_task": top_task,
        "top_loaded": top_loaded,
        "lightest": lightest,
        "next_meeting": next_meeting,
        "next_task_title": (next_task.get("title") if next_task else None) or
                            (top_task.get("title") if top_task else None),
        "oldest_overdue_title": (overdue[0].get("title") if overdue else None),
        "anomalies": anomalies,
        "time_budget": time_budget,
        "overload_threshold": _ASSIGNEE_OVERLOAD,
    }


def _build_cockpit_panel(signals: dict) -> str:
    """Cockpit matnini blok-stilda yaratish."""
    DIVIDER = "━" * 20
    now = signals["now"]

    # ── Block 1: Sarlavha ──
    blocks: list[list[str]] = [[
        "🎛 **BOSHQARUV PANELI**",
        "",
        _greeting_for_hour(now.hour),
        _date_label(now),
    ]]

    # Empty state — 0 aktiv vazifa + 0 uchrashuv
    if signals["counts"]["active"] == 0 and not signals["next_meeting"]:
        blocks.append([
            "✨ **Vaziyat tinch**",
            "",
            "Hozir aktiv vazifa va uchrashuv yo'q.",
        ])
        blocks.append([
            "💡 **TAVSIYA**",
            "",
            "Bugun keyingi haftani rejalashtirish uchun yaxshi vaqt. Yangi "
            "maqsadlar belgilang yoki uzoq muddatli loyihaga vaqt ajrating.",
        ])
        return _join_blocks(blocks, DIVIDER)

    # ── Block 2: Bugungi holat (Risk skor olib tashlandi — foydalanuvchi so'rovi) ──
    c = signals["counts"]
    by_p = signals["by_priority"]
    overdue_prefix = "🚨 " if c["overdue"] > 0 else ""
    unassigned_prefix = "⚠️ " if c["unassigned"] > 0 else ""
    blocks.append([
        "📌 **BUGUNGI HOLAT**",
        "",
        f"Aktiv vazifalar: **{c['active']}** ta (🔴 {by_p.get('P0', 0)} shoshilinch · 🟠 {by_p.get('P1', 0)} muhim)",
        f"Bugun yopilishi kerak: **{c['today']}** ta",
        f"{overdue_prefix}Muddati o'tgan: **{c['overdue']}** ta",
        f"{unassigned_prefix}Ijrochisiz: **{c['unassigned']}** ta",
        "",
        f"24 soat ichida deadline: {c['due_24h']} ta",
        f"48 soat ichida deadline: {c['due_48h']} ta",
    ])

    # ── Block 4: Eng muhim (top_task) ──
    top = signals.get("top_task")
    if top:
        title = (top.get("title") or "—").strip()
        assignee = (top.get("assignee") or "").strip() or "belgilanmagan"
        deadline_label = _format_top_task_deadline(top.get("deadline"), now)
        status_label = _top_task_status(top, now)
        blocks.append([
            "⚡ **ENG MUHIM**",
            "",
            title,
            "",
            f"      ⏰ Muddat:        {deadline_label}",
            f"      👤 Ijrochi:        {assignee}",
            f"      ⚡ Holat:          {status_label}",
        ])

    # ── Block 5: Jamoa yuklamasi ──
    top_loaded = signals.get("top_loaded")
    lightest = signals.get("lightest")
    overload = signals["overload_threshold"]
    if top_loaded or lightest or c["unassigned"]:
        team_lines = ["👥 **JAMOA YUKLAMASI**", ""]
        if top_loaded:
            overload_mark = " 🔴 ortiqcha" if top_loaded["active"] >= overload else ""
            team_lines.append(f"Eng yuklangan: **{top_loaded['name']}** — {top_loaded['active']} ta aktiv{overload_mark}")
        if lightest and (not top_loaded or lightest["name"] != top_loaded["name"]):
            team_lines.append(f"Eng bo'sh: **{lightest['name']}** — {lightest['active']} ta aktiv")
        if c["unassigned"]:
            team_lines.append(f"Belgilanmagan: **{c['unassigned']}** ta vazifa")
        blocks.append(team_lines)

    # ── Block 6: Keyingi uchrashuv ──
    nm = signals.get("next_meeting")
    if nm:
        try:
            dt = datetime.fromisoformat(nm["datetime_start"]).astimezone(database.TZ)
            if dt.date() == now.date():
                time_label = f"Bugun {dt.strftime('%H:%M')}"
            elif (now + timedelta(days=1)).date() == dt.date():
                time_label = f"Ertaga {dt.strftime('%H:%M')}"
            else:
                time_label = dt.strftime("%d-%m %H:%M")
        except (ValueError, TypeError):
            time_label = "—"
        title = (nm.get("title") or "—").strip()
        parts = nm.get("participants") or []
        plabel = ", ".join(parts[:3]) if parts else "belgilanmagan"
        if len(parts) > 3:
            plabel += f" (+{len(parts) - 3} nafar)"
        blocks.append([
            "📆 **KEYINGI UCHRASHUV**",
            "",
            f"{time_label} — {title}",
            f"👥 {plabel}",
        ])

    # ── Block 7: AI Tavsiya ──
    blocks.append([
        "💡 **TIZIM TAVSIYASI**",
        "",
        _cockpit_recommendation(signals),
    ])

    # ── Block 8: 🚨 ANOMALIYA SIGNALI ──
    anomalies = signals.get("anomalies") or []
    if anomalies:
        anomaly_lines = ["🚨 **ANOMALIYA**", ""]
        for a in anomalies:
            anomaly_lines.append(f"• {a}")
        blocks.append(anomaly_lines)

    # ── Block 9: ⏳ VAQT BO'LIMI ──
    tb = signals.get("time_budget") or {}
    if tb.get("meeting_count", 0) > 0 or tb.get("deadline_total", 0) > 0:
        tb_lines = ["⏳ **BUGUNGI VAQT**", ""]
        if tb.get("meeting_count", 0) > 0:
            tb_lines.append(
                f"      🤝 Uchrashuv:         {tb['meeting_hours']} soat ({tb['meeting_count']} ta)"
            )
        if tb.get("deadline_total", 0) > 0:
            tb_lines.append(
                f"      📌 Vazifa deadline:   {tb['deadline_total']} ta"
            )
        if tb.get("peak_window") and tb.get("peak_count", 0) >= 2:
            start_h, end_h = tb["peak_window"]
            tb_lines.append("")
            tb_lines.append(
                f"      🚨 Zich oraliq:       "
                f"{start_h:02d}:00–{end_h+1:02d}:00 ({tb['peak_count']} ta deadline)"
            )
        blocks.append(tb_lines)

    return _join_blocks(blocks, DIVIDER)


def _join_blocks(blocks: list[list[str]], divider: str) -> str:
    """Block ro'yxatini divider + bo'sh qator bilan birlashtirish."""
    result: list[str] = []
    for i, block in enumerate(blocks):
        if i > 0:
            result.extend(["", divider, ""])
        result.extend(block)
    return "\n".join(result)


@router.message(Command("cockpit"))
async def cmd_cockpit(message: Message, state: FSMContext | None = None) -> None:
    """🎛 Boshqaruv Paneli — professional operational dashboard.

    Spec-based 8-block layout: sarlavha + risk + bugungi holat + eng muhim
    + jamoa + keyingi uchrashuv + AI tavsiya + 24h delta.
    """
    now = datetime.now(database.TZ)
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        signals = await _cockpit_compute_signals(now)
        text = _build_cockpit_panel(signals)
    finally:
        typing_task.cancel()

    # Inline kbd with drill-down rows so users can jump straight from the
    # cockpit into the underlying panels (team, risks, stats, delegations)
    # without leaving and finding the section buttons in the main keyboard.
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👥 Ijrochilar", callback_data="cockpit_team"),
            InlineKeyboardButton(text="🚨 Risklar", callback_data="cockpit_risks"),
        ],
        [
            InlineKeyboardButton(text="📊 Statistika", callback_data="cockpit_stats"),
            InlineKeyboardButton(text="📋 Delegatsiyalar", callback_data="cockpit_delegations"),
        ],
        [InlineKeyboardButton(text="🔄 Yangilash", callback_data="cockpit_refresh")],
    ])

    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)




@router.callback_query(F.data == "cockpit_refresh")
async def cb_cockpit_refresh(query: CallbackQuery) -> None:
    await query.answer("Yangilanmoqda...")
    await cmd_cockpit(query.message)


@router.callback_query(F.data == "cockpit_team")
async def cb_cockpit_team(query: CallbackQuery) -> None:
    await query.answer()
    await cmd_team(query.message)


@router.callback_query(F.data == "cockpit_risks")
async def cb_cockpit_risks(query: CallbackQuery) -> None:
    await query.answer()
    await cmd_risks(query.message)






@router.callback_query(F.data == "cockpit_stats")
async def cb_cockpit_stats(query: CallbackQuery) -> None:
    await query.answer()
    days, label = 7, "Oxirgi 7 kun"
    stats = await database.executive_stats(days=days)
    await _safe_answer(
        query.message,
        _format_stats_dashboard(stats, label),
        parse_mode="Markdown",
        reply_markup=_stats_period_keyboard(days),
    )


@router.callback_query(F.data == "cockpit_delegations")
async def cb_cockpit_delegations(query: CallbackQuery) -> None:
    await query.answer()
    await cmd_delegations(query.message)


# ─────────────────────── MESSAGE HANDLERS ───────────────────────


# ─────────────────────── VOICE CONFIRMATION ───────────────────────


def _escape_markdown(text: str) -> str:
    """Telegram Markdown V1 special chars: *, _, `, [, ]."""
    if not text:
        return ""
    for c in ("_", "*", "`", "[", "]"):
        text = text.replace(c, "\\" + c)
    return text


_VOICE_CONFIRM_KEYWORDS = {
    "confirm": {"tasdiqlayman", "tasdiq", "ha", "to'g'ri", "togri", "to'gri",
                "yes", "ok", "okay", "qabul", "davom"},
    "cancel":  {"yo'q", "yoq", "bekor", "no", "qayt", "to'xtat", "toxtat"},
    "edit":    {"tahrir", "tahrirla", "boshqacha", "noto'g'ri", "notogri",
                "qayta", "o'zgartir", "ozgartir"},
}


def _classify_voice_response(text: str) -> str | None:
    """Quick keyword classifier for confirm/cancel/edit. Returns None if ambiguous."""
    t = (text or "").lower().strip()
    if not t:
        return None
    # Direct single-word match
    words = set(t.replace(",", " ").replace(".", " ").split())
    for key, vocab in _VOICE_CONFIRM_KEYWORDS.items():
        if words & vocab:
            return key
    # Substring match (handles "tasdiqlayman.", "bekor qilaman" etc.)
    for key, vocab in _VOICE_CONFIRM_KEYWORDS.items():
        if any(v in t for v in vocab):
            return key
    return None


async def _send_voice_confirm_prompt(message: Message, state: FSMContext, transcript: str) -> None:
    """Show the confirm/edit/cancel prompt and store the transcript in FSM state."""
    DIVIDER = "━" * 20
    text = (
        "🎙 **OVOZ XABARINGIZ**\n\n"
        f"{DIVIDER}\n\n"
        f"\"{_escape_markdown(transcript)}\"\n\n"
        f"{DIVIDER}\n\n"
        "Tushundimi to'g'ri?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Tasdiqlayman", callback_data="voice_ok"),
            InlineKeyboardButton(text="✏️ Tahrirlayman", callback_data="voice_edit"),
        ],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="voice_cancel")],
    ])
    await state.set_state(VoiceConfirmFSM.awaiting_action)
    await state.update_data(transcript=transcript)
    await _safe_answer(message, text, parse_mode="Markdown", reply_markup=kb)


async def _download_voice(message: Message, bot: Bot) -> bytes | None:
    """Download a voice file from Telegram. Returns None if size exceeds limit."""
    if message.voice.file_size and message.voice.file_size > voice_service.MAX_AUDIO_BYTES:
        await message.answer(
            f"Ovoz xabari juda katta ({message.voice.file_size // 1024} KB). "
            f"Iltimos, {voice_service.MAX_AUDIO_BYTES // (1024 * 1024)} MB dan kichikroq yuboring."
        )
        return None
    file = await bot.get_file(message.voice.file_id)
    audio_io = await bot.download_file(file.file_path)
    if hasattr(audio_io, "getvalue"):
        return audio_io.getvalue()
    if hasattr(audio_io, "read"):
        return audio_io.read()
    return bytes(audio_io)


def _forward_signal(message: Message) -> tuple[str | None, str | None]:
    """Extract (source_chat_label, author_label) from a Telegram forward.
    Returns (None, None) if the message isn't a forward.

    aiogram 3 unified forward metadata into `message.forward_origin`
    (MessageOrigin variants). Older clients/updates still populate the
    legacy `forward_from` / `forward_from_chat` attributes — check both."""
    chat_label: str | None = None
    author_label: str | None = None

    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        # Try the union variants we care about.
        chat = getattr(origin, "chat", None)
        if chat is not None:
            chat_label = getattr(chat, "title", None) or getattr(chat, "username", None)
        sender_user = getattr(origin, "sender_user", None)
        if sender_user is not None:
            author_label = " ".join(filter(None, [
                getattr(sender_user, "first_name", None),
                getattr(sender_user, "last_name", None),
            ])).strip() or None
        sender_name = getattr(origin, "sender_user_name", None)
        if sender_name and not author_label:
            author_label = sender_name

    # Legacy fields (still set by some clients)
    legacy_chat = getattr(message, "forward_from_chat", None)
    if legacy_chat and not chat_label:
        chat_label = getattr(legacy_chat, "title", None) or getattr(legacy_chat, "username", None)
    legacy_user = getattr(message, "forward_from", None)
    if legacy_user and not author_label:
        author_label = " ".join(filter(None, [
            getattr(legacy_user, "first_name", None),
            getattr(legacy_user, "last_name", None),
        ])).strip() or None
    legacy_name = getattr(message, "forward_sender_name", None)
    if legacy_name and not author_label:
        author_label = legacy_name

    if chat_label or author_label or origin or legacy_chat or legacy_user or legacy_name:
        return chat_label, author_label
    return None, None


@router.message(
    # A forward is an explicit "save this" gesture — capture it as a note in the
    # default chat AND while browsing any section. Previously this was
    # default_state only, so forwarding *inside a section* fell through to the
    # section's text handler -> _process_and_reply -> Claude, which reinterpreted
    # the forwarded message instead of saving it (reported: "meaning changed").
    # Active input FSMs (NewTask, reminders, meeting edit/protocol, …) are
    # intentionally NOT listed, so a forward there still serves as that flow's input.
    StateFilter(
        default_state,
        SectionFSM.in_tasks, SectionFSM.in_reminders, SectionFSM.in_meetings,
        SectionFSM.in_stats, SectionFSM.in_team, SectionFSM.in_risks,
        SectionFSM.in_today, SectionFSM.in_new, SectionFSM.in_search,
        SectionFSM.in_settings, SectionFSM.in_notes,
    ),
    F.forward_origin | F.forward_from | F.forward_from_chat | F.forward_sender_name,
)
async def handle_forwarded_message(message: Message, state: FSMContext) -> None:
    """Auto-capture: any forwarded message in default_state becomes a note.

    Empty forwards (stickers without caption, etc.) are rejected with a
    short explanation. Otherwise we save the text/caption with provenance
    metadata and confirm with two quick-actions (Inbox / Tahlil)."""
    chat_label, author_label = _forward_signal(message)
    content = (message.text or message.caption or "").strip()
    if not content:
        await message.answer(
            "📎 Bo'sh forward — note yaratilmadi. Matn yoki izoh bo'lgan xabarni forward qiling."
        )
        return
    # html_text preserves Telegram entities (bold, italic, code, links) as HTML
    # tags. Used by _format_note_detail to render the forwarded text inside a
    # native <blockquote> with original formatting intact.
    html_content: str | None = None
    try:
        # aiogram 3 provides html_text for text and caption_html_text for media.
        html_content = (getattr(message, "html_text", None)
                         or getattr(message, "caption_html_text", None))
    except Exception:
        html_content = None
    nid = await database.create_note({
        "content": content,
        "content_html": html_content,
        "source": "forward",
        "source_chat": chat_label,
        "source_author": author_label,
        "source_message_id": message.message_id,
    })
    source_hint = "forward"
    if chat_label:
        source_hint = f"forward · {chat_label}"
    elif author_label:
        source_hint = f"forward · {author_label}"
    await _note_capture_reply(message, nid, source_hint)
    await _maybe_refresh_section(message, state, {"note": [nid]})


@router.message(StateFilter(default_state), F.voice)
async def handle_voice(message: Message, bot: Bot, state: FSMContext) -> None:
    """Free-form ovoz handler: transkripsiya → auto-process (default) yoki
    confirm prompt (agar foydalanuvchi /settings da yoqib qo'ygan bo'lsa).

    Faqat FSM state'siz holatda ishga tushadi. Boshqa FSM state aktiv bo'lsa
    (MeetingRescheduleFSM, MeetingEditFSM, MeetingProtocolFSM va boshqalar),
    o'sha state'ning maxsus handler'i ovozni qabul qiladi.
    """
    audio_bytes = await _download_voice(message, bot)
    if audio_bytes is None:
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    transcript = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
    if not transcript:
        await message.answer("Ovozni o'qiy olmadim. Iltimos, qaytadan urinib ko'ring yoki matn yozing.")
        return

    # Variant A+B: respect the voice_auto_confirm setting.
    settings = await database.get_settings()
    if settings.get("voice_auto_confirm", True):
        # Default — show the transcript first (so the user sees what was heard)
        # then immediately dispatch to Claude without an extra tap. Any
        # destructive action (create_task / schedule_meeting) still gets a
        # confirm prompt downstream via confirm_create_actions.
        await message.answer(
            f"_🎙 Tushundim:_ {_escape_markdown(transcript[:400])}",
            parse_mode="Markdown",
        )
        await _process_and_reply(message, transcript, state=state)
    else:
        # Legacy flow — explicit confirm/edit/cancel buttons before processing.
        await _send_voice_confirm_prompt(message, state, transcript)


@router.callback_query(F.data == "voice_ok")
async def cb_voice_ok(query: CallbackQuery, state: FSMContext) -> None:
    """Confirmed transcript → process with Claude."""
    data = await state.get_data()
    transcript = (data.get("transcript") or "").strip()
    await state.clear()
    if not transcript:
        await query.answer("Matn topilmadi", show_alert=True)
        return
    await query.answer("✅ Tasdiqlandi")
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    await _process_and_reply(query.message, transcript, state=state)


@router.callback_query(F.data == "voice_cancel")
async def cb_voice_cancel(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer("✕ Bekor qilindi")
    try:
        await query.message.edit_text(
            "✕ Bekor qilindi. Yangi xabar yuboring.",
            reply_markup=None,
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "acts_confirm")
async def cb_actions_confirm(query: CallbackQuery, state: FSMContext) -> None:
    """User tapped ✅ on a create-action preview — execute the deferred Claude
    response now, send the original user_message reply, AND restore the prior
    section state so the section auto-refresh sees the new item."""
    data = await state.get_data()
    response = data.get("pending_response")
    prior_state = data.get("_prior_section")
    note_id = data.get("_note_id")  # set when confirming a note-analyze action
    await state.clear()
    if not response or not isinstance(response, dict):
        await query.answer("Tasdiqlash vaqti o'tdi — yangi so'rov yuboring.",
                            show_alert=True)
        return
    await query.answer("✅ Tasdiqlandi")
    # Strip the confirm keyboard so it can't be re-tapped while we execute.
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
    try:
        ids_by_type = await _execute_actions(response.get("actions", []))
    except Exception:
        logger.exception("Deferred _execute_actions failed after confirm")
        await query.message.answer(
            "❌ Yaratishda xato yuz berdi. Logni tekshiring (/diagnostics)."
        )
        return
    # If this came from a note-analyze confirm, mark the source note processed.
    if note_id:
        for key in ("task", "reminder"):
            if ids_by_type.get(key):
                try:
                    await database.mark_note_processed(note_id, key, ids_by_type[key][0])
                except Exception:
                    logger.debug("mark_note_processed failed for %s", note_id)
                break
    keyboard = _build_keyboard(response.get("buttons", []), ids_by_type)
    if keyboard:
        keyboard = _append_back_row(keyboard)
    text = (response.get("user_message") or "").strip() or "✅ Yaratildi"
    text += _failed_actions_note(ids_by_type)
    await _safe_answer(query.message, text,
                        parse_mode="Markdown", reply_markup=keyboard)
    # Restore prior section state and auto-refresh the section list so the
    # new item is visible (otherwise the user sees a stale list higher in
    # the chat and thinks the create failed).
    if prior_state and isinstance(prior_state, str):
        try:
            await state.set_state(prior_state)
        except Exception:
            logger.debug("Could not restore prior state %r", prior_state)
    await _maybe_refresh_section(query.message, state, ids_by_type)


@router.callback_query(F.data == "acts_cancel")
async def cb_actions_cancel(query: CallbackQuery, state: FSMContext) -> None:
    """User tapped ✕ on a create-action preview — drop the deferred response."""
    await state.clear()
    await query.answer("✕ Bekor qilindi")
    try:
        await query.message.edit_text(
            "✕ **Yaratish bekor qilindi.**\n\nYangi xabar yuboring.",
            parse_mode="Markdown",
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "voice_edit")
async def cb_voice_edit(query: CallbackQuery, state: FSMContext) -> None:
    """Switch to revision state — next message (text or voice) replaces the transcript."""
    await state.set_state(VoiceConfirmFSM.awaiting_revision)
    await query.answer()
    text = (
        "✏️ **TAHRIRLASH**\n\n"
        "Yangi matn yoki ovoz yuboring — transkripsiyani almashtiraman."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="voice_cancel")],
    ])
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


@router.message(StateFilter(VoiceConfirmFSM.awaiting_revision), F.text | F.voice)
async def handle_voice_revision(message: Message, bot: Bot, state: FSMContext) -> None:
    """User sent a correction during voice edit flow — re-transcribe (if voice) and show confirm again."""
    if message.voice:
        audio_bytes = await _download_voice(message, bot)
        if audio_bytes is None:
            return
        await message.bot.send_chat_action(message.chat.id, "typing")
        new_transcript = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not new_transcript:
            await message.answer("Ovozni o'qiy olmadim. Matn yozing.")
            return
    else:
        new_transcript = (message.text or "").strip()
        if not new_transcript:
            await message.answer("Bo'sh xabar. Yangi matn yuboring.")
            return
    await _send_voice_confirm_prompt(message, state, new_transcript)


# ─────────────── POLISH OUTPUT ACTIONS (📋 Nusxa / 📤 Yuborish / ✎ Yana tahrir) ───────────────
# These callbacks ('copy', 'share', 'edit:polish') are emitted by Claude in the
# POLISH response's buttons array — they had no handlers, so the buttons did
# nothing (copy/share) or hit the task editor with id='polish' → "Vazifa topilmadi".

def _extract_polished_body(message: Message) -> str:
    """Pull just the polished letter out of a 'Tahrirlangan matn:' card —
    header line and ─── rules stripped — so it can be re-sent clean."""
    raw = (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()
    out: list[str] = []
    for ln in raw.split("\n"):
        s = ln.strip()
        if not s:
            out.append("")
            continue
        if "Tahrirlangan matn" in s or "Tahrirlangan xat" in s:
            continue
        if set(s) <= set("─—-_=•· "):  # a line made only of rule/bullet chars
            continue
        out.append(ln)
    return "\n".join(out).strip()


@router.callback_query(F.data == "copy")
async def cb_polish_copy(query: CallbackQuery) -> None:
    """📋 Nusxa olish — re-send the polished text alone so it's easy to
    long-press → copy (a bot can't write to the clipboard directly)."""
    body = _extract_polished_body(query.message)
    if not body:
        await query.answer("Nusxa olinadigan matn topilmadi", show_alert=True)
        return
    await query.answer("📋 Toza matn pastda — bosib turib nusxa oling")
    await query.message.answer(body)


@router.callback_query(F.data == "share")
async def cb_polish_share(query: CallbackQuery) -> None:
    """📤 Boshqaga yuborish — re-send the polished text as a standalone message
    the user can forward to the recipient."""
    body = _extract_polished_body(query.message)
    if not body:
        await query.answer("Yuboriladigan matn topilmadi", show_alert=True)
        return
    await query.answer()
    await query.message.answer(body)
    await query.message.answer(
        "📤 _Yuqoridagi xabarni kerakli odamga forward qiling._",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "edit:polish")
async def cb_polish_edit(query: CallbackQuery, state: FSMContext) -> None:
    """✎ Yana tahrir — refine the polished text. Registered BEFORE the generic
    edit:<task-id> handler so it isn't mis-routed to the task editor."""
    body = _extract_polished_body(query.message)
    await state.set_state(PolishRevisionFSM.awaiting)
    await state.update_data(polish_original=body)
    await query.answer()
    await query.message.answer(
        "✏️ **Qanday o'zgartiray?**\n\nKo'rsatma yoki yangi matn yuboring "
        "(masalan: «qisqartir», «rasmiyroq qil», «iliqroq ohang»).",
        parse_mode="Markdown",
    )


@router.message(StateFilter(PolishRevisionFSM.awaiting), F.text | F.voice)
async def handle_polish_revision(message: Message, state: FSMContext) -> None:
    """User sent a revision instruction for the polished text — re-polish it
    (original + instruction) through the normal pipeline, which returns a fresh
    polished card with the same buttons."""
    instr = await _get_text_or_transcribe(message, bot=message.bot)
    if instr is None:
        return
    data = await state.get_data()
    original = data.get("polish_original", "")
    await state.clear()
    combined = (
        "Quyidagi rasmiy matnni ko'rsatma bo'yicha qayta tahrirla (polish). "
        f"Ko'rsatma: {(message.text or '').strip()}\n\nAsl matn:\n{original}"
    )
    await _process_and_reply(message, combined, state=state)


@router.message(StateFilter(VoiceConfirmFSM.awaiting_action), F.text | F.voice)
async def handle_voice_action(message: Message, bot: Bot, state: FSMContext) -> None:
    """User responded with text/voice instead of pressing a button — classify intent."""
    if message.voice:
        audio_bytes = await _download_voice(message, bot)
        if audio_bytes is None:
            return
        response_text = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not response_text:
            await message.answer("Ovozni o'qiy olmadim. Tugmalarni bosing yoki matn yuboring.")
            return
    else:
        response_text = (message.text or "").strip()

    intent = _classify_voice_response(response_text)
    data = await state.get_data()
    transcript = (data.get("transcript") or "").strip()

    if intent == "confirm":
        await state.clear()
        if transcript:
            await message.answer("✅ Tasdiqlandi")
            await _process_and_reply(message, transcript, state=state)
        return
    if intent == "cancel":
        await state.clear()
        await message.answer("✕ Bekor qilindi. Yangi xabar yuboring.")
        return
    if intent == "edit":
        await state.set_state(VoiceConfirmFSM.awaiting_revision)
        await message.answer(
            "✏️ Yangi matn yoki ovoz yuboring.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✕ Bekor qilish", callback_data="voice_cancel")],
            ]),
        )
        return

    # Ambiguous — treat as a brand-new transcript and re-ask.
    await _send_voice_confirm_prompt(message, state, response_text)


_REPLY_BUTTON_LABELS: set[str] = {
    BTN_COCKPIT, BTN_TODAY, BTN_TASKS, BTN_REMINDERS, BTN_TEAM, BTN_RISKS,
    BTN_NEW, BTN_STATS, BTN_SEARCH, BTN_MEETINGS, BTN_SETTINGS,
}



async def _restore_main_keyboard(message: Message) -> None:
    """Pastdagi reply kbd ni Asosiy menyuga qaytarish."""
    await message.answer(
        "🎛 **Asosiy menyu**\n\nKerakli bo'limni tanlang yoki matn/ovoz yuboring.",
        parse_mode="Markdown",
        reply_markup=main_reply_keyboard(),
    )


@router.message(F.text.func(lambda t: t and t.strip() in _REPLY_BUTTON_LABELS | set(_LEGACY_BTN_TASKS)))
async def handle_main_reply_button(message: Message, state: FSMContext) -> None:
    """Asosiy menyu reply tugmalari — har qanday holatda ishlasin
    (section'dan chiqib boshqa bo'limga o'tish uchun)."""
    label = message.text.strip()
    current = await state.get_state()
    if current is not None:
        await state.clear()
    if label == BTN_COCKPIT:
        await _restore_main_keyboard(message); await cmd_cockpit(message); return
    if label == BTN_TODAY:
        await cmd_today(message, state); return
    if label == BTN_TASKS or label in _LEGACY_BTN_TASKS:
        await cmd_tasks(message, state); return
    if label == BTN_REMINDERS:
        await cmd_reminders(message, state); return
    if label == BTN_TEAM:
        await cmd_team(message, state); return
    if label == BTN_RISKS:
        await cmd_risks(message, state); return
    if label == BTN_NEW:
        await cmd_new(message, state); return
    if label == BTN_STATS:
        await cmd_stats(message, state); return
    if label == BTN_SEARCH:
        await cmd_search(message, state); return
    if label == BTN_MEETINGS:
        await cmd_meetings(message, state); return
    if label == BTN_SETTINGS:
        await cmd_settings(message, state); return


@router.message(F.text == BTN_BACK_MAIN)
async def handle_back_to_main(message: Message, state: FSMContext) -> None:
    """⬅️ Asosiy menyu — har qanday section'dan asosiy menyuga qaytadi."""
    await state.clear()
    await _restore_main_keyboard(message)


@router.message(StateFilter(SectionFSM.in_tasks), F.text | F.voice)
async def handle_tasks_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Vazifalar bo'limidagi reply tugmalari (state == in_tasks)."""
    label = message.text.strip()
    if label in _TASKS_SECTION_FILTERS:
        await _render_tasks_for_filter(message, _TASKS_SECTION_FILTERS[label])
        return
    if label == TBTN_TASKS_NEW:
        await _safe_answer(
            message,
            "➕ **YANGI VAZIFA**\n\nMatn yoki ovoz yuboring. Misol:\n"
            "_\"Ertaga ertalab Aziz akaga marketing hisobotini yuborish\"_",
            parse_mode="Markdown",
        )
        return
    if label == TBTN_TASKS_SEARCH:
        # Vazifalar bo'limidan kelganda task-only qidiruv (TaskSearchFSM)
        # ishga tushiriladi — global Qidiruv bo'limiga sakramaymiz.
        await state.set_state(TaskSearchFSM.awaiting_query)
        await _safe_answer(
            message,
            "🔍 **VAZIFA QIDIRISH**\n\n"
            "Sarlavha, tavsif, teg yoki ijrochi bo'yicha so'z yuboring.",
            parse_mode="Markdown",
        )
        return
    # Boshqa matn — Claude'ga yuborish (section state'da ham erkin xabar mumkin)
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_reminders), F.text | F.voice)
async def handle_reminders_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Eslatmalar bo'limidagi reply tugmalari."""
    label = message.text.strip()
    if label in _REMINDERS_SECTION_FILTERS:
        await _render_reminders_for_filter(message, _REMINDERS_SECTION_FILTERS[label])
        return
    if label == RBTN_REMINDERS_NEW:
        await _newreminder_start(message, state)
        return
    if label == RBTN_REMINDERS_SEARCH:
        await state.set_state(ReminderSearchFSM.awaiting_query)
        await _safe_answer(
            message,
            "🔍 **ESLATMA QIDIRISH**\n\nQidiruv so'zini yuboring.",
            parse_mode="Markdown",
        )
        return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_meetings), F.text | F.voice)
async def handle_meetings_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Uchrashuvlar bo'limidagi reply tugmalari (state == in_meetings)."""
    label = message.text.strip()
    if label in _MEETINGS_SECTION_FILTERS:
        await _render_meetings_for_filter(message, _MEETINGS_SECTION_FILTERS[label])
        return
    if label == MBTN_MEETINGS_NEW:
        await _safe_answer(
            message,
            "➕ **YANGI UCHRASHUV**\n\nMatn yoki ovoz yuboring. Misol:\n"
            "_\"Ertaga soat 12:00 da Dinislam bilan biznes forum\"_",
            parse_mode="Markdown",
        )
        return
    if label == MBTN_MEETINGS_SEARCH:
        await cmd_search(message); return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_stats), F.text | F.voice)
async def handle_stats_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Statistika bo'limidagi reply tugmalari (state == in_stats)."""
    label = message.text.strip()
    if label in _STATS_SECTION_PERIODS:
        days = _STATS_SECTION_PERIODS[label]
        stats = await database.executive_stats(days=days)
        period_label = {1: "Bugun", 7: "7 kun", 30: "30 kun"}.get(days, f"{days} kun")
        await _safe_answer(
            message, _format_stats_dashboard(stats, period_label),
            parse_mode="Markdown",
        )
        return
    if label == SBTN_STATS_REPORT_WEEK:
        stats = await database.executive_stats(days=7)
        await _safe_answer(message, _format_executive_report(stats, "Oxirgi 7 kun"),
                            parse_mode="Markdown", reply_markup=_report_keyboard(7))
        return
    if label == SBTN_STATS_REPORT_MONTH:
        stats = await database.executive_stats(days=30)
        await _safe_answer(message, _format_executive_report(stats, "Oxirgi 30 kun"),
                            parse_mode="Markdown", reply_markup=_report_keyboard(30))
        return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_team), F.text | F.voice)
async def handle_team_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Ijrochilar bo'limidagi reply tugmalari (state == in_team)."""
    label = message.text.strip()
    if label == YBTN_TEAM_REFRESH:
        await _render_team_panel(message); return
    if label == YBTN_TEAM_UNASSIGNED:
        tasks = await database.list_unassigned_tasks(limit=50)
        text = _format_tasks_compact(tasks, "Ijrochisiz vazifalar")
        await _safe_answer(message, text, parse_mode="Markdown",
                            reply_markup=tasks_compact_keyboard(tasks, "all"))
        return
    if label == YBTN_TEAM_REASSIGN:
        await _safe_answer(
            message,
            "🔄 **QAYTA TAQSIMLASH**\n\n"
            "Vazifa raqamini va yangi ijrochini yuboring.\n"
            "_Misol: \"3-vazifani Sanjarga ber\"_",
            parse_mode="Markdown",
        )
        return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_risks), F.text | F.voice)
async def handle_risks_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Risklar bo'limidagi reply tugmalari (state == in_risks)."""
    label = message.text.strip()
    if label == RBTN_RISKS_REFRESH:
        await _render_risks_panel(message); return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_today), F.text | F.voice)
async def handle_today_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Bugun bo'limidagi reply tugmalari (state == in_today)."""
    label = message.text.strip()
    if label == DBTN_TODAY_EVENING:
        # Re-use cb_today_evening logic without callback object
        typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
        try:
            response = await claude_service.process_message(
                "", internal_directive="[INTERNAL] generate_evening_summary"
            )
        finally:
            typing_task.cancel()
        text = (response.get("user_message") or "").strip()
        if text:
            await _safe_answer(message, text, parse_mode="Markdown")
        else:
            await message.answer("Hozircha yakun chiqarib bo'lmadi.")
        return
    if label == DBTN_TODAY_ALL_TASKS:
        await cmd_tasks(message, state); return
    if label == DBTN_TODAY_NEW_TASK:
        await _safe_answer(
            message,
            "📝 **YANGI VAZIFA**\n\nMatn yoki ovoz yuboring. Misol:\n"
            "_\"Ertaga ertalab Aziz akaga marketing hisobotini yuborish\"_",
            parse_mode="Markdown",
        )
        return
    if label == DBTN_TODAY_MEETINGS:
        await cmd_meetings(message, state); return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_new), F.text | F.voice)
async def handle_new_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Yangi bo'limidagi reply tugmalari (state == in_new)."""
    label = message.text.strip()
    prompts = {
        NBTN_NEW_TASK: ("📝 **YANGI VAZIFA**\n\nMatn yoki ovoz yuboring. Misol:\n"
                        "_\"Ertaga ertalab Aziz akaga marketing hisoboti\"_"),
        NBTN_NEW_MEETING: ("🤝 **YANGI UCHRASHUV**\n\nMatn yoki ovoz yuboring. Misol:\n"
                           "_\"Juma 15:00 da Olim aka bilan byudjet uchrashuvi\"_"),
        NBTN_NEW_VOICE: ("🎙 **OVOZLI VAZIFA**\n\nMikrofon tugmasini bosib o'zbekcha gapiring. "
                         "Men transkripsiya qilib, vazifani tushunaman."),
        NBTN_NEW_POLISH: ("✏️ **MATN TAHRIRLASH**\n\nXabaringizni yuboring va aytib qo'ying — "
                          "kimga, qanday tonda. Misol:\n_\"Aziz akaga rasmiy qil: ertaga hisobot tayyor\"_"),
    }
    if label == NBTN_NEW_REMINDER:
        await _newreminder_start(message, state)
        return
    if label == NBTN_NEW_NOTE:
        # Enter the one-shot capture FSM — next text/voice becomes a note.
        await state.set_state(NoteCaptureFSM.awaiting_text)
        await _safe_answer(
            message,
            "📥 **YANGI QAYD**\n\nMatn yoki ovoz yuboring — bot uni Inbox'ga saqlaydi.\n"
            "Bekor qilish: /cancel",
            parse_mode="Markdown",
        )
        return
    if label in prompts:
        await _safe_answer(message, prompts[label], parse_mode="Markdown")
        return
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_search), F.text | F.voice)
async def handle_search_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Qidiruv bo'limidagi reply tugmalari (state == in_search).
    Scope tugmasi tanlanmasa, matn to'g'ridan-to'g'ri qidiruv so'zi sifatida ishlatiladi.
    """
    label = message.text.strip()
    scope_map = {
        QBTN_SEARCH_TASKS:    "tasks",
        QBTN_SEARCH_MEETINGS: "meetings",
        QBTN_SEARCH_CONTACTS: "contacts",
        QBTN_SEARCH_ALL:      "all",
    }
    if label in scope_map:
        await state.update_data(scope=scope_map[label])
        scope_label = {
            "tasks": "📌 Faqat vazifalar",
            "meetings": "🤝 Faqat uchrashuvlar",
            "contacts": "👥 Faqat kontaktlar",
            "all": "🗂 Hammasi",
        }[scope_map[label]]
        await message.answer(
            f"✅ Scope: **{scope_label}**\n\nQidiruv so'zini yuboring.",
            parse_mode="Markdown",
        )
        return
    # Matn — qidirish bajariladi
    data = await state.get_data()
    scope = data.get("scope", "all")
    results = await database.search_all(label, limit=30)
    parts = []
    if scope in ("tasks", "all") and results.get("tasks"):
        parts.append(_format_tasks_compact(results["tasks"][:10], f"Qidiruv: {label}"))
    if scope in ("meetings", "all") and results.get("meetings"):
        parts.append(_format_meetings_compact(results["meetings"][:10], f"Qidiruv: {label}"))
    if scope == "all" and results.get("reminders"):
        parts.append(_format_reminders_compact(results["reminders"][:10], f"Qidiruv: {label}"))
    if scope in ("contacts", "all") and results.get("contacts"):
        cl = results["contacts"][:10]
        lines = [f"👥 **KONTAKTLAR** · {len(cl)} ta", ""]
        for c in cl:
            lines.append(f"• {c['name']} ({c.get('role') or '—'})")
        parts.append("\n".join(lines))
    if not parts:
        await message.answer(f"Hech narsa topilmadi: «{label}»")
        return
    await _safe_answer(message, "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(parts),
                       parse_mode="Markdown")


@router.message(StateFilter(SectionFSM.in_notes), F.text | F.voice)
async def handle_notes_section_button(message: Message, state: FSMContext) -> None:
    """Reply-keyboard tugmalari Qaydlar bo'limida. Inbox/Ishlangan/Arxiv —
    filter; '➕ Yangi note' → one-shot capture FSM; '🔍 Qidirish' → search
    flow; matn fall-through Claude'ga, voice ham."""
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return
    label = message.text.strip()
    if label in _NOTES_SECTION_FILTERS:
        await _render_notes_for_filter(message, _NOTES_SECTION_FILTERS[label])
        return
    if label == NBTN_NOTES_NEW:
        await state.set_state(NoteCaptureFSM.awaiting_text)
        await _safe_answer(
            message,
            "📥 **YANGI QAYD**\n\nMatn yoki ovoz yuboring — bot uni Inbox'ga saqlaydi.\n"
            "Bekor qilish: /cancel",
            parse_mode="Markdown",
        )
        return
    if label == NBTN_NOTES_SEARCH:
        # Reuse the global search FSM but pre-flag a notes-only view via
        # a hint in state.data; the existing flow handles the rest.
        await state.set_state(GlobalSearchFSM.awaiting_query)
        await state.update_data(_search_scope="notes")
        await _safe_answer(
            message,
            "🔍 **QAYD QIDIRISH**\n\nKalit so'z yoki ibora yuboring.",
            parse_mode="Markdown",
        )
        return
    # Fall-through: free text/voice → Claude (might create a note via LLM).
    await _process_and_reply(message, message.text, state=state)


@router.message(StateFilter(SectionFSM.in_settings), F.text | F.voice)
async def handle_settings_section_button(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """Sozlamalar bo'limidagi reply tugmalari (state == in_settings)."""
    label = _msg_text.strip()
    if label == GBTN_SETTINGS_NOTIFY:
        settings = await database.get_settings()
        new_val = not settings["notifications_enabled"]
        await database.set_setting("notifications_enabled", new_val)
        await message.answer(
            f"🔔 Bildirishnomalar: **{'yoqildi ✅' if new_val else 'oʻchirildi ❌'}**",
            parse_mode="Markdown",
        )
        return
    if label == GBTN_SETTINGS_BRIEFING:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f"brieftime:{t}") for t in ("07:00", "07:30", "08:00")],
            [InlineKeyboardButton(text=t, callback_data=f"brieftime:{t}") for t in ("08:30", "09:00", "10:00")],
        ])
        await message.answer("⏰ **Ertalab brifing vaqti:**", parse_mode="Markdown", reply_markup=kb)
        return
    if label == GBTN_SETTINGS_EVENING:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=f"eveningtime:{t}") for t in ("17:00", "17:30", "18:00")],
            [InlineKeyboardButton(text=t, callback_data=f"eveningtime:{t}") for t in ("18:30", "19:00", "20:00")],
        ])
        await message.answer("🌙 **Kechki yakun vaqti:**", parse_mode="Markdown", reply_markup=kb)
        return
    if label == GBTN_SETTINGS_REMINDER:
        settings = await database.get_settings()
        text = (
            "📲 **Eslatma parametrlari**\n\n"
            f"• Uchrashuv: `{settings['meeting_reminder_min']} daq` oldin\n"
            f"• Vazifa: `{settings['task_reminder_hours']} soat` oldin (Shoshilinch va Muhim)"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="15 daq", callback_data="meetremind:15"),
             InlineKeyboardButton(text="30 daq", callback_data="meetremind:30"),
             InlineKeyboardButton(text="60 daq", callback_data="meetremind:60")],
            [InlineKeyboardButton(text="1 soat", callback_data="taskremind:1"),
             InlineKeyboardButton(text="2 soat", callback_data="taskremind:2"),
             InlineKeyboardButton(text="4 soat", callback_data="taskremind:4")],
        ])
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)
        return
    if label == GBTN_SETTINGS_VOICE:
        settings = await database.get_settings()
        new_val = not settings.get("voice_auto_confirm", True)
        await database.set_setting("voice_auto_confirm", new_val)
        if new_val:
            text = (
                "🎙 **Ovoz tasdig'i:** AVTO - tasdiqsiz ishlaydi ✅\n\n"
                "Ovoz transkripti darhol qayta ishlanadi."
            )
        else:
            text = (
                "🎙 **Ovoz tasdig'i:** tasdiq so'raladi ✅\n\n"
                "Har bir ovoz xabarida transkript ko'rsatiladi; "
                "tasdiqlasangizgina davom etadi."
            )
        await message.answer(text, parse_mode="Markdown")
        return
    if label == GBTN_SETTINGS_CREATE_CONFIRM:
        settings = await database.get_settings()
        new_val = not settings.get("confirm_create_actions", True)
        await database.set_setting("confirm_create_actions", new_val)
        if new_val:
            text = (
                "✅ **Yaratish tasdig'i:** yoqildi ✅\n\n"
                "Vazifa yoki uchrashuv yaratishdan oldin tasdiq so'raladi."
            )
        else:
            text = (
                "✅ **Yaratish tasdig'i:** o'chirildi ✅\n\n"
                "Vazifa va uchrashuvlar tasdiqsiz yaratiladi."
            )
        await message.answer(text, parse_mode="Markdown")
        return
    if label == GBTN_SETTINGS_CALENDAR:
        status = "yoqilgan" if config.ICLOUD_ENABLED else "oʻchirilgan"
        cal_name = config.ICLOUD_CALENDAR_NAME or "(default)"
        text = (
            "📅 **Kalendar holati**\n\n"
            f"iCloud sync: **{status}**\n"
            f"Kalendar: `{cal_name}`\n"
            f"Sync interval: `{config.ICLOUD_SYNC_INTERVAL_MIN} daq`"
        )
        await message.answer(text, parse_mode="Markdown")
        return
    await _process_and_reply(message, _msg_text, state=state)


@router.message(StateFilter(default_state), F.text)
async def handle_text(message: Message, state: FSMContext) -> None:
    """Free-form text (faqat FSM state'siz holatda). Aktiv state'da
    o'sha state'ning maxsus handler'i tomonidan qabul qilinadi."""
    if message.text.startswith("/"):
        return
    await _process_and_reply(message, message.text, state=state)


# ─────────────────────── CALLBACK HANDLERS ───────────────────────


async def _delete_if_possible(message: Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramBadRequest:
        # Message too old / already deleted / not modifiable — benign.
        # Narrow except prevents masking unexpected errors (network, programming).
        pass


@router.callback_query(F.data == "nav_cockpit")
async def cb_nav_cockpit(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await _delete_if_possible(query.message)
    await cmd_cockpit(query.message)


@router.callback_query(F.data == "nav_new")
async def cb_nav_new(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await _delete_if_possible(query.message)
    await cmd_new(query.message)


@router.callback_query(F.data == "nav_settings")
async def cb_nav_settings(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await _delete_if_possible(query.message)
    await cmd_settings(query.message)




@router.callback_query(F.data == "nav_meetings")
async def cb_nav_meetings(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await query.answer()
    await _render_meetings_for_filter(query.message, "week", edit_existing=True)



@router.callback_query(F.data.startswith("confirm:"))
async def cb_confirm(query: CallbackQuery) -> None:
    await query.answer("Tasdiqlandi ✅")
    try:
        await query.message.edit_reply_markup(reply_markup=single_back_keyboard())
    except Exception:
        pass


@router.callback_query(F.data.startswith("task_del:"))
async def cb_task_del_confirm(query: CallbackQuery) -> None:
    """Two-step task delete: first show confirmation prompt."""
    tid = query.data.split(":", 1)[1]
    task = await database.get_task(tid)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer()
    title = (task.get("title") or "—").strip()
    text = (
        "🗑 **VAZIFANI O'CHIRISH**\n\n"
        f"{title}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Haqiqatdan ham o'chirmoqchimisiz?"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha, o'chir", callback_data=f"task_del_do:{tid}"),
            InlineKeyboardButton(text="⬅️ Yo'q", callback_data=f"taskopen:{tid}"),
        ],
    ])
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("task_del_do:"))
async def cb_task_del_do(query: CallbackQuery) -> None:
    """Execute task deletion after confirmation."""
    tid = query.data.split(":", 1)[1]
    await database.delete_task(tid)
    await query.answer("✅ Vazifa o'chirildi")
    try:
        await query.message.edit_text(
            "🗑 Vazifa o'chirildi.",
            reply_markup=single_back_keyboard("taskfilter:active"),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(query: CallbackQuery) -> None:
    parts = query.data.split(":", 1)
    target_id = parts[1] if len(parts) > 1 else ""
    if target_id.startswith("t-"):
        await database.delete_task(target_id)
        await query.answer("Vazifa bekor qilindi ✕")
        back_target = "taskfilter:active"
    elif target_id.startswith("m-"):
        await database.cancel_meeting(target_id)
        sched = scheduler_module.get_scheduler()
        if sched:
            try:
                sched.remove_meeting_reminder(target_id)
            except Exception:
                logger.exception("Failed to remove meeting reminder for %s", target_id)
        if config.ICLOUD_ENABLED:
            try:
                await asyncio.to_thread(calendar_service.delete_meeting, target_id)
            except Exception:
                pass
        await query.answer("Uchrashuv bekor qilindi ✕")
        back_target = "meetingfilter:week"
    else:
        await query.answer("Bekor qilindi")
        back_target = "nav_cockpit"
    try:
        await query.message.edit_reply_markup(reply_markup=single_back_keyboard(back_target))
    except Exception:
        pass


@router.callback_query(F.data.startswith("complete:"))
async def cb_complete(query: CallbackQuery) -> None:
    parts = query.data.split(":", 1)
    target_id = parts[1] if len(parts) > 1 else ""
    if not target_id:
        await query.answer()
        return
    await database.complete_task(target_id)
    await query.answer("Bajarildi ✅")
    # Cardni vizual yangilash — foydalanuvchi yangi statusni darrov ko'rsin.
    # Plus add an "Undo" button so a fat-finger tap can be reversed within
    # the next minute without hunting through the task detail menu.
    task = await database.get_task(target_id)
    if task:
        undo_row = InlineKeyboardButton(text="↶ Bekor qilish", callback_data=f"unclomp:{target_id}")
        kb = _task_card_kb_with_back(task)
        # Prepend the undo row so it's the most prominent action.
        kb = InlineKeyboardMarkup(inline_keyboard=[[undo_row]] + list(kb.inline_keyboard))
        try:
            await query.message.edit_text(
                _format_task_card(task), parse_mode="Markdown",
                reply_markup=kb,
            )
            return
        except TelegramBadRequest:
            pass
    # Fallback — agar matn yangilanmasa, kamida tugmalarni yangilab qo'yamiz.
    try:
        await query.message.edit_reply_markup(
            reply_markup=single_back_keyboard("taskfilter:active"))
    except Exception:
        pass


@router.callback_query(F.data.startswith("unclomp:"))
async def cb_uncomplete(query: CallbackQuery) -> None:
    """Undo a task completion. Flips status back to 'todo' and re-renders
    the card so the user can immediately recover from a fat-finger tap."""
    parts = query.data.split(":", 1)
    target_id = parts[1] if len(parts) > 1 else ""
    if not target_id:
        await query.answer()
        return
    ok = await database.update_task(target_id, {"status": "todo"}, source="undo_complete")
    if not ok:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer("↶ Yana aktiv ✅")
    task = await database.get_task(target_id)
    if task:
        try:
            await query.message.edit_text(
                _format_task_card(task), parse_mode="Markdown",
                reply_markup=_task_card_kb_with_back(task),
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("task_detail:"))
async def cb_task_detail(query: CallbackQuery) -> None:
    """⋯ Batafsil — show the full action menu for a task."""
    tid = query.data.split(":", 1)[1]
    task = await database.get_task(tid)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer()
    text = _format_task_detail_card(task)
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=task_detail_menu(task))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                           reply_markup=task_detail_menu(task))


@router.callback_query(F.data.startswith("set_assignee:"))
async def cb_set_assignee(query: CallbackQuery, state: FSMContext) -> None:
    """👤 Ijrochi — prompt user for assignee name (free text or 'Men' for self)."""
    tid = query.data.split(":", 1)[1]
    task = await database.get_task(tid)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await state.set_state(AssigneeFSM.awaiting_name)
    await state.update_data(task_id=tid)
    await query.answer()
    current = task.get("assignee") or "belgilanmagan"
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧹 Tozalash", callback_data=f"clear_assignee:{tid}")],
        [InlineKeyboardButton(text="✕ Bekor qilish", callback_data=f"taskopen:{tid}")],
    ])
    await query.message.answer(
        f"👤 **Ijrochi tayinlash**\n\nHozir: {current}\n\n"
        f"Yangi ism yuboring (masalan: «Komilov Javohir» yoki «Men»):",
        parse_mode="Markdown",
        reply_markup=cancel_kb,
    )


@router.callback_query(F.data.startswith("clear_assignee:"))
async def cb_clear_assignee(query: CallbackQuery, state: FSMContext) -> None:
    tid = query.data.split(":", 1)[1]
    await state.clear()
    await database.update_task(tid, {"assignee": None}, source="edit")
    await query.answer("Ijrochi tozalandi ✅")
    task = await database.get_task(tid)
    if task:
        try:
            await query.message.edit_text(
                _format_task_card(task), parse_mode="Markdown",
                reply_markup=_task_card_kb_with_back(task),
            )
        except TelegramBadRequest:
            pass


@router.message(StateFilter(AssigneeFSM.awaiting_name), F.text | F.voice)
async def handle_assignee_input(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    data = await state.get_data()
    tid = data.get("task_id")
    if not tid:
        await state.clear()
        return
    name = message.text.strip()[:80]
    if not name:
        await message.answer("Boʻsh nom qabul qilinmadi. Qaytadan yuboring yoki bekor qiling.")
        return
    await state.clear()
    await database.update_task(tid, {"assignee": name}, source="edit")
    task = await database.get_task(tid)
    if task:
        await _safe_answer(
            message, f"✅ Ijrochi: **{name}**\n\n" + _format_task_card(task),
            parse_mode="Markdown",
            reply_markup=_task_card_kb_with_back(task),
        )


def _reschedule_presets_keyboard(mid: str) -> InlineKeyboardMarkup:
    """6 ta tezkor preset + qo'lda kiritish + bekor."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏰ Bugun 16:00", callback_data=f"resched_preset:{mid}:today_16"),
            InlineKeyboardButton(text="⏰ Bugun 18:00", callback_data=f"resched_preset:{mid}:today_18"),
        ],
        [
            InlineKeyboardButton(text="📅 Ertaga 09:00", callback_data=f"resched_preset:{mid}:tomorrow_9"),
            InlineKeyboardButton(text="📅 Ertaga 14:00", callback_data=f"resched_preset:{mid}:tomorrow_14"),
        ],
        [
            InlineKeyboardButton(text="📅 +3 kun, 10:00", callback_data=f"resched_preset:{mid}:plus3"),
            InlineKeyboardButton(text="📅 Keyingi hafta", callback_data=f"resched_preset:{mid}:next_week"),
        ],
        [InlineKeyboardButton(text="✏️ Qo'lda kiritish", callback_data=f"resched_manual:{mid}")],
        [InlineKeyboardButton(text="⬅️ Bekor", callback_data=f"meetingopen:{mid}")],
    ])


def _resched_preset_to_datetime(
    key: str,
    now: datetime,
    meeting_start: datetime | None = None,
) -> datetime | None:
    """Map preset key to a concrete future datetime (TZ-aware).

    Today-presets roll to tomorrow if the chosen clock time has already passed.
    `next_week` preserves the original meeting's clock time (or 10:00 if missing).
    `meeting_start` — current datetime_start of the meeting (used for next_week).
    """
    today = now.replace(second=0, microsecond=0)

    def _today_or_tomorrow_at(hour: int) -> datetime:
        candidate = today.replace(hour=hour, minute=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if key == "today_16":
        return _today_or_tomorrow_at(16)
    if key == "today_18":
        return _today_or_tomorrow_at(18)
    if key == "tomorrow_9":
        return (today + timedelta(days=1)).replace(hour=9, minute=0)
    if key == "tomorrow_14":
        return (today + timedelta(days=1)).replace(hour=14, minute=0)
    if key == "plus3":
        # +3 kun, asl uchrashuv vaqtini saqlash (yo'q bo'lsa 10:00)
        if meeting_start:
            base = meeting_start.astimezone(database.TZ)
            return (today + timedelta(days=3)).replace(hour=base.hour, minute=base.minute)
        return (today + timedelta(days=3)).replace(hour=10, minute=0)
    if key == "next_week":
        # +7 kun — asl uchrashuv vaqtini saqlash
        if meeting_start:
            base = meeting_start.astimezone(database.TZ)
            return (today + timedelta(days=7)).replace(hour=base.hour, minute=base.minute)
        return (today + timedelta(days=7)).replace(hour=10, minute=0)
    return None


async def _apply_reschedule(mid: str, new_start: datetime) -> dict | None:
    """Persist new start (and shift end by same delta), trigger iCloud re-sync,
    and re-register the meeting reminder job at the new time.
    Returns the updated meeting dict or None if not found.
    """
    meeting = await database.get_meeting(mid)
    if not meeting:
        return None
    try:
        old_start = datetime.fromisoformat(meeting["datetime_start"])
    except (ValueError, TypeError):
        return None
    if new_start.tzinfo is None:
        new_start = database.TZ.localize(new_start)
    delta = new_start - old_start.astimezone(database.TZ)
    new_end_iso = None
    if meeting.get("datetime_end"):
        try:
            old_end = datetime.fromisoformat(meeting["datetime_end"])
            new_end_iso = (old_end + delta).isoformat()
        except (ValueError, TypeError):
            pass
    import aiosqlite
    async with aiosqlite.connect(config.DATABASE_PATH) as db:
        if new_end_iso:
            await db.execute(
                "UPDATE meetings SET datetime_start=?, datetime_end=?, "
                "reminded_at=NULL, prep_sent_at=NULL, followup_sent_at=NULL "
                "WHERE id=?",
                (new_start.isoformat(), new_end_iso, mid),
            )
        else:
            await db.execute(
                "UPDATE meetings SET datetime_start=?, "
                "reminded_at=NULL, prep_sent_at=NULL, followup_sent_at=NULL "
                "WHERE id=?",
                (new_start.isoformat(), mid),
            )
        await db.commit()

    # Re-register the reminder at the new time. APScheduler replaces
    # the existing job with the same id, so the old reminder is cancelled.
    sched = scheduler_module.get_scheduler()
    if sched:
        try:
            sched.schedule_meeting_reminder(mid, new_start.isoformat())
        except Exception:
            logger.exception("Failed to reschedule reminder for %s", mid)

    # iCloud re-sync (background — delete old event, push new one)
    if config.ICLOUD_ENABLED:
        _spawn_background(_resync_meeting_to_icloud(mid), name=f"icloud_resync:{mid}")

    return await database.get_meeting(mid)


@router.callback_query(F.data.startswith("reschedule:"))
async def cb_reschedule(query: CallbackQuery) -> None:
    """Show the time-preset picker for a meeting."""
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer()
    current = _meeting_time_label(meeting.get("datetime_start") or "", with_past_marker=False)
    DIVIDER = "━" * 20
    text = "\n".join([
        "🔄 **YANGI VAQT TANLANG**",
        "",
        f"Hozirgi vaqt: {current}",
        "",
        DIVIDER,
        "",
        "Tezkor variantlar:",
    ])
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_reschedule_presets_keyboard(mid))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=_reschedule_presets_keyboard(mid))


@router.callback_query(F.data.startswith("resched_preset:"))
async def cb_resched_preset(query: CallbackQuery) -> None:
    """Apply a preset reschedule."""
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Xato format", show_alert=True)
        return
    mid, key = parts[1], parts[2]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    now = datetime.now(database.TZ)
    meeting_start: datetime | None = None
    try:
        meeting_start = datetime.fromisoformat(meeting["datetime_start"]).astimezone(database.TZ)
    except (ValueError, TypeError, KeyError):
        meeting_start = None
    new_start = _resched_preset_to_datetime(key, now, meeting_start)
    if not new_start:
        await query.answer("Preset noma'lum", show_alert=True)
        return
    updated = await _apply_reschedule(mid, new_start)
    if not updated:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    label = _meeting_time_label(updated.get("datetime_start") or "", with_past_marker=False)
    await query.answer(f"✅ Vaqt {label} ga ko'chirildi")
    try:
        await query.message.edit_text(
            _format_meeting_card(updated, show_date=True),
            parse_mode="Markdown",
            reply_markup=meeting_inline_actions(updated),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, _format_meeting_card(updated, show_date=True),
                            parse_mode="Markdown", reply_markup=meeting_inline_actions(updated))


class MeetingRescheduleFSM(StatesGroup):
    awaiting_datetime = State()


@router.callback_query(F.data.startswith("resched_manual:"))
async def cb_resched_manual(query: CallbackQuery, state: FSMContext) -> None:
    """Ask the user to type or speak the new date/time. Claude parses on receipt."""
    mid = query.data.split(":", 1)[1]
    await state.set_state(MeetingRescheduleFSM.awaiting_datetime)
    await state.update_data(meeting_id=mid)
    await query.answer()
    text = (
        "✏️ **YANGI VAQT**\n\n"
        "Yangi sana va vaqtni yozib yuboring yoki ovoz xabar yuboring.\n\n"
        "_Misol: \"28-may 11:00\" yoki \"ertaga ertalab 09:00\"_"
    )
    await query.message.edit_text(text, parse_mode="Markdown",
                                   reply_markup=single_back_keyboard(f"meetingopen:{mid}",
                                                                     text="✕ Bekor qilish"))


@router.message(StateFilter(MeetingRescheduleFSM.awaiting_datetime), F.text | F.voice)
async def handle_resched_manual(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    mid = data.get("meeting_id")
    await state.clear()
    if not mid:
        await message.answer("Uchrashuv topilmadi.")
        return
    # Resolve user text — accept voice too.
    if message.voice:
        await message.bot.send_chat_action(message.chat.id, "typing")
        file = await bot.get_file(message.voice.file_id)
        audio_io = await bot.download_file(file.file_path)
        audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
        user_text = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not user_text:
            await message.answer("Ovozni o'qiy olmadim. Matn yuboring.")
            return
    else:
        user_text = (message.text or "").strip()
    if not user_text:
        await message.answer("Bo'sh xabar — bekor qilindi.")
        return
    # Ask Claude to parse the date/time. Use an internal directive that returns
    # a normalized ISO datetime in user_message, or "INVALID" if it cannot parse.
    directive = (
        "[INTERNAL] parse_reschedule_datetime\n\n"
        f"User input: {user_text!r}\n"
        f"Current time: {datetime.now(database.TZ).isoformat()}\n"
        "Return ONLY the ISO 8601 datetime (Asia/Tashkent, +05:00) in user_message, "
        "for example '2026-05-28T11:00:00+05:00'. "
        "If you cannot determine a clear future datetime, return user_message='INVALID'."
    )
    response = await claude_service.process_message("", internal_directive=directive)
    raw = (response.get("user_message") or "").strip()
    new_start: datetime | None = None
    if raw and raw.upper() != "INVALID":
        try:
            new_start = datetime.fromisoformat(raw)
            if new_start.tzinfo is None:
                new_start = database.TZ.localize(new_start)
        except (ValueError, TypeError):
            new_start = None
    if not new_start:
        await message.answer(
            "Vaqtni tushuna olmadim. Misol: \"28-may 11:00\" yoki \"ertaga 09:00\".",
            reply_markup=single_back_keyboard(f"meetingopen:{mid}", text="✕ Bekor"),
        )
        return
    updated = await _apply_reschedule(mid, new_start)
    if not updated:
        await message.answer("Uchrashuv topilmadi.")
        return
    label = _meeting_time_label(updated.get("datetime_start") or "", with_past_marker=False)
    await _safe_answer(
        message,
        f"✅ Vaqt **{label}** ga ko'chirildi.\n\n" + _format_meeting_card(updated, show_date=True),
        parse_mode="Markdown",
        reply_markup=meeting_inline_actions(updated),
    )


async def _resync_meeting_to_icloud(meeting_id: str) -> None:
    """Delete and re-push a meeting after reschedule."""
    try:
        await asyncio.to_thread(calendar_service.delete_meeting, meeting_id)
        meeting = await database.get_meeting(meeting_id)
        if not meeting:
            return
        start_dt = datetime.fromisoformat(meeting["datetime_start"])
        end_iso = meeting.get("datetime_end") or (start_dt + timedelta(hours=1)).isoformat()
        end_dt = datetime.fromisoformat(end_iso)
        uid = await asyncio.to_thread(
            calendar_service.push_meeting,
            meeting_id, meeting["title"], start_dt, end_dt,
            meeting.get("participants"), meeting.get("location_or_link"), meeting.get("agenda"),
        )
        if uid:
            import aiosqlite
            async with aiosqlite.connect(config.DATABASE_PATH) as db:
                await db.execute("UPDATE meetings SET icloud_uid=? WHERE id=?", (uid, meeting_id))
                await db.commit()
    except Exception:
        logger.exception("Meeting re-sync to iCloud failed")


_EDIT_FIELD_LABELS = {
    "title":        ("📝 Sarlavha",      "Sarlavhani yuboring."),
    "participants": ("👥 Ishtirokchilar", "Ishtirokchilarni vergul bilan ajratib yuboring. Misol: Dinislam, Sanjar"),
    "location":     ("📍 Manzil",        "Manzil yoki havolani yuboring."),
    "agenda":       ("📅 Kun tartibi",   "Kun tartibini yuboring."),
}


def _meeting_edit_menu_keyboard(mid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"medit:{mid}:{field}")
         for field, (label, _) in [(k, v) for k, v in _EDIT_FIELD_LABELS.items() if k == "title"]],
        [InlineKeyboardButton(text=_EDIT_FIELD_LABELS["participants"][0],
                              callback_data=f"medit:{mid}:participants")],
        [InlineKeyboardButton(text=_EDIT_FIELD_LABELS["location"][0],
                              callback_data=f"medit:{mid}:location")],
        [InlineKeyboardButton(text=_EDIT_FIELD_LABELS["agenda"][0],
                              callback_data=f"medit:{mid}:agenda")],
        [InlineKeyboardButton(text="⏱ Davomiylik",
                              callback_data=f"medit:{mid}:duration")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"meetingopen:{mid}")],
    ])


def _duration_picker_keyboard(mid: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="30 daqiqa", callback_data=f"mdur:{mid}:30"),
            InlineKeyboardButton(text="1 soat", callback_data=f"mdur:{mid}:60"),
        ],
        [
            InlineKeyboardButton(text="1.5 soat", callback_data=f"mdur:{mid}:90"),
            InlineKeyboardButton(text="2 soat", callback_data=f"mdur:{mid}:120"),
        ],
        [
            InlineKeyboardButton(text="3 soat", callback_data=f"mdur:{mid}:180"),
            InlineKeyboardButton(text="4 soat", callback_data=f"mdur:{mid}:240"),
        ],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"meeting_edit:{mid}")],
    ])


@router.callback_query(F.data.startswith("meeting_edit:"))
async def cb_meeting_edit(query: CallbackQuery) -> None:
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer()
    title = (meeting.get("title") or "—").strip()
    text = (
        "✏️ **TAHRIRLASH**\n\n"
        f"Uchrashuv: {title}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Qaysi maydonni o'zgartirmoqchisiz?"
    )
    try:
        await query.message.edit_text(text, parse_mode="Markdown",
                                       reply_markup=_meeting_edit_menu_keyboard(mid))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=_meeting_edit_menu_keyboard(mid))


@router.callback_query(F.data.startswith("medit:"))
async def cb_meeting_edit_field(query: CallbackQuery, state: FSMContext) -> None:
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Xato format", show_alert=True)
        return
    mid, field = parts[1], parts[2]
    if field == "duration":
        await query.answer()
        text = (
            "⏱ **DAVOMIYLIK**\n\n"
            "Uchrashuv qancha davom etadi?"
        )
        try:
            await query.message.edit_text(text, parse_mode="Markdown",
                                           reply_markup=_duration_picker_keyboard(mid))
        except TelegramBadRequest:
            await _safe_answer(query.message, text, parse_mode="Markdown",
                                reply_markup=_duration_picker_keyboard(mid))
        return
    if field not in _EDIT_FIELD_LABELS:
        await query.answer("Maydon noma'lum", show_alert=True)
        return
    label, prompt = _EDIT_FIELD_LABELS[field]
    await state.set_state(MeetingEditFSM.awaiting_value)
    await state.update_data(meeting_id=mid, field=field)
    await query.answer()
    text = f"{label}\n\n{prompt}"
    try:
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=single_back_keyboard(f"meeting_edit:{mid}", text="✕ Bekor"),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=single_back_keyboard(f"meeting_edit:{mid}", text="✕ Bekor"))


@router.message(StateFilter(MeetingEditFSM.awaiting_value), F.text | F.voice)
async def handle_meeting_edit_value(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    mid = data.get("meeting_id")
    field = data.get("field")
    await state.clear()
    if not mid or field not in _EDIT_FIELD_LABELS:
        await message.answer("Holat yo'qoldi — qayta urinib ko'ring.")
        return
    if message.voice:
        await message.bot.send_chat_action(message.chat.id, "typing")
        file = await bot.get_file(message.voice.file_id)
        audio_io = await bot.download_file(file.file_path)
        audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
        new_value = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not new_value:
            await message.answer("Ovozni o'qiy olmadim. Matn yuboring.")
            return
    else:
        new_value = (message.text or "").strip()
    if not new_value:
        await message.answer("Bo'sh qiymat — bekor qilindi.")
        return

    update_data: dict = {}
    if field == "participants":
        parts = [p.strip() for p in new_value.replace(";", ",").split(",") if p.strip()]
        update_data["participants"] = parts
    elif field == "title":
        update_data["title"] = new_value
    elif field == "location":
        update_data["location_or_link"] = new_value
    elif field == "agenda":
        update_data["agenda"] = new_value

    await database.update_meeting(mid, update_data)
    if config.ICLOUD_ENABLED:
        _spawn_background(_resync_meeting_to_icloud(mid), name=f"icloud_resync:{mid}")

    updated = await database.get_meeting(mid)
    if not updated:
        await message.answer("Uchrashuv topilmadi.")
        return
    await _safe_answer(
        message,
        "✅ Yangilandi.\n\n" + _format_meeting_card(updated, show_date=True),
        parse_mode="Markdown",
        reply_markup=meeting_inline_actions(updated),
    )


@router.callback_query(F.data.startswith("mdur:"))
async def cb_meeting_duration(query: CallbackQuery) -> None:
    """Set duration: keeps start as-is, computes new end = start + N minutes."""
    parts = query.data.split(":")
    if len(parts) < 3:
        await query.answer("Xato format", show_alert=True)
        return
    mid, minutes_str = parts[1], parts[2]
    try:
        minutes = int(minutes_str)
    except ValueError:
        await query.answer("Davomiylik xato", show_alert=True)
        return
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    try:
        start = datetime.fromisoformat(meeting["datetime_start"])
    except (ValueError, TypeError):
        await query.answer("Vaqt formatida xato", show_alert=True)
        return
    new_end = start + timedelta(minutes=minutes)
    await database.update_meeting(mid, {"datetime_end": new_end.isoformat()})
    if config.ICLOUD_ENABLED:
        _spawn_background(_resync_meeting_to_icloud(mid), name=f"icloud_resync:{mid}")
    updated = await database.get_meeting(mid)
    await query.answer(f"✅ Davomiylik {minutes} daq ga o'zgartirildi")
    if updated:
        try:
            await query.message.edit_text(_format_meeting_card(updated, show_date=True),
                                           parse_mode="Markdown",
                                           reply_markup=meeting_inline_actions(updated))
        except TelegramBadRequest:
            await _safe_answer(query.message, _format_meeting_card(updated, show_date=True),
                                parse_mode="Markdown", reply_markup=meeting_inline_actions(updated))


def _build_protocol_directive(meeting: dict, user_notes: str) -> str:
    """Build the [INTERNAL] generate_meeting_protocol directive for Claude.
    Asks Claude to return a JSON envelope where:
      - user_message: full formatted protocol text (with markdown sections)
      - actions: list of create_task actions for the topshiriqlar
    """
    try:
        dt = datetime.fromisoformat(meeting["datetime_start"]).astimezone(database.TZ)
        date_str = f"{dt.year}-yil {dt.day}-{UZ_MONTHS_FULL[dt.month - 1]}"
        time_str = dt.strftime("%H:%M")
    except (ValueError, TypeError):
        date_str = meeting.get("datetime_start") or "—"
        time_str = ""
    end_time_str = ""
    if meeting.get("datetime_end"):
        try:
            end_dt = datetime.fromisoformat(meeting["datetime_end"]).astimezone(database.TZ)
            end_time_str = end_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass
    participants = ", ".join(meeting.get("participants") or []) or "belgilanmagan"
    location = (meeting.get("location_or_link") or "").strip() or "belgilanmagan"
    agenda = (meeting.get("agenda") or "").strip() or "belgilanmagan"

    return (
        "[INTERNAL] generate_meeting_protocol\n\n"
        "Sen rasmiy uzbek tilidagi uchrashuv bayonnomasini tuzasan.\n\n"
        "UCHRASHUV MA'LUMOTLARI:\n"
        f"  Mavzu: {meeting.get('title') or '—'}\n"
        f"  Sana: {date_str}\n"
        f"  Vaqt: soat {time_str}" + (f" dan {end_time_str} gacha" if end_time_str else "") + "\n"
        f"  Joy: {location}\n"
        f"  Ishtirokchilar: {participants}\n"
        f"  Kun tartibi: {agenda}\n\n"
        f"FOYDALANUVCHI YOZUVI:\n{user_notes}\n\n"
        "VAZIFA:\n"
        "JSON envelope qaytar. `user_message` ichida rasmiy uzbek tilida tuzilgan "
        "to'liq bayonnomani markdown formatda joylab ber. Quyidagi tartibni saqla:\n\n"
        "📝 **UCHRASHUV BAYONNOMASI**\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📅 **Sana va vaqt**\n{sana, vaqt diapazoni}\n\n"
        "📍 **O'tkazilgan joy**\n{joy}\n\n"
        "👥 **Ishtirokchilar**\n1. {Familiya Ism otasining ismi}, {lavozim}\n2. ...\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📋 **KUN TARTIBI**\n1. {mavzu}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "💬 **MUHOKAMA**\n{Har mavzu bo'yicha 2-4 gap rasmiy uslubda.}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ **QABUL QILINGAN QARORLAR**\n1. {Qaror buyruq fe'li bilan: '...sin'.}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📌 **TOPSHIRIQLAR**\n1. {Topshiriq matni}\n   Mas'ul: {ism}\n   Muddat: {sana}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "_Bayonnoma Maqsud Rustamov tomonidan tasdiqlandi._\n\n"
        "USLUB QOIDALARI:\n"
        "- Faol nisbat ('tayyorlang' emas 'tayyorlanishi kerak')\n"
        "- Ismlar to'liq, lavozimlar bilan\n"
        "- Hissiy so'zlar yo'q ('juda muhim' — yo'q, aniq sana — ha)\n"
        "- Qarorlar buyruq fe'li bilan tugasin ('...sin')\n"
        "- Foydalanuvchi yozuvida ma'lumot yo'q bo'lsa, mavjud metadata'dan foydalan, "
        "to'qib chiqarma. Yo'q narsani '[aniqlashtirish kerak]' deb belgila.\n\n"
        "TOPSHIRIQLAR uchun `actions` maydoniga create_task elementlarini joyla. "
        "Har bir create_task data: {title, assignee, deadline (ISO 8601 yoki null), priority='P1'}. "
        "Foydalanuvchi yozuvida aniq topshiriq yo'q bo'lsa, `actions: []` qaytar."
    )


@router.callback_query(F.data.startswith("protocol:"))
async def cb_meeting_protocol_start(query: CallbackQuery, state: FSMContext) -> None:
    """Open the protocol-creation prompt for a meeting."""
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await state.set_state(MeetingProtocolFSM.awaiting_notes)
    await state.update_data(meeting_id=mid)
    await query.answer()
    title = (meeting.get("title") or "—").strip()
    when = _meeting_time_label(meeting.get("datetime_start") or "", with_past_marker=False)
    text = (
        "📝 **BAYONNOMA**\n\n"
        f"{title}\n"
        f"{when}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Uchrashuvda nimalar muhokama qilindi?\n\n"
        "Qisqacha yozing yoki ovozli xabar yuboring.\n"
        "Bot sizning matningiz asosida rasmiy bayonnoma tayyorlaydi.\n\n"
        "_Eslatib o'ting:_\n"
        "_• Muhokama mavzulari_\n"
        "_• Qabul qilingan qarorlar_\n"
        "_• Topshiriqlar (kim, nima, qachon)_"
    )
    try:
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=single_back_keyboard(f"meetingopen:{mid}", text="✕ Bekor"),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=single_back_keyboard(f"meetingopen:{mid}", text="✕ Bekor"))


@router.message(StateFilter(MeetingProtocolFSM.awaiting_notes), F.text | F.voice)
async def handle_protocol_notes(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    mid = data.get("meeting_id")
    if not mid:
        await state.clear()
        await message.answer("Holat yo'qoldi — qayta urinib ko'ring.")
        return
    if message.voice:
        await message.bot.send_chat_action(message.chat.id, "typing")
        file = await bot.get_file(message.voice.file_id)
        audio_io = await bot.download_file(file.file_path)
        audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
        user_notes = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not user_notes:
            await message.answer("Ovozni o'qiy olmadim. Matn yuboring.")
            return
    else:
        user_notes = (message.text or "").strip()
    if not user_notes:
        await message.answer("Bo'sh xabar — bekor qilindi.")
        await state.clear()
        return

    meeting = await database.get_meeting(mid)
    if not meeting:
        await state.clear()
        await message.answer("Uchrashuv topilmadi.")
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        directive = _build_protocol_directive(meeting, user_notes)
        response = await claude_service.process_message("", internal_directive=directive)
    finally:
        typing_task.cancel()

    protocol_text = (response.get("user_message") or "").strip()
    pending_actions = response.get("actions") or []
    if not protocol_text:
        await message.answer("Bayonnomani tuzib bo'lmadi — qayta urinib ko'ring.")
        await state.clear()
        return

    # Cache the result in FSM data so confirm/edit can find it.
    await state.update_data(
        protocol_text=protocol_text,
        pending_actions=pending_actions,
        user_notes=user_notes,
    )

    pending_count = sum(1 for a in pending_actions if a.get("type") == "create_task")
    confirm_label = "✅ Tasdiqlash"
    if pending_count:
        confirm_label = f"✅ Tasdiqlash ({pending_count} ta vazifa)"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=confirm_label, callback_data=f"proto_ok:{mid}"),
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"proto_edit:{mid}"),
        ],
        [InlineKeyboardButton(text="📤 Ulashish", callback_data=f"proto_share:{mid}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"meetingopen:{mid}")],
    ])
    await _safe_answer(message, protocol_text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("proto_ok:"))
async def cb_protocol_confirm(query: CallbackQuery, state: FSMContext) -> None:
    """Save protocol text + auto-create tasks from pending_actions."""
    mid = query.data.split(":", 1)[1]
    data = await state.get_data()
    protocol_text = data.get("protocol_text", "")
    pending_actions = data.get("pending_actions") or []
    await state.clear()
    if not protocol_text:
        await query.answer("Bayonnoma topilmadi", show_alert=True)
        return
    # Persist protocol text in follow_up_actions (existing column, stores arbitrary text/JSON).
    await database.update_meeting(mid, {
        "follow_up_actions": [protocol_text],
        "followup_sent_at": datetime.now(database.TZ).isoformat(),
    })
    created = await _execute_actions(pending_actions)
    n_tasks = len(created.get("task", []))
    await query.answer(f"✅ Saqlandi · {n_tasks} ta vazifa yaratildi" if n_tasks
                       else "✅ Bayonnoma saqlandi")
    meeting = await database.get_meeting(mid)
    if meeting:
        try:
            await query.message.edit_text(
                _format_meeting_card(meeting, show_date=True),
                parse_mode="Markdown",
                reply_markup=meeting_inline_actions(meeting),
            )
        except TelegramBadRequest:
            await _safe_answer(query.message, _format_meeting_card(meeting, show_date=True),
                                parse_mode="Markdown",
                                reply_markup=meeting_inline_actions(meeting))


@router.callback_query(F.data.startswith("proto_edit:"))
async def cb_protocol_edit(query: CallbackQuery, state: FSMContext) -> None:
    """Ask the user for additional corrections; re-run Claude with combined notes."""
    mid = query.data.split(":", 1)[1]
    data = await state.get_data()
    prev_notes = data.get("user_notes", "")
    await state.set_state(MeetingProtocolFSM.awaiting_revision)
    await state.update_data(meeting_id=mid, prev_notes=prev_notes)
    await query.answer()
    text = (
        "✏️ **BAYONNOMANI TUZATISH**\n\n"
        "Qaysi joyini o'zgartirish kerak? Qo'shimcha ma'lumot yoki tuzatishni\n"
        "yozib yuboring (matn yoki ovoz).\n\n"
        "_Misol: \"Dinislamning lavozimi — hamkorlik direktori\"_"
    )
    try:
        await query.message.edit_text(
            text, parse_mode="Markdown",
            reply_markup=single_back_keyboard(f"meetingopen:{mid}", text="✕ Bekor"),
        )
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown",
                            reply_markup=single_back_keyboard(f"meetingopen:{mid}", text="✕ Bekor"))


@router.message(StateFilter(MeetingProtocolFSM.awaiting_revision), F.text | F.voice)
async def handle_protocol_revision(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    mid = data.get("meeting_id")
    prev_notes = data.get("prev_notes", "")
    if not mid:
        await state.clear()
        await message.answer("Holat yo'qoldi — qayta urinib ko'ring.")
        return
    if message.voice:
        file = await bot.get_file(message.voice.file_id)
        audio_io = await bot.download_file(file.file_path)
        audio_bytes = audio_io.getvalue() if hasattr(audio_io, "getvalue") else audio_io.read()
        correction = await voice_service.transcribe(audio_bytes, filename="voice.ogg", language="uz")
        if not correction:
            await message.answer("Ovozni o'qiy olmadim. Matn yuboring.")
            return
    else:
        correction = (message.text or "").strip()
    if not correction:
        await message.answer("Bo'sh xabar — bekor qilindi.")
        await state.clear()
        return

    meeting = await database.get_meeting(mid)
    if not meeting:
        await state.clear()
        await message.answer("Uchrashuv topilmadi.")
        return

    combined = f"{prev_notes}\n\nTUZATISH: {correction}"
    await message.bot.send_chat_action(message.chat.id, "typing")
    typing_task = asyncio.create_task(_keep_typing(message.bot, message.chat.id))
    try:
        directive = _build_protocol_directive(meeting, combined)
        response = await claude_service.process_message("", internal_directive=directive)
    finally:
        typing_task.cancel()

    protocol_text = (response.get("user_message") or "").strip()
    pending_actions = response.get("actions") or []
    if not protocol_text:
        await message.answer("Tuzata olmadim — qayta urinib ko'ring.")
        return

    await state.set_state(MeetingProtocolFSM.awaiting_notes)
    await state.update_data(
        protocol_text=protocol_text,
        pending_actions=pending_actions,
        user_notes=combined,
    )

    pending_count = sum(1 for a in pending_actions if a.get("type") == "create_task")
    confirm_label = f"✅ Tasdiqlash ({pending_count} ta vazifa)" if pending_count else "✅ Tasdiqlash"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=confirm_label, callback_data=f"proto_ok:{mid}"),
            InlineKeyboardButton(text="✏️ Tahrirlash", callback_data=f"proto_edit:{mid}"),
        ],
        [InlineKeyboardButton(text="📤 Ulashish", callback_data=f"proto_share:{mid}")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"meetingopen:{mid}")],
    ])
    await _safe_answer(message, protocol_text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("proto_share:"))
async def cb_protocol_share(query: CallbackQuery, state: FSMContext) -> None:
    """Hand the protocol text off via the bot's existing share menu (forward-to-chat)."""
    data = await state.get_data()
    protocol_text = data.get("protocol_text", "")
    if not protocol_text:
        await query.answer("Bayonnoma topilmadi", show_alert=True)
        return
    await query.answer("Matnni yuqorida nusxalab, kerakli chat'ga jo'nating.")
    # Re-send the protocol as a standalone copyable message so the user can long-press → forward.
    await _safe_answer(query.message, protocol_text, parse_mode="Markdown")


@router.callback_query(F.data.startswith("meeting_cancel:"))
async def cb_meeting_cancel_confirm(query: CallbackQuery) -> None:
    """Two-step cancel: first shows the confirmation prompt."""
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await query.answer()
    title = (meeting.get("title") or "—").strip()
    when = _meeting_time_label(meeting.get("datetime_start") or "", with_past_marker=False)
    text = (
        "✕ **UCHRASHUVNI BEKOR QILISH**\n\n"
        f"{title}\n"
        f"{when}\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Haqiqatdan ham bekor qilmoqchimisiz?\n\n"
        "_iCloud kalendaridan ham o'chiriladi._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Ha, bekor", callback_data=f"mcanc_do:{mid}"),
            InlineKeyboardButton(text="⬅️ Yo'q", callback_data=f"meetingopen:{mid}"),
        ],
    ])
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=kb)


@router.callback_query(F.data.startswith("mcanc_do:"))
async def cb_meeting_cancel_do(query: CallbackQuery) -> None:
    """Execute the cancel + iCloud delete."""
    mid = query.data.split(":", 1)[1]
    meeting = await database.get_meeting(mid)
    if not meeting:
        await query.answer("Uchrashuv topilmadi", show_alert=True)
        return
    await database.cancel_meeting(mid)
    # Scheduler'dan 15-daqiqali eslatma jobini olib tashlash — bekor qilingan
    # uchrashuv uchun eslatma chiqmasin.
    sched = scheduler_module.get_scheduler()
    if sched:
        try:
            sched.remove_meeting_reminder(mid)
        except Exception:
            logger.exception("Failed to remove meeting reminder for %s", mid)
    if config.ICLOUD_ENABLED:
        try:
            await asyncio.to_thread(calendar_service.delete_meeting, mid)
        except Exception:
            logger.exception("iCloud meeting delete failed (will retry)")
    await query.answer("✅ Uchrashuv bekor qilindi")
    try:
        await query.message.edit_text(
            "✕ Uchrashuv bekor qilindi.\n\nKalendar bilan sinxronlanmoqda...",
            parse_mode="Markdown",
            reply_markup=single_back_keyboard("meetingfilter:week"),
        )
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("reopen:"))
async def cb_reopen(query: CallbackQuery) -> None:
    parts = query.data.split(":", 1)
    target_id = parts[1] if len(parts) > 1 else ""
    if target_id:
        await database.update_task(target_id, {"status": "todo"})
        await query.answer("Qaytarildi ↺")
        task = await database.get_task(target_id)
        if task:
            try:
                await query.message.edit_text(_format_task_card(task), parse_mode="Markdown",
                                              reply_markup=task_inline_actions(task))
            except Exception:
                pass
    else:
        await query.answer()



@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(query: CallbackQuery) -> None:
    """Open the field-editing menu for a task."""
    parts = query.data.split(":", 1)
    target_id = parts[1] if len(parts) > 1 else ""
    if not target_id:
        await query.answer()
        return
    task = await database.get_task(target_id)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer()
    text = (
        f"✎ **Qaysi maydonni tahrirlash kerak?**\n\n"
        f"{_format_task_card(task)}"
    )
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=task_edit_menu(task))
    except TelegramBadRequest:
        await _safe_answer(query.message, text, parse_mode="Markdown", reply_markup=task_edit_menu(task))


@router.callback_query(F.data.startswith("editfield:"))
async def cb_edit_field(query: CallbackQuery, state: FSMContext) -> None:
    """User picked a field to edit. Show picker (for enums) or ask for free-text input."""
    _, tid, field = query.data.split(":", 2)
    task = await database.get_task(tid)
    if not task:
        await query.answer("Vazifa topilmadi", show_alert=True)
        return
    await query.answer()

    if field == "priority":
        await query.message.edit_text(
            f"⚡ **Yangi prioritet?**\n\nHozir: `{task.get('priority', 'P2')}`",
            parse_mode="Markdown", reply_markup=priority_picker(tid),
        )
        return
    if field == "status":
        await query.message.edit_text(
            f"📊 **Yangi status?**\n\nHozir: `{_STATUS_LABEL_UZ.get(task.get('status', 'todo'), task.get('status'))}`",
            parse_mode="Markdown", reply_markup=status_picker(tid),
        )
        return
    if field == "deadline":
        current = task.get("deadline")
        current_label = _format_deadline_short(current)[0] if current else "yo'q"
        await query.message.edit_text(
            f"📅 **Yangi deadline?**\n\nHozir: {current_label}",
            parse_mode="Markdown", reply_markup=deadline_picker(tid),
        )
        return

    # Free-text fields → set FSM state, ask for input
    prompts = {
        "title": "📝 Yangi **sarlavha** yuboring (matn yoki ovoz):",
        "description": "📄 Yangi **tavsif** yuboring (yoki `-` deb yozsangiz tozalanadi):",
        "tags": "🏷 **Teglar** vergul bilan ajratib yuboring (masalan: `marketing, urgent`):",
    }
    prompt = prompts.get(field)
    if not prompt:
        await query.answer("Noto'g'ri maydon", show_alert=True)
        return

    await state.set_state(TaskEditFSM.awaiting_value)
    await state.update_data(task_id=tid, field=field)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Bekor qilish", callback_data=f"edit_cancel:{tid}")
    ]])
    await query.message.edit_text(prompt, parse_mode="Markdown", reply_markup=cancel_kb)


@router.callback_query(F.data.startswith("setfield:"))
async def cb_set_field(query: CallbackQuery) -> None:
    """Apply an enum/preset value picked from picker."""
    _, tid, field, value = query.data.split(":", 3)
    if value == "none":
        update_val = None
    else:
        update_val = value
    if field == "status" and update_val == "done":
        await database.complete_task(tid)
    else:
        await database.update_task(tid, {field: update_val}, source="edit")
    # Toast — Uzbek labellarda. Foydalanuvchi raw kodlarni ko'rmasin.
    field_uz = {"priority": "Ustuvorlik", "status": "Holat",
                "deadline": "Muddat", "title": "Sarlavha",
                "description": "Tavsif", "tags": "Teglar"}.get(field, field)
    if update_val is None:
        value_uz = "tozalandi"
    elif field == "priority":
        value_uz = _PRIORITY_LABEL_UZ.get(update_val, update_val)
    elif field == "status":
        value_uz = _STATUS_LABEL_UZ.get(update_val, update_val)
    else:
        value_uz = update_val
    await query.answer(f"{field_uz} → {value_uz} ✅")
    task = await database.get_task(tid)
    if task:
        text = _format_task_card(task)
        try:
            await query.message.edit_text(
                text, parse_mode="Markdown",
                reply_markup=_task_card_kb_with_back(task),
            )
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("deadline_preset:"))
async def cb_deadline_preset(query: CallbackQuery) -> None:
    _, tid, preset = query.data.split(":", 2)
    now = datetime.now(database.TZ)
    new_dt = None
    if preset == "today":
        new_dt = now.replace(hour=17, minute=0, second=0, microsecond=0)
        if new_dt < now:
            new_dt = now + timedelta(hours=2)
    elif preset == "tomorrow":
        new_dt = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    elif preset == "plus3":
        new_dt = (now + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    elif preset == "weekend":
        days_to_sunday = (6 - now.weekday()) % 7 or 7
        new_dt = (now + timedelta(days=days_to_sunday)).replace(hour=18, minute=0, second=0, microsecond=0)

    if new_dt:
        await database.update_task(tid, {"deadline": new_dt.isoformat()}, source="edit")
        await query.answer(f"Deadline: {new_dt.strftime('%d-%m %H:%M')} ✅")
        task = await database.get_task(tid)
        if task:
            try:
                await query.message.edit_text(
                    _format_task_card(task), parse_mode="Markdown",
                    reply_markup=_task_card_kb_with_back(task),
                )
            except TelegramBadRequest:
                pass


@router.callback_query(F.data.startswith("deadline_manual:"))
async def cb_deadline_manual(query: CallbackQuery, state: FSMContext) -> None:
    tid = query.data.split(":", 1)[1]
    await state.set_state(TaskEditFSM.awaiting_value)
    await state.update_data(task_id=tid, field="deadline")
    await query.answer()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Bekor qilish", callback_data=f"edit_cancel:{tid}")
    ]])
    await query.message.edit_text(
        "📅 Deadline'ni quyidagi formatlardan birida yuboring:\n\n"
        "• `2026-05-25 14:30`\n"
        "• `25-05 14:30` (yil — bu yil)\n"
        "• `ertaga 09:00`\n"
        "• `juma 10:00`",
        parse_mode="Markdown",
        reply_markup=cancel_kb,
    )


@router.callback_query(F.data.startswith("edit_cancel:"))
async def cb_edit_cancel(query: CallbackQuery, state: FSMContext) -> None:
    tid = query.data.split(":", 1)[1]
    await state.clear()
    task = await database.get_task(tid)
    await query.answer("Bekor qilindi")
    if task:
        try:
            await query.message.edit_text(
                _format_task_card(task), parse_mode="Markdown",
                reply_markup=_task_card_kb_with_back(task),
            )
        except TelegramBadRequest:
            pass


@router.message(StateFilter(TaskEditFSM.awaiting_value), F.text | F.voice)
async def handle_edit_value(message: Message, state: FSMContext) -> None:
    _msg_text = await _get_text_or_transcribe(message, bot=message.bot)
    if _msg_text is None:
        return

    """User typed a new value while editing. Apply and exit FSM."""
    data = await state.get_data()
    tid = data.get("task_id")
    field = data.get("field")
    if not tid or not field:
        await state.clear()
        return
    raw = message.text.strip()

    if field == "title":
        await database.update_task(tid, {"title": raw[:200]}, source="edit")
    elif field == "description":
        await database.update_task(tid, {"description": None if raw == "-" else raw}, source="edit")
    elif field == "tags":
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        await database.update_task(tid, {"tags": tags}, source="edit")
    elif field == "deadline":
        # Try to parse via Claude (consistent with how user-input dates are parsed)
        parsed, reason = await _parse_deadline_natural(raw)
        if parsed:
            await database.update_task(tid, {"deadline": parsed}, source="edit")
        else:
            await _safe_answer(message, _deadline_error_message(reason, kind="deadline"),
                               parse_mode="Markdown")
            return  # keep FSM open for retry
    await state.clear()

    task = await database.get_task(tid)
    if task:
        await _safe_answer(message, "✅ Saqlandi\n\n" + _format_task_card(task),
                           parse_mode="Markdown", reply_markup=_task_card_kb_with_back(task))


def _deadline_error_message(reason: str | None, *, kind: str = "deadline") -> str:
    """Map a _parse_deadline_natural() reason code to a specific, actionable
    message so different mistakes get different guidance — instead of one
    generic "tushunmadim" line for every kind of bad input.

    kind: 'deadline' (task due-date) or 'time' (reminder/edit time) — only
    changes the noun shown to the user.
    """
    noun = "Muddatni" if kind == "deadline" else "Vaqtni"
    if reason == "too_far":
        # The bot DID understand the input — it just exceeds the 7-day relative
        # cap. Saying "tushunmadim" here would be misleading, so be explicit.
        return ("⏳ Juda uzoq muddat — nisbiy vaqt eng ko'pi 7 kun bo'lishi mumkin.\n"
                "Aniq sana yuboring, masalan: `2026-06-10 15:00`.")
    if reason == "invalid":
        return ("📅 Bunday sana/vaqt mavjud emas (kun yoki oy noto'g'ri).\n"
                "Tekshirib qaytadan yuboring: `2026-06-10 15:00`.")
    # "unparsable" (or any unexpected reason) → generic-but-helpful with examples.
    return (f"❌ {noun} tushunmadim. Masalan: `ertaga 09:00`, `2 soat`, "
            f"yoki `2026-06-10 15:00`.")


async def _parse_deadline_natural(text: str) -> tuple[str | None, str | None]:
    """Lightweight natural-language → ISO 8601 in Asia/Tashkent.
    Handles common formats without invoking Claude.

    Returns (iso, reason):
      - (iso,  None)         — parsed successfully
      - (None, "too_far")    — relative offset exceeds the 7-day cap
      - (None, "invalid")    — matched a date/time shape but the value is impossible
      - (None, "unparsable") — nothing matched
    Pass the reason to _deadline_error_message() so each mistake gets its own
    fix-it message rather than one generic line.
    """
    import re
    text = text.strip().lower()
    now = datetime.now(database.TZ)

    # 0) Relative: "15 daqiqa", "2 soat", "2 soatdan keyin"
    # Cap at 7 days so absurd inputs like "999 daqiqa" don't quietly schedule
    # tasks 16 hours out (or worse, "99 soat" → 4 days). Anything bigger should
    # use an explicit date.
    MAX_RELATIVE_MINUTES = 7 * 24 * 60
    MAX_RELATIVE_HOURS = 7 * 24
    m = re.match(r"^(\d{1,4})\s*(daq|daqiqa|daqiqadan|min|minut)\b", text)
    if m:
        minutes = int(m.group(1))
        if 0 < minutes <= MAX_RELATIVE_MINUTES:
            return (now + timedelta(minutes=minutes)).replace(second=0, microsecond=0).isoformat(), None
        return None, "too_far"
    m = re.match(r"^(\d{1,3})\s*(soat|soatdan|hour|h)\b", text)
    if m:
        hours = int(m.group(1))
        if 0 < hours <= MAX_RELATIVE_HOURS:
            return (now + timedelta(hours=hours)).replace(second=0, microsecond=0).isoformat(), None
        return None, "too_far"

    # 1) ISO-ish: 2026-05-25 14:30 or 2026-05-25T14:30
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[\s tT]+(\d{1,2}):(\d{2})$", text)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        try:
            return database.TZ.localize(datetime(y, mo, d, hh, mm)).isoformat(), None
        except ValueError:
            return None, "invalid"

    # 2) Short: 25-05 14:30 (current year)
    m = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})$", text)
    if m:
        d, mo, hh, mm = map(int, m.groups())
        try:
            return database.TZ.localize(datetime(now.year, mo, d, hh, mm)).isoformat(), None
        except ValueError:
            return None, "invalid"

    # 3) Relative keywords
    weekday_map = {
        "dushanba": 0, "seshanba": 1, "chorshanba": 2, "payshanba": 3,
        "juma": 4, "shanba": 5, "yakshanba": 6,
    }
    m = re.match(r"^(ertaga|indin|bugun|dushanba|seshanba|chorshanba|payshanba|juma|shanba|yakshanba)\s+(?:soat\s+)?(\d{1,2})(?::(\d{2}))?$", text)
    if m:
        word = m.group(1)
        hh = int(m.group(2))
        mm = int(m.group(3) or 0)
        if word == "bugun":
            target = now
        elif word == "ertaga":
            target = now + timedelta(days=1)
        elif word == "indin":
            target = now + timedelta(days=2)
        else:
            target_wd = weekday_map[word]
            delta = (target_wd - now.weekday()) % 7 or 7
            target = now + timedelta(days=delta)
        try:
            return target.replace(hour=hh, minute=mm, second=0, microsecond=0).isoformat(), None
        except ValueError:
            return None, "invalid"
    return None, "unparsable"


def _task_card_kb_with_back(task: dict) -> InlineKeyboardMarkup:
    """Opened task card — intentionally minimal: just ⋯ Batafsil and ⬅️ Ro'yxatga.
    Every per-task action (Ijrochi, Bajarildi, Muddat, Tahrir, O'chirish, …) lives
    inside ⋯ Batafsil (task_detail_menu), so we don't duplicate them on the card."""
    tid = task["id"]
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⋯ Batafsil", callback_data=f"task_detail:{tid}"),
            InlineKeyboardButton(text="⬅️ Ro'yxatga", callback_data="taskfilter:active"),
        ],
    ])


@router.callback_query()
async def cb_fallback(query: CallbackQuery) -> None:
    await query.answer()


# ─────────────────────── INLINE MODE ───────────────────────


@router.inline_query()
async def handle_inline_query(query: InlineQuery) -> None:
    """Inline mode: `@bot <q>` from any chat. Only the principal sees results.

    Results:
      - Direct text result (whatever the principal typed) — they can send as-is
      - If query matches a task title, offer that task's title as a quick send
      - If query matches a meeting, offer that meeting's summary
    """
    if not _is_principal(query.from_user.id):
        await query.answer(results=[], cache_time=1, is_personal=True)
        return

    q = (query.query or "").strip()
    results = []

    # Always offer raw text as result #0 so principal can use the bot as a quick scratchpad
    if q:
        results.append(InlineQueryResultArticle(
            id="raw",
            title=f"📝 «{q[:60]}»",
            description="Bu matnni shu chatga jo'natish",
            input_message_content=InputTextMessageContent(message_text=q),
        ))

    # If query is at least 2 chars, search tasks/meetings/contacts
    if len(q) >= 2:
        try:
            search_results = await database.search_all(q, limit=8)
            for t in search_results.get("tasks", [])[:5]:
                badge = _PRIORITY_BADGE.get(t.get("priority", "P2"), "🔵")
                deadline_label, _ = _format_deadline_short(t.get("deadline"))
                results.append(InlineQueryResultArticle(
                    id=f"task:{t['id']}",
                    title=f"{badge} {_truncate(t['title'], 60)}",
                    description=f"Vazifa · {deadline_label}",
                    input_message_content=InputTextMessageContent(
                        message_text=f"{badge} {t['title']}\n📅 {deadline_label}"
                    ),
                ))
            for m in search_results.get("meetings", [])[:3]:
                try:
                    dt = datetime.fromisoformat(m["datetime_start"]).astimezone(database.TZ)
                    time_str = dt.strftime("%d-%m %H:%M")
                except (ValueError, TypeError):
                    time_str = "—"
                participants = ", ".join(m.get("participants", [])[:2]) or "—"
                results.append(InlineQueryResultArticle(
                    id=f"meeting:{m['id']}",
                    title=f"🤝 {_truncate(m['title'], 60)}",
                    description=f"Uchrashuv · {time_str} · {participants}",
                    input_message_content=InputTextMessageContent(
                        message_text=f"🤝 {m['title']}\n🕐 {time_str}\n👥 {participants}"
                    ),
                ))
        except Exception:
            logger.exception("Inline search failed")

    if not results:
        results.append(InlineQueryResultArticle(
            id="empty",
            title="Hech narsa topilmadi",
            description="Boshqa kalit so'z bilan urinib ko'ring",
            input_message_content=InputTextMessageContent(message_text="(qidiruv natijasi yoʻq)"),
        ))

    await query.answer(results=results, cache_time=2, is_personal=True)


# ─────────────────────── FALLBACK HANDLERS (last-resort) ───────────────────────
# These MUST be registered last — aiogram dispatches in registration order,
# so state-specific handlers win when they match. Anything that falls through
# (unexpected content type, message in a state with no matching handler,
# edited messages) lands here instead of silently disappearing.


@router.edited_message()
async def handle_edited_message(message: Message) -> None:
    """Telegram delivers message edits as `edited_message` updates. Without
    this handler they vanish — user thinks the bot saw their edit when it
    didn't. Reply with explicit guidance so they re-send."""
    await message.answer(
        "✏️ Tahrir qilingan xabarlar qabul qilinmaydi. "
        "Yangi xabar (matn yoki ovoz) yuboring."
    )


@router.message(F.voice)
async def handle_voice_fallback(message: Message, bot: Bot, state: FSMContext) -> None:
    """Catches voice messages in states whose specific handler is text-only.
    Transcribes, then re-emits the transcript as if the user typed it by
    re-dispatching through the router. Without this, voice in any non-
    default, non-meeting state vanishes."""
    transcript = await _get_text_or_transcribe(message, bot)
    if transcript is None:
        return
    # message.text was just patched in-place by the helper above. Now do the
    # equivalent of the default_state voice handler: send the voice-confirm
    # prompt only if we're in default_state. In other states, the handler
    # that would have caught this text directly didn't run because aiogram
    # already chose this fallback — so we either route through Claude or
    # echo a recovery hint. Default state → confirm; else → Claude direct.
    current = await state.get_state()
    if current is None:
        await _send_voice_confirm_prompt(message, state, transcript)
    else:
        # Voice arrived inside an FSM state that didn't claim it. Best UX:
        # treat the transcript as a free-form message to Claude (same as
        # text would in a section state's fall-through branch).
        await _process_and_reply(message, transcript, state=state)


@router.message(F.photo | F.document | F.video | F.video_note | F.sticker | F.animation | F.audio)
async def handle_unsupported_attachment(message: Message) -> None:
    """Polite "not yet supported" instead of silent drop for media types the
    bot has no handler for. Helps the user know the bot is alive and what
    DOES work."""
    kind = (
        "rasm" if message.photo else
        "hujjat" if message.document else
        "video" if message.video else
        "video-xabar" if message.video_note else
        "stiker" if message.sticker else
        "animatsiya" if message.animation else
        "audio fayl" if message.audio else
        "fayl"
    )
    await message.answer(
        f"📎 Hozircha {kind} qabul qila olmayman. "
        "Matn yoki ovoz xabari yuboring."
    )


@router.message()
async def handle_unmatched_message(message: Message) -> None:
    """Last-resort catch-all for anything that didn't match a more specific
    handler (unexpected content type, lingering FSM state with no matching
    filter, etc). Without this the message vanishes and the user has no
    idea what went wrong."""
    await message.answer(
        "🤔 Bu xabarni tushuna olmadim.\n\n"
        "• Matn yoki ovoz xabari yuboring\n"
        "• `/cancel` — joriy amalni bekor qilish\n"
        "• `/help` — qo'llanma\n"
        "• `/start` — boshidan boshlash",
        parse_mode="Markdown",
    )
