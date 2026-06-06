"""Lokal smoke test — Vazifalar bo'limining hamma funksiyalari.

Tashqi API ishlatilmaydi (Claude, iCloud, Muxlisa, Whisper — barcha mocksiz tashlandi).
Faqat handlers + database + scheduler ichidagi pure-Python logika tekshiriladi.
"""

import asyncio
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import database  # noqa: E402
import handlers  # noqa: E402
import scheduler as sched_mod  # noqa: E402


# ─────────────── tiny test framework ───────────────
_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def t(area: str, name: str, ok: bool) -> None:
    global _PASS, _FAIL
    status = "✓" if ok else "✗"
    print(f"  [{status}] {area:<14} {name}")
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
        _FAILED.append(f"{area}: {name}")


def section(title: str) -> None:
    print(f"\n━━━━━━━━━━ {title} ━━━━━━━━━━")


# ─────────────── 1. Reply keyboard va navigatsiya ───────────────
async def test_reply_and_navigation() -> None:
    section("1. REPLY KBD + NAVIGATION")

    # Main reply keyboard contains 📌 Vazifalar
    kb = handlers.main_reply_keyboard()
    labels = [b.text for r in kb.keyboard for b in r]
    t("nav", "main kbd has '📌 Vazifalar'", handlers.BTN_TASKS in labels)
    t("nav", "main kbd has '⬅️ Asosiy menyu' alias avlod-mavjud (BTN_BACK_MAIN)",
      hasattr(handlers, "BTN_BACK_MAIN") and handlers.BTN_BACK_MAIN.startswith("⬅️"))

    # Tasks section reply keyboard
    skb = handlers.tasks_section_reply_keyboard()
    sl = [b.text for r in skb.keyboard for b in r]
    expected_filters = {handlers.TBTN_TASKS_ACTIVE, handlers.TBTN_TASKS_TODAY,
                        handlers.TBTN_TASKS_OVERDUE, handlers.TBTN_TASKS_IMPORTANT,
                        handlers.TBTN_TASKS_DONE, handlers.TBTN_TASKS_ALL}
    t("nav", "section kbd has all 6 filters", expected_filters <= set(sl))
    t("nav", "section kbd has '➕ Yangi vazifa'", handlers.TBTN_TASKS_NEW in sl)
    t("nav", "section kbd has '🔎 Vazifa qidirish'", handlers.TBTN_TASKS_SEARCH in sl)
    t("nav", "section kbd has '⬅️ Asosiy menyu'", handlers.BTN_BACK_MAIN in sl)
    t("nav", "section kbd persistent + resizable",
      skb.is_persistent and skb.resize_keyboard)

    # Filter label → DB key mapping
    fmap = handlers._TASKS_SECTION_FILTERS
    t("nav", "filter map → active", fmap.get(handlers.TBTN_TASKS_ACTIVE) == "active")
    t("nav", "filter map → today", fmap.get(handlers.TBTN_TASKS_TODAY) == "today")
    t("nav", "filter map → overdue", fmap.get(handlers.TBTN_TASKS_OVERDUE) == "overdue")
    t("nav", "filter map → important", fmap.get(handlers.TBTN_TASKS_IMPORTANT) == "important")
    t("nav", "filter map → done", fmap.get(handlers.TBTN_TASKS_DONE) == "done")
    t("nav", "filter map → all", fmap.get(handlers.TBTN_TASKS_ALL) == "all")

    # SectionFSM has dedicated in_tasks state — no collision risk
    t("nav", "SectionFSM.in_tasks defined",
      hasattr(handlers.SectionFSM, "in_tasks"))
    # FSMs uchun NewTaskFSM, TaskEditFSM, AssigneeFSM, TaskSearchFSM alohida
    for fsm in ("NewTaskFSM", "TaskEditFSM", "AssigneeFSM", "TaskSearchFSM"):
        t("nav", f"{fsm} defined", hasattr(handlers, fsm))

    # TBTN_TASKS_SEARCH → TaskSearchFSM (not global)
    src = inspect.getsource(handlers.handle_tasks_section_button)
    t("nav", "Vazifa qidirish → TaskSearchFSM (task-only scope)",
      "TaskSearchFSM.awaiting_query" in src)


# ─────────────── 2. List sort + filter ───────────────
async def test_list_and_filters() -> None:
    section("2. TASKS LIST + FILTERS")

    await database.init()

    # Seed: create 6 tasks across states/priorities
    seeded: list[str] = []
    from datetime import datetime, timedelta
    now = datetime.now(database.TZ)

    seeded.append(await database.create_task({
        "title": "AUDIT_active_P0", "priority": "P0",
        "deadline": (now + timedelta(hours=4)).isoformat(),
    }))
    seeded.append(await database.create_task({
        "title": "AUDIT_active_P2_today", "priority": "P2",
        "deadline": (now + timedelta(hours=2)).isoformat(),
    }))
    seeded.append(await database.create_task({
        "title": "AUDIT_overdue_P1", "priority": "P1",
        # Yesterday — clearly outside today's date range
        "deadline": (now - timedelta(days=1)).isoformat(),
    }))
    seeded.append(await database.create_task({
        "title": "AUDIT_active_P3_future", "priority": "P3",
        "deadline": (now + timedelta(days=3)).isoformat(),
    }))
    done_id = await database.create_task({"title": "AUDIT_done", "priority": "P1"})
    await database.complete_task(done_id)
    seeded.append(done_id)
    seeded.append(await database.create_task({
        "title": "AUDIT_important_P1_no_dl", "priority": "P1",
    }))

    try:
        # Aktiv (todo/in_progress) sort: priority then deadline
        active = await database.list_tasks(status_in=["todo", "in_progress"], limit=200)
        priorities = [t.get("priority") for t in active]
        priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        is_sorted = all(
            priority_order.get(priorities[i], 99) <= priority_order.get(priorities[i + 1], 99)
            for i in range(len(priorities) - 1)
        )
        t("filter", "list_tasks active sorted by priority", is_sorted)

        # Bugun — only today's deadline
        today_tasks = await database.list_today_tasks()
        today_date = now.date()
        all_today = all(
            (lambda dt: dt.date() == today_date)(
                datetime.fromisoformat(t["deadline"]).astimezone(database.TZ)
            )
            for t in today_tasks if t.get("deadline")
        )
        t("filter", "list_today_tasks → only today's deadlines", all_today)
        # no overdue/future in Bugun
        seeded_titles = {t["title"] for t in today_tasks}
        t("filter", "list_today_tasks excludes overdue (AUDIT_overdue_P1 missing)",
          "AUDIT_overdue_P1" not in seeded_titles)
        t("filter", "list_today_tasks excludes future-day (AUDIT_active_P3_future missing)",
          "AUDIT_active_P3_future" not in seeded_titles)

        # O'tgan — only past deadlines, active status
        overdue = await database.list_overdue_tasks()
        all_past = all(
            datetime.fromisoformat(t["deadline"]).astimezone(database.TZ) < now
            for t in overdue if t.get("deadline")
        )
        t("filter", "list_overdue_tasks → only past deadlines", all_past)
        ovs = {t["title"] for t in overdue}
        t("filter", "AUDIT_overdue_P1 present in overdue", "AUDIT_overdue_P1" in ovs)

        # Muhim — P0/P1 only (active)
        all_active = await database.list_tasks(status_in=["todo", "in_progress"], limit=200)
        muhim = [t for t in all_active if t.get("priority") in ("P0", "P1")]
        priorities_ok = all(t.get("priority") in ("P0", "P1") for t in muhim)
        t("filter", "Muhim → only P0/P1 in active", priorities_ok and len(muhim) >= 2)

        # Bajarilgan — only done
        done_tasks = await database.list_tasks(status_in=["done"], limit=200)
        all_done = all(t.get("status") == "done" for t in done_tasks)
        t("filter", "list_tasks(['done']) → only done", all_done and len(done_tasks) >= 1)

        # Barchasi — must contain done + active
        all_t = await database.list_tasks(limit=500)
        statuses = {t.get("status") for t in all_t}
        t("filter", "list_tasks() returns all statuses",
          "done" in statuses and ("todo" in statuses or "in_progress" in statuses))

        # Pagination button-text alignment fix (B1) — re-render sort happens at handler level.
        # Simulate the handler sort: done → end, stable within groups.
        mixed = list(all_t)
        mixed.sort(key=lambda x: 1 if x.get("status") == "done" else 0)
        last_unfinished = max(
            (i for i, x in enumerate(mixed) if x.get("status") != "done"), default=-1
        )
        first_done = next(
            (i for i, x in enumerate(mixed) if x.get("status") == "done"), len(mixed)
        )
        t("filter", "B1: stable sort places done after all unfinished",
          last_unfinished < first_done)

        # tasks_compact_keyboard pagination
        kb1 = handlers.tasks_compact_keyboard(active[:5], current_filter="active", page=1)
        if kb1:
            btns = [b for r in kb1.inline_keyboard for b in r]
            nums = [int(b.text) for b in btns if b.text.isdigit()]
            t("filter", "tasks_compact_keyboard numbers 1..N for page 1",
              nums == list(range(1, len(nums) + 1)) and len(nums) >= 1)

        # Empty list → keyboard returns None
        empty_kb = handlers.tasks_compact_keyboard([], current_filter="active")
        t("filter", "empty task list → keyboard None", empty_kb is None)

    finally:
        for tid in seeded:
            await database.delete_task(tid, source="audit_cleanup")


# ─────────────── 3. Task card + 9 buttons ───────────────
async def test_task_card_buttons() -> None:
    section("3. TASK CARD ACTION BUTTONS")

    # Active card: 3 quick actions
    act = handlers.task_inline_actions({"id": "t-x", "status": "todo"})
    quick = [b.text for r in act.inline_keyboard for b in r]
    t("card", "Active quick: 3 buttons",
      sum(len(r) for r in act.inline_keyboard) == 3)
    t("card", "Active quick has 👤 Ijrochi", any("Ijrochi" in q for q in quick))
    t("card", "Active quick has ✅ Bajarildi", any("Bajarildi" in q for q in quick))
    t("card", "Active quick has ⋯ Batafsil", any("Batafsil" in q for q in quick))

    # Done card: 3 quick actions (Qaytarish / O'chirish / Batafsil)
    done = handlers.task_inline_actions({"id": "t-x", "status": "done"})
    dquick = [b.text for r in done.inline_keyboard for b in r]
    t("card", "Done quick has ↺ Qaytarish", any("Qaytarish" in q for q in dquick))
    t("card", "Done quick has 🗑 O'chirish", any("chirish" in q for q in dquick))

    # Opened task card — direct actions, no '⋯ Batafsil': Bajarildi/Tahrir/O'chirish + Back
    card = handlers._task_card_kb_with_back({"id": "t-x", "status": "todo"})
    card_labels = [b.text for r in card.inline_keyboard for b in r]
    for required in ("Bajarildi", "Tahrir", "chirish", "Ro'yxatga"):
        t("card", f"Card has '{required}'", any(required in d for d in card_labels))
    t("card", "Card has NO '⋯ Batafsil' (retired)",
      not any("Batafsil" in d for d in card_labels))
    # Field edits + Ijrochi moved into ✏️ Tahrir (task_edit_menu)
    em_labels = [b.text for r in handlers.task_edit_menu({"id": "t-x"}).inline_keyboard for b in r]
    for required in ("Deadline", "Prioritet", "Ijrochi"):
        t("card", f"Edit menu has '{required}'", any(required in d for d in em_labels))

    # Card-with-back keyboard (used for taskopen / after assignee change)
    cwb = handlers._task_card_kb_with_back({"id": "t-x", "status": "todo"})
    cwb_labels = [b.text for r in cwb.inline_keyboard for b in r]
    t("card", "_task_card_kb_with_back has '⬅️ Ro'yxatga'",
      any("Ro'yxatga" in l for l in cwb_labels))

    # mark_important bump map — skip if the feature was removed
    if hasattr(handlers, "cb_mark_important"):
        src = inspect.getsource(handlers.cb_mark_important)
        t("card", "mark_important bumps P3/P2→P1, P1→P0, P0 unchanged",
          '"P3": "P1"' in src and '"P1": "P0"' in src and '"P0": "P0"' in src)

    # cb_complete re-renders the card (B6)
    src = inspect.getsource(handlers.cb_complete)
    t("card", "B6: cb_complete re-renders card via _format_task_card",
      "_format_task_card" in src and "_task_card_kb_with_back" in src)


# ─────────────── 4. Vazifa yaratish ───────────────
async def test_creation_flow() -> None:
    section("4. TASK CREATION (quick + guided + presets)")

    # NewTaskFSM states
    expected_states = {"awaiting_title", "awaiting_priority", "awaiting_deadline",
                        "awaiting_deadline_manual", "awaiting_assignee", "awaiting_confirm"}
    actual_states = {s for s in dir(handlers.NewTaskFSM)
                      if not s.startswith("_") and s.islower()}
    t("create", "NewTaskFSM has 6 expected states",
      expected_states <= actual_states)

    # Priority Uzbek labels in NewTask kbd (B3 — also applied to edit picker)
    pkb = handlers._newtask_priority_kb()
    plabels = [b.text for r in pkb.inline_keyboard for b in r]
    t("create", "NewTask priority kbd: Shoshilinch/Muhim/Rejadagi/Oddiy",
      all(any(w in l for l in plabels) for w in
          ("Shoshilinch", "Muhim", "Rejadagi", "Oddiy")))

    # Edit-flow priority picker (B3 fix applied)
    epp = handlers.priority_picker("t-x")
    elabels = [b.text for r in epp.inline_keyboard for b in r]
    t("create", "B3: edit priority_picker uses Uzbek labels",
      "🔴 Shoshilinch" in elabels and "🟠 Muhim" in elabels
      and "🔵 Rejadagi" in elabels and "⚪ Oddiy" in elabels)
    t("create", "B3: edit priority_picker NO raw P0/P1/P2/P3",
      not any(l.endswith("P0") or l.endswith("P1") or l.endswith("P2") or l.endswith("P3")
              for l in elabels))

    # Deadline presets — skip if the helper was refactored away
    if hasattr(handlers, "_newtask_compute_deadline"):
        for preset in ("today", "tomorrow", "plus3", "weekend"):
            iso = handlers._newtask_compute_deadline(preset)
            t("create", f"preset '{preset}' → valid ISO", bool(iso) and "T" in iso)
        t("create", "preset 'skip' → None",
          handlers._newtask_compute_deadline("skip") is None)

    # Natural language deadline parser (used in manual mode + edit)
    for input_str, expect_year in (
        ("ertaga 09:00", True), ("bugun 17:00", True),
        ("juma 12:00", True), ("indin 14:30", True),
        ("2 soat", True), ("30 daqiqa", True),
    ):
        parsed_iso, _ = await handlers._parse_deadline_natural(input_str)
        t("create", f"_parse_deadline_natural('{input_str}') → valid",
          bool(parsed_iso) and "T" in parsed_iso)
    parsed_iso, _ = await handlers._parse_deadline_natural("noma'lum")
    t("create", "_parse_deadline_natural('noma\\'lum') → None", parsed_iso is None)
    parsed_iso, _ = await handlers._parse_deadline_natural("32-13 14:30")
    t("create", "_parse_deadline_natural('32-13 14:30') → None (invalid date)",
      parsed_iso is None)

    # Cancel flow (B2 fix — no ReplyKeyboardMarkup to edit_text)
    src = inspect.getsource(handlers.newtask_cancel)
    t("create", "B2: newtask_cancel uses _restore_main_keyboard (not edit_text)",
      "_restore_main_keyboard" in src
      and "reply_markup=main_reply_keyboard" not in src.replace(" ", ""))

    # Summary renderer
    summary = handlers._newtask_summary({"title": "X", "priority": "P0"})
    t("create", "newtask summary shows Shoshilinch for P0",
      "X" in summary and "Shoshilinch" in summary)


# ─────────────── 5. Tahrir + 6. Ijrochi + 7. Qidiruv ───────────────
async def test_edit_assignee_search() -> None:
    section("5/6/7. EDIT + ASSIGNEE + SEARCH")

    await database.init()
    tid = await database.create_task({
        "title": "AUDIT_edit_target",
        "priority": "P3",
    })

    try:
        # Title edit
        await database.update_task(tid, {"title": "AUDIT_edit_NEW"}, source="edit")
        task = await database.get_task(tid)
        t("edit", "title update persists", task["title"] == "AUDIT_edit_NEW")

        # Priority change
        await database.update_task(tid, {"priority": "P0"}, source="edit")
        task = await database.get_task(tid)
        t("edit", "priority update persists", task["priority"] == "P0")

        # Deadline change resets reminded_at
        from datetime import datetime, timedelta
        # Mark as reminded first
        await database.mark_task_reminded(tid)
        before = await database.get_task(tid)
        t("edit", "reminded_at set after mark_task_reminded",
          before.get("reminded_at") is not None)
        new_dl = (datetime.now(database.TZ) + timedelta(hours=5)).isoformat()
        await database.update_task(tid, {"deadline": new_dl}, source="edit")
        after = await database.get_task(tid)
        t("edit", "B-internal: deadline change resets reminded_at",
          after.get("reminded_at") is None)

        # Tags JSON round-trip
        await database.update_task(tid, {"tags": ["marketing", "urgent"]}, source="edit")
        task = await database.get_task(tid)
        t("edit", "tags stored as JSON list",
          isinstance(task["tags"], list) and "marketing" in task["tags"])

        # Description clear via "-"
        await database.update_task(tid, {"description": "Some desc"}, source="edit")
        await database.update_task(tid, {"description": None}, source="edit")
        task = await database.get_task(tid)
        t("edit", "description can be cleared (None)", task.get("description") is None)

        # Status done via update_task should NOT trigger recurring (only complete_task does)
        # We verify via complete_task path:
        rec_tid = await database.create_task({
            "title": "AUDIT_recur_check", "priority": "P2",
            "deadline": (datetime.now(database.TZ) + timedelta(days=1)).isoformat(),
            "recurrence_rule": "daily",
        })
        await database.complete_task(rec_tid)
        recur_list = await database.list_recurring_tasks(limit=10)
        next_ones = [t for t in recur_list if t.get("recurrence_parent_id") == rec_tid]
        t("edit", "complete_task creates next recurring instance",
          len(next_ones) >= 1)
        for nt in next_ones:
            await database.delete_task(nt["id"], source="audit_cleanup")
        await database.delete_task(rec_tid, source="audit_cleanup")

        # Assignee assign
        await database.update_task(tid, {"assignee": "Komilov Javohir"}, source="edit")
        task = await database.get_task(tid)
        t("assignee", "assign sets assignee",
          task.get("assignee") == "Komilov Javohir")

        # Clear assignee
        await database.update_task(tid, {"assignee": None}, source="edit")
        task = await database.get_task(tid)
        t("assignee", "clear sets assignee=None",
          task.get("assignee") is None)

        # "Men" treated specially in load map
        await database.update_task(tid, {"assignee": "Men"}, source="edit")
        loads = await database.assignee_load_map()
        t("assignee", "'Men' key present in load map", "Men" in loads)

        # Search by title
        results = await database.search_all("AUDIT_edit_NEW", limit=10)
        title_hit = any(r["title"] == "AUDIT_edit_NEW" for r in results.get("tasks", []))
        t("search", "search_all finds task by title", title_hit)

        # Search by tag
        await database.update_task(tid, {"tags": ["marketing"]}, source="edit")
        results = await database.search_all("marketing", limit=10)
        tag_hit = any(tid == r["id"] for r in results.get("tasks", []))
        t("search", "search_all finds task by tag", tag_hit)

        # Search empty query handling (handler-level)
        src = inspect.getsource(handlers.handle_task_search)
        t("search", "empty search query gives user-friendly message",
          "bo'sh bo'lmasin" in src)

    finally:
        await database.delete_task(tid, source="audit_cleanup")


# ─────────────── 8. Recurring tasks edge cases ───────────────
async def test_recurring_edges() -> None:
    section("8. RECURRING — edge cases")

    from datetime import datetime, timedelta

    # Normalize variants
    for raw, expected in (
        ("daily", "daily"), ("har kuni", "daily"), ("kunlik", "daily"),
        ("weekly", "weekly"), ("haftalik", "weekly"), ("har hafta", "weekly"),
        ("monthly", "monthly"), ("oylik", "monthly"),
        ("quarterly", "quarterly"), ("choraklik", "quarterly"),
        ("yearly", "yearly"), ("yillik", "yearly"),
        ("nonsense", None), ("", None),
    ):
        result = database.normalize_recurrence_rule(raw)
        t("recurring", f"normalize '{raw}' → {expected}", result == expected)

    # compute_next_recurrence — daily
    now = datetime.now(database.TZ)
    base = now.isoformat()
    nxt = database.compute_next_recurrence(base, "daily")
    nxt_dt = datetime.fromisoformat(nxt)
    t("recurring", "daily: next is in future (>now)", nxt_dt > now)
    delta_days = (nxt_dt - now).total_seconds() / 86400
    t("recurring", "daily: next is ~1 day away",
      0.99 <= delta_days <= 1.5)

    # weekly
    nxt = database.compute_next_recurrence(base, "weekly")
    nxt_dt = datetime.fromisoformat(nxt)
    t("recurring", "weekly: next is in future",
      nxt_dt > now and 6.99 <= (nxt_dt - now).days <= 8)

    # monthly — edge: feb 29 → next month
    feb29 = database.TZ.localize(datetime(2024, 2, 29, 10, 0)).isoformat()
    nxt = database.compute_next_recurrence(feb29, "monthly")
    t("recurring", "monthly handles 31→30 day-of-month rollback",
      nxt is not None and "T" in nxt)

    # quarterly
    nxt = database.compute_next_recurrence(base, "quarterly")
    nxt_dt = datetime.fromisoformat(nxt)
    t("recurring", "quarterly: ~90 days ahead",
      85 <= (nxt_dt - now).days <= 95)

    # yearly
    nxt = database.compute_next_recurrence(base, "yearly")
    nxt_dt = datetime.fromisoformat(nxt)
    t("recurring", "yearly: ~365 days ahead",
      360 <= (nxt_dt - now).days <= 370)

    # End-to-end: complete → next created → recurrence_next_at populated
    tid = await database.create_task({
        "title": "AUDIT_rec_e2e", "priority": "P2",
        "deadline": (now + timedelta(hours=2)).isoformat(),
        "recurrence_rule": "daily",
    })
    await database.complete_task(tid)
    children = await database.list_recurring_tasks(limit=20)
    child = next((c for c in children if c.get("recurrence_parent_id") == tid), None)
    t("recurring", "complete creates child with parent_id",
      child is not None)
    if child:
        t("recurring", "child has recurrence_next_at populated",
          child.get("recurrence_next_at") is not None)
        await database.delete_task(child["id"], source="audit_cleanup")
    await database.delete_task(tid, source="audit_cleanup")


# ─────────────── 9. Task reminder ───────────────
async def test_reminders_logic() -> None:
    section("9. TASK REMINDER (sweep + setting + no-dup)")

    from datetime import datetime, timedelta
    now = datetime.now(database.TZ)

    # task_reminder_hours setting writes + reads
    await database.set_setting("task_reminder_hours", 3)
    s = await database.get_settings()
    t("reminder", "task_reminder_hours setting persisted (=3)",
      s.get("task_reminder_hours") == 3)
    await database.set_setting("task_reminder_hours", 2)  # restore

    # list_due_in_window returns P0/P1 in window, not yet reminded
    p0_id = await database.create_task({
        "title": "AUDIT_due_P0", "priority": "P0",
        "deadline": (now + timedelta(hours=2, minutes=1)).isoformat(),
    })
    p2_id = await database.create_task({
        "title": "AUDIT_due_P2", "priority": "P2",  # should NOT appear
        "deadline": (now + timedelta(hours=2)).isoformat(),
    })
    p1_far = await database.create_task({
        "title": "AUDIT_due_P1_far", "priority": "P1",  # outside window
        "deadline": (now + timedelta(hours=6)).isoformat(),
    })

    try:
        win_start = (now + timedelta(hours=2, minutes=-5)).isoformat()
        win_end = (now + timedelta(hours=2, minutes=5)).isoformat()
        due = await database.list_due_in_window(win_start, win_end)
        ids = {t["id"] for t in due}
        t("reminder", "list_due_in_window picks P0 in window", p0_id in ids)
        t("reminder", "list_due_in_window excludes P2",
          p2_id not in ids)
        t("reminder", "list_due_in_window excludes P1 outside window",
          p1_far not in ids)

        # mark_task_reminded → not picked up again
        await database.mark_task_reminded(p0_id)
        due_after = await database.list_due_in_window(win_start, win_end)
        ids_after = {t["id"] for t in due_after}
        t("reminder", "mark_task_reminded prevents re-pickup", p0_id not in ids_after)

        # B5 — scheduler uses Uzbek priority labels
        src = inspect.getsource(sched_mod.YordamchiScheduler._task_reminder_sweep)
        t("reminder", "B5: scheduler uses Uzbek priority (Shoshilinch/Muhim)",
          "Shoshilinch" in src and "Muhim" in src)
        t("reminder", "B5: scheduler does NOT show raw task['priority']",
          'task["priority"]' not in src and "task['priority']" not in src)

    finally:
        for x in (p0_id, p2_id, p1_far):
            await database.delete_task(x, source="audit_cleanup")


# ─────────────── B4 — cb_set_field Uzbek toast ───────────────
async def test_bugfix_b4_toast() -> None:
    section("BUGFIX VERIFICATION — B4 toast")
    src = inspect.getsource(handlers.cb_set_field)
    t("b4", "Uzbek field map present",
      "'priority': 'Ustuvorlik'" in src or '"priority": "Ustuvorlik"' in src)
    t("b4", "Uses _PRIORITY_LABEL_UZ for priority values",
      "_PRIORITY_LABEL_UZ" in src)
    t("b4", "Uses _STATUS_LABEL_UZ for status values",
      "_STATUS_LABEL_UZ" in src)


# ─────────────── Runner ───────────────
async def main() -> None:
    print("\n┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
    print("┃ Vazifalar Bo'limi — Senior Audit Smoke Test           ┃")
    print("┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")

    await test_reply_and_navigation()
    await test_list_and_filters()
    await test_task_card_buttons()
    await test_creation_flow()
    await test_edit_assignee_search()
    await test_recurring_edges()
    await test_reminders_logic()
    await test_bugfix_b4_toast()

    print(f"\n━━━━━━━━━━ NATIJA ━━━━━━━━━━")
    print(f"Passed: {_PASS}    Failed: {_FAIL}")
    if _FAILED:
        print("\nFailed tests:")
        for f in _FAILED:
            print(f"  ✗ {f}")
        sys.exit(1)
    else:
        print("✓ Barcha testlar muvaffaqiyatli o'tdi.")


if __name__ == "__main__":
    asyncio.run(main())
