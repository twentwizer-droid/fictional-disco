"""Админ-панель: тексты и кнопки, города, магазины, категории, товары, цены,
наличие, вопросы покупателей, рассылка, статистика."""

import asyncio
import re

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import db
import texts
from support import question_kb, question_text, send_answer
from texts import DEFAULTS, GROUPS, T
from ui import IsAdmin, e, ikb, main_kb, price, show

router = Router()
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())


class AdminForm(StatesGroup):
    value = State()


# Что можно редактировать: таблица -> поле -> (подпись, подсказка)
FIELDS = {
    "cities": {"name": ("Название", "Отправьте новое название города.")},
    "shops": {
        "name": ("Название", "Отправьте название магазина."),
        "address": ("Адрес", "Отправьте адрес магазина."),
        "hours": ("Часы работы", "Например: Ежедневно 10:00–22:00"),
        "phone": ("Телефон", "Отправьте телефон или «-», чтобы убрать."),
        "location": (
            "Точка на карте",
            "Отправьте геолокацию (📎 → Геопозиция) или координаты текстом: "
            "<code>55.7558, 37.6173</code>. «-» — убрать.",
        ),
    },
    "categories": {"name": ("Название", "Отправьте новое название категории.")},
    "products": {
        "name": ("Название", "Отправьте название товара."),
        "description": ("Описание", "Отправьте описание или «-», чтобы убрать."),
        "photo": ("Фото", "Отправьте фото товара или «-», чтобы убрать."),
    },
    "variants": {
        "title": ("Название варианта", "Например: 30 мл · 20 мг"),
        "price": ("Цена", "Отправьте цену числом, например: 450"),
    },
}

PARENT_SCREEN = {
    "cities": lambda row: "a:cities",
    "shops": lambda row: f"a:city:{row['city_id']}",
    "categories": lambda row: "a:cats",
    "products": lambda row: f"a:cat:{row['category_id']}",
    "variants": lambda row: f"a:prod:{row['product_id']}",
}

ADD_PROMPTS = {
    "city": "Отправьте название нового города.",
    "shop": "Отправьте название нового магазина (адрес, часы и точку на карте добавите потом).",
    "cat": "Отправьте название новой категории.",
    "prod": "Отправьте название нового товара.",
    "var": "Отправьте вариант и цену через «;»\nНапример: <code>30 мл · 20 мг; 450</code>",
}

CANCEL_ROW = [("✖️ Отмена", "a:cancel")]


# ================= экраны =================


async def screen(data: str) -> tuple[str, object]:
    p = data.split(":")
    cmd = p[1]
    arg = p[2] if len(p) > 2 else None

    if cmd == "menu":
        open_q = (await db.fetch_one("SELECT COUNT(*) n FROM questions WHERE status='open'"))["n"]
        return "⚙️ <b>Админ-панель</b>", ikb(
            [
                [("✏️ Тексты и кнопки", "a:tg")],
                [("🏙 Города и магазины", "a:cities")],
                [("📂 Категории, товары, цены", "a:cats")],
                [(f"💬 Вопросы ({open_q} ждут ответа)", "a:qs")],
                [("📢 Рассылка", "a:bc"), ("📊 Статистика", "a:stats")],
            ]
        )

    # ---------- тексты ----------
    if cmd == "tg":
        if arg is None:
            rows = [[(title, f"a:tg:{g}")] for g, title in GROUPS.items()]
            rows.append([("⬅️ Назад", "a:menu")])
            return (
                "✏️ <b>Тексты и кнопки</b>\n\nВыберите раздел. Любую кнопку и любое "
                "сообщение бота можно переименовать.",
                ikb(rows),
            )
        rows = []
        for key, (group, title, _) in DEFAULTS.items():
            if group == arg:
                value = re.sub(r"<[^>]+>", "", T(key)).replace("\n", " ")
                label = f"{title}: {value}"
                rows.append([(label[:60], f"a:t:{key}")])
        rows.append([("⬅️ Назад", "a:tg")])
        return f"{GROUPS.get(arg, '')}\n\nНажмите, чтобы изменить:", ikb(rows)

    if cmd == "t":
        key = arg
        group, title, _ = DEFAULTS[key]
        current = e(T(key)) if texts.is_plain(key) else T(key)
        return (
            f"✏️ <b>{e(title)}</b>\n\nСейчас:\n{current}\n\n"
            "Отправьте новое значение следующим сообщением."
            + ("" if texts.is_plain(key) else "\nЖирный, курсив и ссылки сохранятся."),
            ikb([[("↩️ Вернуть по умолчанию", f"a:treset:{key}")], [("⬅️ Назад", f"a:tg:{group}")]]),
        )

    # ---------- города и магазины ----------
    if cmd == "cities":
        cities = await db.fetch_all(
            "SELECT c.*, (SELECT COUNT(*) FROM shops s WHERE s.city_id=c.id) n "
            "FROM cities c ORDER BY name"
        )
        rows = [[(f"{c['name']} ({c['n']})", f"a:city:{c['id']}")] for c in cities]
        rows.append([("➕ Добавить город", "a:add:city:0")])
        rows.append([("⬅️ Назад", "a:menu")])
        return "🏙 <b>Города</b> (в скобках — число магазинов)", ikb(rows)

    if cmd == "city":
        c = await db.fetch_one("SELECT * FROM cities WHERE id=?", int(arg))
        if not c:
            return await screen("a:cities")
        shops = await db.fetch_all("SELECT * FROM shops WHERE city_id=? ORDER BY name", c["id"])
        rows = [[(f"🏬 {s['name']}", f"a:shop:{s['id']}")] for s in shops]
        rows += [
            [("➕ Добавить магазин", f"a:add:shop:{c['id']}")],
            [("✏️ Переименовать", f"a:set:cities:{c['id']}:name"), ("🗑 Удалить", f"a:del:cities:{c['id']}")],
            [("⬅️ Назад", "a:cities")],
        ]
        return f"🏙 <b>{e(c['name'])}</b>\nМагазинов: {len(shops)}", ikb(rows)

    if cmd == "shop":
        s = await db.fetch_one("SELECT * FROM shops WHERE id=?", int(arg))
        if not s:
            return await screen("a:cities")
        geo = f"{s['lat']:.5f}, {s['lon']:.5f}" if s["lat"] is not None else "—"
        text = (
            f"🏬 <b>{e(s['name'])}</b>\n\n"
            f"📍 Адрес: {e(s['address']) or '—'}\n"
            f"🕘 Часы: {e(s['hours']) or '—'}\n"
            f"📞 Телефон: {e(s['phone']) or '—'}\n"
            f"🗺 Точка на карте: {geo}"
        )
        sid = s["id"]
        rows = [
            [("✏️ Название", f"a:set:shops:{sid}:name"), ("📍 Адрес", f"a:set:shops:{sid}:address")],
            [("🕘 Часы", f"a:set:shops:{sid}:hours"), ("📞 Телефон", f"a:set:shops:{sid}:phone")],
            [("🗺 Точка на карте", f"a:set:shops:{sid}:location")],
            [("🗑 Удалить магазин", f"a:del:shops:{sid}")],
            [("⬅️ Назад", f"a:city:{s['city_id']}")],
        ]
        return text, ikb(rows)

    # ---------- категории и товары ----------
    if cmd == "cats":
        cats = await db.fetch_all(
            "SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) n "
            "FROM categories c ORDER BY name"
        )
        rows = [[(f"{c['name']} ({c['n']})", f"a:cat:{c['id']}")] for c in cats]
        rows.append([("➕ Добавить категорию", "a:add:cat:0")])
        rows.append([("⬅️ Назад", "a:menu")])
        return "📂 <b>Категории</b> (в скобках — число товаров)", ikb(rows)

    if cmd == "cat":
        c = await db.fetch_one("SELECT * FROM categories WHERE id=?", int(arg))
        if not c:
            return await screen("a:cats")
        prods = await db.fetch_all(
            "SELECT * FROM products WHERE category_id=? ORDER BY name", c["id"]
        )
        rows = [[(f"📦 {p['name']}", f"a:prod:{p['id']}")] for p in prods]
        rows += [
            [("➕ Добавить товар", f"a:add:prod:{c['id']}")],
            [("✏️ Переименовать", f"a:set:categories:{c['id']}:name"), ("🗑 Удалить", f"a:del:categories:{c['id']}")],
            [("⬅️ Назад", "a:cats")],
        ]
        return f"📂 <b>{e(c['name'])}</b>\nТоваров: {len(prods)}", ikb(rows)

    if cmd == "prod":
        pr = await db.fetch_one("SELECT * FROM products WHERE id=?", int(arg))
        if not pr:
            return await screen("a:cats")
        variants = await db.fetch_all(
            "SELECT * FROM variants WHERE product_id=? ORDER BY price, title", pr["id"]
        )
        pid = pr["id"]
        text = (
            f"📦 <b>{e(pr['name'])}</b>\n\n"
            f"📝 Описание: {e(pr['description']) or '—'}\n"
            f"🖼 Фото: {'есть' if pr['photo'] else 'нет'}\n\n"
            "<b>Варианты и цены</b> (нажмите, чтобы изменить цену и наличие):"
        )
        rows = [[(f"{v['title']} — {price(v['price'])}", f"a:var:{v['id']}")] for v in variants]
        rows += [
            [("➕ Добавить вариант/цену", f"a:add:var:{pid}")],
            [("✏️ Название", f"a:set:products:{pid}:name"), ("📝 Описание", f"a:set:products:{pid}:description")],
            [("🖼 Фото", f"a:set:products:{pid}:photo"), ("🗑 Удалить", f"a:del:products:{pid}")],
            [("⬅️ Назад", f"a:cat:{pr['category_id']}")],
        ]
        return text, ikb(rows)

    if cmd == "var":
        v = await db.fetch_one(
            "SELECT v.*, p.name AS product FROM variants v JOIN products p ON p.id=v.product_id "
            "WHERE v.id=?",
            int(arg),
        )
        if not v:
            return await screen("a:cats")
        shops = await db.fetch_all(
            "SELECT s.id, s.name, c.name AS city, EXISTS(SELECT 1 FROM stock st "
            "WHERE st.shop_id=s.id AND st.variant_id=?) AS avail "
            "FROM shops s JOIN cities c ON c.id=s.city_id ORDER BY c.name, s.name",
            v["id"],
        )
        vid = v["id"]
        text = (
            f"📦 {e(v['product'])}\n🔹 <b>{e(v['title'])}</b>\n💰 {price(v['price'])}\n\n"
            "<b>Наличие по магазинам</b> (нажмите, чтобы переключить):"
        )
        if not shops:
            text += "\nСначала добавьте магазины."
        rows = [
            [(("✅ " if s["avail"] else "❌ ") + f"{s['city']} — {s['name']}", f"a:stk:{vid}:{s['id']}")]
            for s in shops
        ]
        if shops:
            rows.append([("✅ Есть везде", f"a:stkall:{vid}:1"), ("❌ Нет нигде", f"a:stkall:{vid}:0")])
        rows += [
            [("✏️ Название", f"a:set:variants:{vid}:title"), ("💰 Цена", f"a:set:variants:{vid}:price")],
            [("🗑 Удалить вариант", f"a:del:variants:{vid}")],
            [("⬅️ Назад", f"a:prod:{v['product_id']}")],
        ]
        return text, ikb(rows)

    # ---------- вопросы ----------
    if cmd == "qs":
        qs = await db.fetch_all(
            "SELECT q.id, q.text, u.full_name FROM questions q LEFT JOIN users u "
            "ON u.id=q.user_id WHERE q.status='open' ORDER BY q.created_at LIMIT 30"
        )
        rows = [
            [(f"#Q{q['id']} {q['full_name'] or ''}: {q['text']}"[:60], f"a:q:{q['id']}")]
            for q in qs
        ]
        rows.append([("⬅️ Назад", "a:menu")])
        text = "💬 <b>Вопросы, ждущие ответа</b>" if qs else "💬 Неотвеченных вопросов нет 👍"
        return text, ikb(rows)

    if cmd == "q":
        q = await db.fetch_one("SELECT user_id FROM questions WHERE id=?", int(arg))
        if not q:
            return await screen("a:qs")
        kb = question_kb(int(arg), q["user_id"])
        kb.inline_keyboard.append(ikb([[("⬅️ К списку", "a:qs")]]).inline_keyboard[0])
        return await question_text(int(arg)), kb

    if cmd == "stats":
        async def n(sql):
            return (await db.fetch_one(sql))["n"]

        open_sql = "SELECT COUNT(*) n FROM questions WHERE status='open'"

        text = (
            "📊 <b>Статистика</b>\n\n"
            f"👤 Пользователей: {await n('SELECT COUNT(*) n FROM users')}\n"
            f"🔞 Подтвердили 18+: {await n('SELECT COUNT(*) n FROM users WHERE age_ok=1')}\n"
            f"🚫 Заблокировано: {await n('SELECT COUNT(*) n FROM users WHERE blocked=1')}\n\n"
            f"🏙 Городов: {await n('SELECT COUNT(*) n FROM cities')}\n"
            f"🏬 Магазинов: {await n('SELECT COUNT(*) n FROM shops')}\n"
            f"📂 Категорий: {await n('SELECT COUNT(*) n FROM categories')}\n"
            f"📦 Товаров: {await n('SELECT COUNT(*) n FROM products')}\n"
            f"🔹 Вариантов: {await n('SELECT COUNT(*) n FROM variants')}\n\n"
            f"💬 Вопросов всего: {await n('SELECT COUNT(*) n FROM questions')}\n"
            f"🟡 Ждут ответа: {await n(open_sql)}"
        )
        return text, ikb([[("⬅️ Назад", "a:menu")]])

    return await screen("a:menu")


async def send_screen(msg: Message, data: str) -> None:
    text, kb = await screen(data)
    await msg.answer(text, reply_markup=kb)


# ================= команды и навигация =================


@router.message(Command("admin"))
async def cmd_admin(msg: Message, state: FSMContext):
    await state.clear()
    await send_screen(msg, "a:menu")


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await send_screen(msg, "a:menu")


@router.callback_query(F.data == "a:cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(call, *await screen("a:menu"))
    await call.answer("Отменено")


@router.callback_query(F.data.regexp(r"^a:(menu|tg|t|cities|city|shop|cats|cat|prod|var|qs|q|stats)(:[\w]+)?$"))
async def cb_screen(call: CallbackQuery, state: FSMContext):
    await state.clear()
    if call.data.startswith("a:t:"):
        # экран редактирования текста сразу ждёт новое значение
        key = call.data.split(":")[2]
        await state.set_state(AdminForm.value)
        await state.update_data(action="text", key=key)
    await show(call, *await screen(call.data))
    await call.answer()


@router.callback_query(F.data.startswith("a:treset:"))
async def cb_text_reset(call: CallbackQuery, state: FSMContext):
    await state.clear()
    key = call.data.split(":")[2]
    await texts.reset(key)
    await call.answer("Возвращено значение по умолчанию")
    if key.startswith("btn_"):
        await call.message.answer("Клавиатура обновлена", reply_markup=main_kb())
    await show(call, *await screen(f"a:tg:{DEFAULTS[key][0]}"))


# ================= редактирование =================


@router.callback_query(F.data.startswith("a:set:"))
async def cb_set(call: CallbackQuery, state: FSMContext):
    _, _, table, row_id, field = call.data.split(":")
    if field not in FIELDS.get(table, {}):
        await call.answer("Нельзя", show_alert=True)
        return
    title, hint = FIELDS[table][field]
    await state.set_state(AdminForm.value)
    await state.update_data(action="set", table=table, id=int(row_id), field=field)
    await show(call, f"✏️ <b>{title}</b>\n\n{hint}", ikb([CANCEL_ROW]))
    await call.answer()


@router.callback_query(F.data.startswith("a:add:"))
async def cb_add(call: CallbackQuery, state: FSMContext):
    _, _, kind, parent = call.data.split(":")
    await state.set_state(AdminForm.value)
    await state.update_data(action="add", kind=kind, parent=int(parent))
    await show(call, "➕ " + ADD_PROMPTS[kind], ikb([CANCEL_ROW]))
    await call.answer()


@router.callback_query(F.data.startswith("a:del:"))
async def cb_delete_ask(call: CallbackQuery):
    _, _, table, row_id = call.data.split(":")
    row = await db.fetch_one(f"SELECT * FROM {table} WHERE id=?", int(row_id)) if table in FIELDS else None
    if not row:
        await call.answer("Уже удалено")
        return
    name = row["title"] if table == "variants" else row["name"]
    warn = {
        "cities": "Будут удалены и все магазины этого города.",
        "categories": "Будут удалены и все товары этой категории.",
        "products": "Будут удалены и все варианты/цены.",
    }.get(table, "")
    await show(
        call,
        f"🗑 Удалить «{e(name)}»?\n{warn}",
        ikb([[("✅ Да, удалить", f"a:delok:{table}:{row_id}"), ("✖️ Нет", PARENT_SCREEN[table](row))]]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:delok:"))
async def cb_delete(call: CallbackQuery):
    _, _, table, row_id = call.data.split(":")
    row = await db.fetch_one(f"SELECT * FROM {table} WHERE id=?", int(row_id)) if table in FIELDS else None
    if row:
        await db.execute(f"DELETE FROM {table} WHERE id=?", int(row_id))
        back = PARENT_SCREEN[table](row)
    else:
        back = "a:menu"
    await show(call, *await screen(back))
    await call.answer("Удалено")


@router.callback_query(F.data.startswith("a:stk:"))
async def cb_stock_toggle(call: CallbackQuery):
    _, _, vid, sid = call.data.split(":")
    if await db.fetch_one("SELECT 1 FROM stock WHERE variant_id=? AND shop_id=?", int(vid), int(sid)):
        await db.execute("DELETE FROM stock WHERE variant_id=? AND shop_id=?", int(vid), int(sid))
    else:
        await db.execute("INSERT OR IGNORE INTO stock (shop_id, variant_id) VALUES (?, ?)", int(sid), int(vid))
    await show(call, *await screen(f"a:var:{vid}"))
    await call.answer()


@router.callback_query(F.data.startswith("a:stkall:"))
async def cb_stock_all(call: CallbackQuery):
    _, _, vid, on = call.data.split(":")
    await db.execute("DELETE FROM stock WHERE variant_id=?", int(vid))
    if on == "1":
        await db.execute(
            "INSERT INTO stock (shop_id, variant_id) SELECT id, ? FROM shops", int(vid)
        )
    await show(call, *await screen(f"a:var:{vid}"))
    await call.answer()


# ================= вопросы покупателей =================


@router.callback_query(F.data.startswith("a:ans:"))
async def cb_answer(call: CallbackQuery, state: FSMContext):
    qid = int(call.data.split(":")[2])
    await state.set_state(AdminForm.value)
    await state.update_data(action="answer", qid=qid)
    await call.message.answer(
        f"✍️ Напишите ответ на вопрос #Q{qid} — он придёт покупателю в бота.",
        reply_markup=ikb([CANCEL_ROW]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("a:qclose:"))
async def cb_close(call: CallbackQuery):
    qid = int(call.data.split(":")[2])
    await db.execute("UPDATE questions SET status='closed' WHERE id=?", qid)
    await call.answer(f"Вопрос #Q{qid} закрыт")
    await show(call, *await screen("a:qs"))


@router.callback_query(F.data.startswith("a:ban:"))
async def cb_ban(call: CallbackQuery):
    uid = int(call.data.split(":")[2])
    await db.execute("UPDATE users SET blocked = 1 - blocked WHERE id=?", uid)
    row = await db.fetch_one("SELECT blocked FROM users WHERE id=?", uid)
    await call.answer(
        "🚫 Пользователь заблокирован" if row and row["blocked"] else "✅ Пользователь разблокирован",
        show_alert=True,
    )


@router.callback_query(F.data == "a:bc")
async def cb_broadcast(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminForm.value)
    await state.update_data(action="broadcast")
    await show(
        call,
        "📢 Отправьте сообщение для рассылки (текст, фото, видео — что угодно).\n"
        "Вместе с ним у всех обновится клавиатура меню.",
        ikb([CANCEL_ROW]),
    )
    await call.answer()


# ================= приём значений от админа =================


def parse_price(text: str | None) -> int | None:
    digits = re.sub(r"[^\d]", "", (text or "").split(",")[0].split(".")[0])
    return int(digits) if digits else None


@router.message(AdminForm.value)
async def on_value(msg: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    action = data.get("action")

    if action == "broadcast":
        await state.clear()
        users = await db.fetch_all("SELECT id FROM users WHERE blocked=0 AND age_ok=1")
        ok = 0
        await msg.answer(f"⏳ Рассылка на {len(users)} пользователей...")
        for u in users:
            try:
                await msg.copy_to(u["id"], reply_markup=main_kb())
                ok += 1
            except TelegramAPIError:
                pass
            await asyncio.sleep(0.05)
        await msg.answer(f"✅ Доставлено: {ok} из {len(users)}")
        return

    if action == "answer":
        if not msg.text:
            await msg.answer("Ответ должен быть текстом.")
            return
        await state.clear()
        await msg.answer(await send_answer(bot, data["qid"], msg.from_user, msg.html_text))
        return

    if action == "text":
        if not msg.text:
            await msg.answer("Отправьте текст.")
            return
        key = data["key"]
        value = msg.text.strip() if texts.is_plain(key) else msg.html_text
        if key.startswith("btn_") and len(value) > 60:
            await msg.answer("Слишком длинная подпись кнопки (максимум 60 символов).")
            return
        await texts.set_text(key, value)
        await state.clear()
        await msg.answer("✅ Сохранено", reply_markup=main_kb())
        await send_screen(msg, f"a:tg:{DEFAULTS[key][0]}")
        return

    if action == "add":
        if not msg.text:
            await msg.answer("Отправьте текст.")
            return
        kind, parent, name = data["kind"], data["parent"], msg.text.strip()
        if kind == "city":
            new_id = await db.execute("INSERT INTO cities (name) VALUES (?)", name)
            back = f"a:city:{new_id}"
        elif kind == "shop":
            new_id = await db.execute("INSERT INTO shops (city_id, name) VALUES (?, ?)", parent, name)
            back = f"a:shop:{new_id}"
        elif kind == "cat":
            new_id = await db.execute("INSERT INTO categories (name) VALUES (?)", name)
            back = f"a:cat:{new_id}"
        elif kind == "prod":
            new_id = await db.execute("INSERT INTO products (category_id, name) VALUES (?, ?)", parent, name)
            back = f"a:prod:{new_id}"
        elif kind == "var":
            title, _, price_part = name.partition(";")
            value = parse_price(price_part)
            if not title.strip() or value is None:
                await msg.answer("Формат: <code>30 мл · 20 мг; 450</code>")
                return
            new_id = await db.execute(
                "INSERT INTO variants (product_id, title, price) VALUES (?, ?, ?)",
                parent, title.strip(), value,
            )
            back = f"a:var:{new_id}"
            await msg.answer("ℹ️ Отметьте, в каких магазинах вариант есть в наличии.")
        else:
            await state.clear()
            return
        await state.clear()
        await msg.answer("✅ Добавлено")
        await send_screen(msg, back)
        return

    if action == "set":
        table, row_id, field = data["table"], data["id"], data["field"]
        row = await db.fetch_one(f"SELECT * FROM {table} WHERE id=?", row_id)
        if not row:
            await state.clear()
            await msg.answer("Запись уже удалена.")
            return
        text = (msg.text or "").strip()

        if field == "location":
            if msg.location:
                lat, lon = msg.location.latitude, msg.location.longitude
            elif text == "-":
                lat = lon = None
            else:
                nums = re.findall(r"-?\d+(?:\.\d+)?", text)
                if len(nums) != 2:
                    await msg.answer("Отправьте геолокацию или координаты: <code>55.7558, 37.6173</code>")
                    return
                lat, lon = float(nums[0]), float(nums[1])
            await db.execute("UPDATE shops SET lat=?, lon=? WHERE id=?", lat, lon, row_id)
        elif field == "photo":
            if msg.photo:
                value = msg.photo[-1].file_id
            elif text == "-":
                value = None
            else:
                await msg.answer("Отправьте фото или «-».")
                return
            await db.execute("UPDATE products SET photo=? WHERE id=?", value, row_id)
        elif field == "price":
            value = parse_price(text)
            if value is None:
                await msg.answer("Отправьте цену числом, например: 450")
                return
            await db.execute("UPDATE variants SET price=? WHERE id=?", value, row_id)
        else:
            if not text:
                await msg.answer("Отправьте текст.")
                return
            if text == "-" and field in ("description", "phone"):
                text = ""
            await db.execute(f"UPDATE {table} SET {field}=? WHERE id=?", text, row_id)

        await state.clear()
        await msg.answer("✅ Сохранено")
        back = {
            "cities": f"a:city:{row_id}",
            "shops": f"a:shop:{row_id}",
            "categories": f"a:cat:{row_id}",
            "products": f"a:prod:{row_id}",
            "variants": f"a:var:{row_id}",
        }[table]
        await send_screen(msg, back)
        return

    await state.clear()


# Быстрый ответ: админ отвечает реплаем на сообщение бота с вопросом #Q123
@router.message(StateFilter(None), F.reply_to_message, F.text)
async def reply_shortcut(msg: Message, bot: Bot):
    src = msg.reply_to_message
    if not src.from_user or src.from_user.id != bot.id:
        return
    found = re.search(r"#Q(\d+)", src.text or src.caption or "")
    if not found:
        return
    await msg.answer(await send_answer(bot, int(found.group(1)), msg.from_user, msg.html_text))
