"""End-to-end integration check for Tasks & Meetings + cross-system consistency.

Runs against a THROWAWAY temp database (config.DATABASE_PATH is repointed before
any DB call), so the real ./data/yordamchi.db is never touched.

Run:  venv/bin/python tests/integration_check.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, time as dtime

# Allow running from anywhere — project root holds config.py / database.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Repoint DB to a temp file BEFORE importing database/handlers.
import config
_TMP = "/tmp/yordamchi_integration_test.db"
if os.path.exists(_TMP):
    os.remove(_TMP)
config.DATABASE_PATH = _TMP

import database  # noqa: E402
import handlers  # noqa: E402

TZ = database.TZ
PASS = 0
FAIL = 0
FAILED = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ❌ {name}   {detail}")


def has(lst, _id):
    return any((x.get("id") == _id) for x in lst)


async def main():
    await database.init()
    now = datetime.now(TZ)
    today_start = TZ.localize(datetime.combine(now.date(), dtime.min))
    wk_start = today_start.isoformat()
    wk_end = (today_start + timedelta(days=7)).isoformat()

    print("\n── A. VAZIFALAR ──")
    tid = await database.create_task({"title": "TEST vazifa A", "priority": "P2",
                                      "status": "todo", "assignee": "Aziz",
                                      "deadline": (now + timedelta(days=1)).isoformat(),
                                      "tags": ["test", "muhim"]})
    t = await database.get_task(tid)
    check("A1 create_task saqlanadi", t is not None and t["title"] == "TEST vazifa A")
    check("A1 tags ro'yxat sifatida o'qiladi", isinstance(t.get("tags"), list) and "test" in t["tags"],
          f"tags={t.get('tags')!r}")
    active = await database.list_tasks(status_in=["todo", "in_progress"], limit=200)
    done = await database.list_tasks(status_in=["done"], limit=200)
    check("A2 yangi vazifa AKTIV filtrda", has(active, tid))
    check("A2 yangi vazifa BAJARILGAN filtrda EMAS", not has(done, tid))

    # cross-system snapshot before complete
    active_before = len(active)
    done_before = len(done)

    print("\n── C1. todo → done (cross-system) ──")
    ok = await database.complete_task(tid)
    t = await database.get_task(tid)
    active2 = await database.list_tasks(status_in=["todo", "in_progress"], limit=200)
    done2 = await database.list_tasks(status_in=["done"], limit=200)
    done_today = await database.list_tasks_done_today()
    check("C1 complete_task ok", ok and t["status"] == "done")
    check("C1 AKTIV'dan yo'qoldi", not has(active2, tid))
    check("C1 BAJARILGAN'da paydo bo'ldi", has(done2, tid))
    check("C1 'bugun bajarilgan'da bor", has(done_today, tid))
    check("C1 sanoq izchil (aktiv -1, done +1)",
          len(active2) == active_before - 1 and len(done2) == done_before + 1,
          f"aktiv {active_before}->{len(active2)}, done {done_before}->{len(done2)}")

    print("\n── A4. Tahrir (prioritet) ──")
    await database.update_task(tid, {"priority": "P0"}, source="edit")
    t = await database.get_task(tid)
    check("A4 prioritet P0 ga o'zgardi", t["priority"] == "P0")

    print("\n── C2. O'chirish (cross-system) ──")
    await database.delete_task(tid)
    gone = await database.get_task(tid)
    a3 = await database.list_tasks(status_in=["todo", "in_progress"], limit=200)
    d3 = await database.list_tasks(status_in=["done"], limit=200)
    allt = await database.list_tasks(limit=500)
    check("C2 get_task None", gone is None)
    check("C2 hech bir filtrda yo'q (aktiv/done/barchasi)",
          not has(a3, tid) and not has(d3, tid) and not has(allt, tid))

    print("\n── A6. Bugungi muddatli vazifa ──")
    tid2 = await database.create_task({"title": "TEST bugun", "priority": "P1", "status": "todo",
                                       "deadline": (today_start + timedelta(hours=15)).isoformat()})
    today_tasks = await database.list_today_tasks()
    check("A6 bugungi muddat → list_today_tasks", has(today_tasks, tid2))
    await database.delete_task(tid2)

    print("\n── B/D1. Uchrashuv + AGENDA RO'YXATI (regressiya) ──")
    mdata = {"title": "TEST uchrashuv", "datetime_start": (now + timedelta(days=1)).isoformat(),
             "datetime_end": (now + timedelta(days=1, hours=1)).isoformat(),
             "participants": ["Dinislam", "Aziz"],
             "agenda": ["Byudjet", "Marketing", "Keyingi qadamlar"]}
    mid = await database.create_meeting(mdata)
    m = await database.get_meeting(mid)
    check("D1 agenda=ro'yxat bo'lsa ham SAQLANADI", m is not None, "create_meeting yiqildi")
    check("D1 agenda matn sifatida o'qiladi", isinstance(m.get("agenda"), str) and "Byudjet" in m["agenda"],
          f"agenda={m.get('agenda')!r}")
    check("D1 participants ro'yxat sifatida o'qiladi", isinstance(m.get("participants"), list) and "Aziz" in m["participants"])

    print("\n── B2/D2. Bugun ERTAROQ uchrashuv → 'Haftalik'da (regressiya) ──")
    earlier = today_start + timedelta(minutes=1)  # bugun, lekin hozirdan oldin
    mid2 = await database.create_meeting({"title": "TEST ertaroq", "datetime_start": earlier.isoformat(),
                                          "datetime_end": (earlier + timedelta(hours=1)).isoformat(),
                                          "participants": ["X"], "agenda": ["a"]})
    week = await database.list_meetings_in_window(wk_start, wk_end)
    week_now = await database.list_meetings_in_window(now.isoformat(), wk_end)  # eski (buggy) oyna
    today_m = await database.list_today_meetings()
    check("D2 'Haftalik' (bugun boshidan) ko'rsatadi", has(week, mid2))
    check("D2 'Bugun' filtrida bor", has(today_m, mid2))
    check("D2 (eslatma) eski now-oyna ko'rsatMASdi", not has(week_now, mid2),
          "eski oyna ham ko'rsatsa, regressiya emas edi")

    print("\n── C4. Reschedule (cross-system) ──")
    new_start = (now + timedelta(days=2, hours=3)).isoformat()
    await database.update_meeting(mid, {"datetime_start": new_start})
    m = await database.get_meeting(mid)
    check("C4 datetime_start yangilandi", m["datetime_start"] == new_start)
    w2 = await database.list_meetings_in_window(wk_start, (today_start + timedelta(days=10)).isoformat())
    found = [x for x in w2 if x["id"] == mid]
    check("C4 oyna so'rovida yangi vaqt bilan chiqadi", bool(found) and found[0]["datetime_start"] == new_start)

    print("\n── C5. Bekor (cross-system) ──")
    await database.cancel_meeting(mid)
    await database.cancel_meeting(mid2)
    check("C5 get_meeting None", (await database.get_meeting(mid)) is None)
    w3 = await database.list_meetings_in_window(wk_start, (today_start + timedelta(days=10)).isoformat())
    check("C5 oyna so'rovidan yo'qoldi", not has(w3, mid) and not has(w3, mid2))

    print("\n── D5. Sana parser — sabab ajratiladi ──")
    cases = {"2 soat": None, "ertaga 09:00": None, "999 soat": "too_far",
             "2026-13-45 10:00": "invalid", "falongdek": "unparsable"}
    for txt, exp in cases.items():
        iso, reason = await handlers._parse_deadline_natural(txt)
        if exp is None:
            check(f"D5 '{txt}' → parse OK", iso is not None and reason is None, f"reason={reason}")
        else:
            check(f"D5 '{txt}' → '{exp}'", iso is None and reason == exp, f"reason={reason}")

    print("\n── Stats ishlaydi (cross-system yuzasi) ──")
    try:
        st = await database.executive_stats(days=7)
        check("executive_stats xatosiz ishlaydi", isinstance(st, dict) and "tasks" in st)
    except Exception as e:
        check("executive_stats xatosiz ishlaydi", False, f"{type(e).__name__}: {e}")

    print("\n── Export / Import (Excel) ──")
    import io as _io
    from openpyxl import load_workbook as _lwb

    class _ExpMsg:
        chat = type("C", (), {"id": 1})()
        captured = {}
        async def answer_document(self, file, caption=None, parse_mode=None):
            _ExpMsg.captured["b"] = file.data
        async def answer(self, *a, **k):
            _ExpMsg.captured["t"] = a

    await database.create_task({"title": "Export sinov", "assignee": "J.K", "priority": "P0",
                                "status": "todo", "deadline": (datetime.now(TZ) + timedelta(days=2)).isoformat(),
                                "description": "izoh"})
    await handlers.cmd_export(_ExpMsg())
    try:
        _ws = _lwb(_io.BytesIO(_ExpMsg.captured["b"])).active
        check("Export: xlsx yaratildi", _ExpMsg.captured.get("b") is not None)
        check("Export: sarlavha B1", _ws["B1"].value == "VAZIFALAR RO'YXATI")
        check("Export: header A3=№ B3=Vazifa", _ws["A3"].value == "№" and _ws["B3"].value == "Vazifa")
        check("Export: header Arial 14", _ws["B3"].font.name == "Arial" and int(_ws["B3"].font.sz) == 14)
        check("Export: yashirin ID ustuni (H)", _ws["H3"].value == "ID" and bool(_ws.column_dimensions["H"].hidden))
    except Exception as e:
        check("Export: xlsx tahlili", False, f"{type(e).__name__}: {e}")

    _acts = handlers._structured_tasks_from_table(
        [("№", "Vazifa", "Ijrochi", "Muddat", "Ustuvorlik", "Holat", "Izoh"),
         (1, "Import A", "Aziz", "08-06-2026", "Shoshilinch", "Aktiv", "x")])
    check("Import (struktura): 1 vazifa", len(_acts) == 1 and _acts[0]["data"]["title"] == "Import A")
    check("Import: Shoshilinch→P0", bool(_acts) and _acts[0]["data"]["priority"] == "P0")
    check("Import: noma'lum ustun → [] (aqlli yo'lga)", handlers._structured_tasks_from_table([("Mahsulot", "Narx", "Soni"), ("Olma", "5000", "10")]) == [])
    check("Import: Mas'ul ustuni → ijrochi", (lambda a: bool(a) and a[0]["data"].get("assignee") == "O.X")(handlers._structured_tasks_from_table([("Vazifa", "Mas'ul"), ("Ish", "O.X")])))
    check("Import: izoh==ijrochi → izoh tushiriladi", (lambda a: bool(a) and not a[0]["data"].get("description"))(handlers._structured_tasks_from_table([("Vazifa", "Ijrochi", "Izoh"), ("Ish", "Aziz", "Aziz")])))

    print("\n── Import round-trip dedup (yangilash, dublikat emas) ──")
    _t = await database.create_task({"title": "Dedup sinov", "priority": "P2", "status": "todo"})
    _a2 = handlers._structured_tasks_from_table(
        [("№", "Vazifa", "Ijrochi", "Muddat", "Ustuvorlik", "Holat", "Izoh", "ID"),
         (1, "Dedup YANGI", "Dilshod", "", "Muhim", "Aktiv", "", _t)])
    check("Import: ID ustuni o'qiladi", bool(_a2) and _a2[0].get("_id") == _t)
    for _a in _a2:
        _rid = _a.pop("_id", "")
        if _rid and await database.get_task(_rid):
            _a["type"] = "update_task"; _a["id"] = _rid
    check("Import dedup: mavjud ID → update_task", _a2[0]["type"] == "update_task")
    await handlers._execute_actions(_a2)
    _cnt = len([t for t in await database.list_tasks(limit=999) if t["title"] in ("Dedup sinov", "Dedup YANGI")])
    _upd = await database.get_task(_t)
    check("Import dedup: yangilandi, dublikat YO'Q", _cnt == 1 and _upd["title"] == "Dedup YANGI" and _upd["assignee"] == "Dilshod", f"cnt={_cnt}")

    # Title-based dedup (covers files with NO hidden ID + smart-extracted PDFs)
    _bt = {(t.get("title") or "").strip().lower(): t["id"]
           for t in await database.list_tasks(status_in=["todo", "in_progress", "blocked"], limit=999)
           if t.get("title")}
    _new = [{"type": "create_task", "data": {"title": "Dedup YANGI", "priority": "P0"}},
            {"type": "create_task", "data": {"title": "Mutlaqo yangi XYZ", "priority": "P2"}}]
    _conv = handlers._apply_title_dedup(_new, _bt)
    check("Title dedup: mavjud sarlavha → update", _new[0]["type"] == "update_task" and _conv == 1)
    check("Title dedup: yangi sarlavha → create", _new[1]["type"] == "create_task")

    print("\n── _humanize_error (aniq sabab) ──")
    check("humanize: tarmoq", "ulanish" in handlers._humanize_error(Exception("Cannot connect [Network is unreachable]")).lower())
    check("humanize: bo'sh xabar", "bo'sh xabar" in handlers._humanize_error(Exception("messages.8: user messages must have non-empty content")).lower())
    check("humanize: noma'lum → tur ko'rsatiladi", "ValueError" in handlers._humanize_error(ValueError("nimadir")))

    print("\n── Ikonka birligi (list ↔ batafsil) ──")
    _p2 = {"title": "P2 sinov", "priority": "P2", "status": "todo", "deadline": None}
    check("Badge: P2 list=batafsil bir xil", handlers._format_task_detail_card(_p2).startswith(handlers._task_badge(_p2)))

    print("\n── /delegations so'rovi xatosiz ──")
    try:
        import aiosqlite as _sq
        async with _sq.connect(config.DATABASE_PATH) as _db:
            await _db.execute("SELECT * FROM tasks WHERE status IN ('todo','in_progress') "
                              "AND assignee IS NOT NULL AND LOWER(TRIM(assignee)) NOT IN "
                              "('','men','siz','belgilanmagan','—','oʻzim','o''zim','o''z','ozim') LIMIT 5")
        check("/delegations SQL ishlaydi", True)
    except Exception as e:
        check("/delegations SQL ishlaydi", False, f"{type(e).__name__}: {e}")

    print("\n" + "=" * 48)
    print(f"NATIJA:  ✅ {PASS} o'tdi   ❌ {FAIL} yiqildi")
    if FAILED:
        print("Yiqilganlar:", ", ".join(FAILED))
    print("=" * 48)
    # cleanup temp db
    try:
        os.remove(_TMP)
    except OSError:
        pass
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
