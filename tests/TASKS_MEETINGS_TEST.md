# 🧪 TEST PROMPTI — Vazifalar va Uchrashuvlar (to'liq + cross-system)

## Maqsad
Vazifalar va Uchrashuvlar bo'limlarini boshidan-oxirigacha sinash.
**Asosiy invariant:** bitta joyda holat o'zgarsa (yaratish / bajarish / tahrir /
o'chirish / ko'chirish), u **butun tizimda** (barcha ro'yxatlar, filtrlar,
`/today`, `/cockpit`, `/stats`, brifing, kalendar, eslatma) izchil aks etishi shart.
Har bir amaldan keyin **hech qanday jim xato bo'lmasligi** va ma'lumot
**haqiqatan saqlanishi** tekshiriladi.

## Tayyorgarlik
- Bot ishlayotganini tasdiqlang (`/start` → javob keladi).
- Test foydalanuvchisi = principal (boshqa user rad etilishini ham bir marta tekshiring).
- Boshlang'ich holatni qayd eting: `/stats` (jami soni, risk score) va `/today`.
- Har "saqlandi" amaldan keyin **bo'limni qayta oching** — ma'lumot rostdan turibdimi
  tekshiring (faqat "✅ yaratildi" xabariga ishonmang).

---

## A. VAZIFALAR (Tasks)

**A1. Yaratish — 3 yo'l**
1. Ovoz: _"Ertaga soat 10:00 da Aziz akaga hisobot yuborish, muhim"_ → tasdiq → `/tasks`(Aktiv)da, ijrochi=Aziz, prioritet=Muhim, muddat=ertaga 10:00.
2. Matn: xuddi shunday.
3. Forma: `/new` → 📝 Forma → sarlavha → prioritet → kun→vaqt picker (Ertaga→14:00) → ijrochi → tasdiq.
- ✅ Har uchchasi bazaga tushadi va Aktiv ro'yxatda darhol ko'rinadi (auto-refresh).

**A2. Filtrlar** — `/tasks`: `Aktiv · Bugun · Muhim · Shoshilinch · Muddati o'tgan · Bajarilgan · Takroriy · Barchasi`.

**A3. Ochish/amallar** — karta (⋯ Batafsil + ⬅️ Ro'yxatga) → Batafsil → 👤 Ijrochi · ✅ Bajarildi · 📅 Muddat · ⭐ Muhim · ✏️ Tahrir · 🗑 O'chirish.

**A4. Tahrir** — Sarlavha/Tavsif/Prioritet/Muddat/Status/Teglar; har birini saqlab tekshiring.

**A5. Takroriy** — _"Har dushanba 09:00 da planerka"_ → `/recurring`da; bajarilganda keyingi nusxa.

**A6. Eslatma** — muddatli vazifa `/reminders`da eslatma yaratadimi.

---

## B. UCHRASHUVLAR (Meetings)

**B1. Yaratish**
1. _"Ertaga soat 12:00 da Dinislam bilan biznes forum, kun tartibi: byudjet, marketing"_
2. _"Bugun soat 9:00 da jamoa stand-up"_ (← bugun ertaroq vaqt — qasddan)
- ✅ Ikkalasi bazaga tushadi; agenda ro'yxat sifatida saqlanadi; `/meetings`(Haftalik)da ikkalasi ham.

**B2. Filtrlar** — `Bugun · Ertaga · Haftalik · Barchasi · O'tgan`.

**B3. Ochish/amallar** — 📝 Bayonnoma · 🔄 Vaqtni o'zgartirish · ✏️ Tahrir · ✕ Bekor.

**B4. iCloud kalendar** — yaratish/reschedule/bekor kalendarga sync.

**B5. Prep brief** — uchrashuvdan oldin tayyorgarlik xabari.

---

## C. ⭐ CROSS-SYSTEM IZCHILLIK (eng muhim)

**C1. todo → done:** ✅ Bajarildi → vazifa Aktiv'dan yo'q, Bajarilgan'da bor;
`/today`, `/cockpit`, `/stats`(done+1, risk), brifing — hammasi yangilangan.
**C2. O'chirish:** barcha filtr/`/today`/`/stats`/`/cockpit`dan yo'q.
**C3. Prioritet/muddat:** P2→P0 → Shoshilinch/Muhim filtr + cockpit/risk darhol.
**C4. Reschedule:** `/meetings` filtrlari + `/today` + eslatma + iCloud — yangi vaqtga.
**C5. Bekor:** `/meetings` + `/today` + kalendar + eslatma — o'chsin.
**C6. Auto-refresh:** bo'lim ochiq turib yangi qo'shsang — ro'yxat avtomatik yangilanadi.

---

## D. REGRESSIYA (yaqinda tuzatilgan)

- D1. Agenda bilan uchrashuv saqlanadi (avval jim yiqilardi).
- D2. Bugun ertaroq uchrashuv "Haftalik"da ko'rinadi.
- D3. Yaratishda xato bo'lsa "⚠️ Saqlanmadi: ..." ogohlantirishi.
- D4. Vazifa ochilganda faqat ⋯ Batafsil + ⬅️ Ro'yxatga.
- D5. `999 soat` / `2026-13-45` → har biriga alohida aniq xabar.

---

## Belgilash
Har punkt: ✅ O'tdi / ❌ Yiqildi (+qadamlar) / ⚠️ Qisman.
❌/⚠️ bo'lsa: qaysi yuzada stale qoldi yoki nima saqlanmadi — yozing.
