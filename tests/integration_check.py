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
