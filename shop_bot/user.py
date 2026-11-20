"""Пользовательская часть: проверка 18+, каталог, магазины, акции."""

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
from texts import T
from ui import MenuButton, age_kb, e, ikb, main_kb, price, show

router = Router()


# ---------- служебное ----------


async def refresh_menu_if_old(msg: Message) -> None:
    """Если пользователь нажал кнопку со старой подписью — обновим ему клавиатуру."""
    labels = {T(k) for k in ("btn_catalog", "btn_shops", "btn_promo", "btn_ask", "btn_about")}
    if msg.text not in labels:
        await msg.answer("🔄", reply_markup=main_kb())


async def user_shop(uid: int):
    return await db.fetch_one(
        "SELECT s.*, c.name AS city FROM users u "
        "JOIN shops s ON s.id = u.shop_id JOIN cities c ON c.id = s.city_id "
        "WHERE u.id = ?",
        uid,
    )


# ---------- экраны ----------


async def screen(data: str, uid: int) -> tuple[str, object, str | None]:
    """Возвращает (текст, клавиатура, фото) для callback-данных пользователя."""
    parts = data.split(":")
    mode, cmd = parts[0], parts[1]  # mode: u — каталог, s — информация о магазинах
    arg = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None

    if cmd == "cities":
        cities = await db.fetch_all("SELECT * FROM cities ORDER BY name")
        if not cities:
            return T("msg_empty"), None, None
        if len(cities) == 1:
            return await screen(f"{mode}:city:{cities[0]['id']}", uid)
        rows = [[(c["name"], f"{mode}:city:{c['id']}")] for c in cities]
        return T("msg_choose_city"), ikb(rows), None

    if cmd == "city":
        city = await db.fetch_one("SELECT * FROM cities WHERE id = ?", arg)
        if not city:
            return await screen(f"{mode}:cities", uid)
        shops = await db.fetch_all(
            "SELECT * FROM shops WHERE city_id = ? ORDER BY name", arg
        )
        rows = [
            [(f"{s['name']}" + (f" — {s['address']}" if s["address"] else ""), f"{mode}:shop:{s['id']}")]
            for s in shops
        ]
        many = (await db.fetch_one("SELECT COUNT(*) AS n FROM cities"))["n"] > 1
        if many:
            rows.append([(T("btn_back"), f"{mode}:cities")])
        text = f"🏙 <b>{e(city['name'])}</b>\n\n" + (
            T("msg_choose_shop") if shops else T("msg_empty")
        )
        return text, ikb(rows), None

    if mode == "s" and cmd == "shop":
        s = await db.fetch_one(
            "SELECT s.*, c.name AS city FROM shops s JOIN cities c ON c.id = s.city_id "
            "WHERE s.id = ?",
            arg,
        )
        if not s:
            return await screen("s:cities", uid)
        lines = [f"🏬 <b>{e(s['name'])}</b>", f"🏙 {e(s['city'])}"]
        if s["address"]:
            lines.append(f"📍 {e(s['address'])}")
        if s["hours"]:
            lines.append(f"🕘 {e(s['hours'])}")
        if s["phone"]:
            lines.append(f"📞 {e(s['phone'])}")
        rows = []
        if s["lat"] is not None:
            rows.append([(T("btn_map"), f"s:map:{s['id']}")])
        rows.append([(T("btn_shop_catalog"), f"u:shop:{s['id']}")])
        rows.append([(T("btn_back"), f"s:city:{s['city_id']}")])
        return "\n".join(lines), ikb(rows), None

    if cmd == "shop":  # выбор магазина для каталога
        if await db.fetch_one("SELECT id FROM shops WHERE id = ?", arg):
            await db.execute("UPDATE users SET shop_id = ? WHERE id = ?", arg, uid)
        return await screen("u:cats", uid)

    if cmd == "cats":
        shop = await user_shop(uid)
        if not shop:
            return await screen("u:cities", uid)
        cats = await db.fetch_all("SELECT * FROM categories ORDER BY name")
        head = f"🏬 <b>{e(shop['name'])}</b>, {e(shop['city'])}"
        if shop["address"]:
            head += f"\n📍 {e(shop['address'])}"
        rows = [[(c["name"], f"u:cat:{c['id']}")] for c in cats]
        rows.append([(T("btn_change_shop"), "u:cities")])
        body = T("msg_choose_category") if cats else T("msg_empty")
        return f"{head}\n\n{body}", ikb(rows), None

    if cmd == "cat":
        shop = await user_shop(uid)
        if not shop:
            return await screen("u:cities", uid)
        cat = await db.fetch_one("SELECT * FROM categories WHERE id = ?", arg)
        if not cat:
            return await screen("u:cats", uid)
        prods = await db.fetch_all(
            "SELECT p.id, p.name, EXISTS(SELECT 1 FROM variants v JOIN stock st "
            "ON st.variant_id = v.id WHERE v.product_id = p.id AND st.shop_id = ?) AS avail "
            "FROM products p WHERE p.category_id = ? ORDER BY p.name",
            shop["id"],
            arg,
        )
        rows = [
            [(("✅ " if p["avail"] else "▫️ ") + p["name"], f"u:prod:{p['id']}")]
            for p in prods
        ]
        rows.append([(T("btn_back"), "u:cats")])
        body = T("msg_choose_product") if prods else T("msg_empty")
        return f"📂 <b>{e(cat['name'])}</b>\n\n{body}", ikb(rows), None

    if cmd == "prod":
        shop = await user_shop(uid)
        p = await db.fetch_one("SELECT * FROM products WHERE id = ?", arg)
        if not p or not shop:
            return await screen("u:cats", uid)
        variants = await db.fetch_all(
            "SELECT v.*, EXISTS(SELECT 1 FROM stock st WHERE st.variant_id = v.id "
            "AND st.shop_id = ?) AS avail FROM variants v WHERE v.product_id = ? "
            "ORDER BY v.price, v.title",
            shop["id"],
            arg,
        )
        lines = [f"<b>{e(p['name'])}</b>"]
        if p["description"]:
            lines += ["", e(p["description"])]
        if variants:
            lines += ["", f"{T('msg_variants_title')} <i>({e(shop['name'])})</i>"]
            for v in variants:
                status = T("msg_in_stock") if v["avail"] else T("msg_out_stock")
                lines.append(f"• {e(v['title'])} — <b>{price(v['price'])}</b> — {status}")
        lines += ["", T("msg_product_footer")]
        rows = [
            [(T("btn_ask_product"), f"u:ask:{p['id']}")],
            [(T("btn_back"), f"u:cat:{p['category_id']}")],
        ]
        return "\n".join(lines), ikb(rows), p["photo"]

    return T("msg_empty"), None, None


# ---------- /start и возраст ----------


@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await db.upsert_user(msg.from_user)
    user = await db.fetch_one("SELECT age_ok FROM users WHERE id = ?", msg.from_user.id)
    if not user["age_ok"] and not config.is_admin(msg.from_user.id):
        await msg.answer(T("msg_age"), reply_markup=age_kb())
        return
    text = T("msg_welcome")
    if config.is_admin(msg.from_user.id):
        text += "\n\n⚙️ Админ-панель: /admin"
    await msg.answer(text, reply_markup=main_kb())


@router.callback_query(F.data == "u:age:yes")
async def age_yes(call: CallbackQuery):
    await db.execute("UPDATE users SET age_ok = 1 WHERE id = ?", call.from_user.id)
    await call.message.edit_text("✅")
    await call.message.answer(T("msg_welcome"), reply_markup=main_kb())
    await call.answer()


@router.callback_query(F.data == "u:age:no")
async def age_no(call: CallbackQuery):
    await call.message.edit_text(T("msg_age_denied"))
    await call.answer()


# ---------- главное меню ----------


@router.message(MenuButton("btn_catalog"))
async def menu_catalog(msg: Message, state: FSMContext):
    await state.clear()
    await refresh_menu_if_old(msg)
    text, kb, photo = await screen("u:cats", msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


@router.message(MenuButton("btn_shops"))
async def menu_shops(msg: Message, state: FSMContext):
    await state.clear()
    await refresh_menu_if_old(msg)
    text, kb, _ = await screen("s:cities", msg.from_user.id)
    await msg.answer(text, reply_markup=kb)


@router.message(MenuButton("btn_promo"))
async def menu_promo(msg: Message, state: FSMContext):
    await state.clear()
    await refresh_menu_if_old(msg)
    await msg.answer(T("msg_promo"))


@router.message(MenuButton("btn_about"))
async def menu_about(msg: Message, state: FSMContext):
    await state.clear()
    await refresh_menu_if_old(msg)
    await msg.answer(T("msg_about"))


# ---------- навигация по inline-кнопкам ----------


@router.callback_query(F.data.startswith("s:map:"))
async def shop_map(call: CallbackQuery):
    s = await db.fetch_one("SELECT * FROM shops WHERE id = ?", int(call.data.split(":")[2]))
    if s and s["lat"] is not None:
        await call.message.answer_venue(
            latitude=s["lat"],
            longitude=s["lon"],
            title=s["name"],
            address=s["address"] or s["name"],
        )
    await call.answer()


@router.callback_query(
    F.data.regexp(r"^[us]:(cities|city|shop|cats|cat|prod)(:\d+)?$")
)
async def navigate(call: CallbackQuery):
    text, kb, photo = await screen(call.data, call.from_user.id)
    await show(call, text, kb, photo)
    await call.answer()
