"""Вопросы покупателей, ответы админов и напоминания о неотвеченных вопросах."""

import asyncio
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import config
import db
from texts import T
from ui import MenuButton, e, ikb

router = Router()
log = logging.getLogger(__name__)


class Ask(StatesGroup):
    text = State()


def question_kb(qid: int, uid: int):
    return ikb(
        [
            [("✍️ Ответить", f"a:ans:{qid}"), ("✔️ Закрыть", f"a:qclose:{qid}")],
            [("🚫 Блок / разблок пользователя", f"a:ban:{uid}")],
        ]
    )


async def question_text(qid: int) -> str:
    q = await db.fetch_one(
        "SELECT q.*, u.username, u.full_name, p.name AS product FROM questions q "
        "LEFT JOIN users u ON u.id = q.user_id "
        "LEFT JOIN products p ON p.id = q.product_id WHERE q.id = ?",
        qid,
    )
    if not q:
        return "Вопрос не найден."
    who = e(q["full_name"] or "без имени")
    if q["username"]:
        who += f" (@{e(q['username'])})"
    status = {"open": "🟡 ждёт ответа", "answered": "🟢 отвечен", "closed": "⚪️ закрыт"}
    minutes = int((time.time() - q["created_at"]) // 60)
    lines = [
        f"❓ <b>Вопрос #Q{q['id']}</b> — {status.get(q['status'], q['status'])}",
        f"👤 {who}, id <code>{q['user_id']}</code>",
        f"⏱ {minutes} мин. назад",
    ]
    if q["product"]:
        lines.append(f"📦 Товар: {e(q['product'])}")
    lines += ["", e(q["text"])]
    if q["answer"]:
        lines += ["", f"<b>Ответ:</b> {q['answer']}"]
    lines += ["", "<i>Чтобы ответить, нажмите «Ответить» или ответьте реплаем на это сообщение.</i>"]
    return "\n".join(lines)


async def notify_admins(bot: Bot, text: str, kb=None) -> None:
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=kb)
        except TelegramAPIError as err:
            log.warning("Не удалось написать админу %s: %s", admin_id, err)


async def send_answer(bot: Bot, qid: int, admin, answer_html: str) -> str:
    """Отправить ответ пользователю. Возвращает текст результата для админа."""
    q = await db.fetch_one("SELECT * FROM questions WHERE id = ?", qid)
    if not q:
        return "❌ Вопрос не найден."
    try:
        await bot.send_message(
            q["user_id"],
            f"{T('msg_answer_prefix')}\n\n{answer_html}",
            reply_markup=ikb([[(T("btn_write_more"), "u:askagain")]]),
        )
    except TelegramAPIError as err:
        return f"❌ Не удалось доставить ответ (пользователь заблокировал бота?): {e(err)}"
    await db.execute(
        "UPDATE questions SET status = 'answered', answered_at = ?, answered_by = ?, "
        "answer = ? WHERE id = ?",
        time.time(),
        admin.id,
        answer_html,
        qid,
    )
    for admin_id in config.ADMIN_IDS - {admin.id}:
        try:
            await bot.send_message(
                admin_id, f"ℹ️ На вопрос #Q{qid} ответил {e(admin.full_name)}:\n{answer_html}"
            )
        except TelegramAPIError:
            pass
    return f"✅ Ответ на #Q{qid} отправлен."


async def start_ask(target: Message, state: FSMContext, product_id: int | None) -> None:
    await state.set_state(Ask.text)
    await state.update_data(product_id=product_id)
    await target.answer(
        T("msg_ask_prompt"), reply_markup=ikb([[(T("btn_cancel"), "u:cancel")]])
    )


# ---------- пользователь ----------


@router.message(MenuButton("btn_ask"))
async def menu_ask(msg: Message, state: FSMContext):
    await start_ask(msg, state, None)


@router.callback_query(F.data.startswith("u:ask:"))
async def ask_about_product(call: CallbackQuery, state: FSMContext):
    await start_ask(call.message, state, int(call.data.split(":")[2]))
    await call.answer()


@router.callback_query(F.data == "u:askagain")
async def ask_again(call: CallbackQuery, state: FSMContext):
    await start_ask(call.message, state, None)
    await call.answer()


@router.callback_query(F.data == "u:cancel")
async def ask_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(T("msg_cancelled"))
    await call.answer()


@router.message(Ask.text)
async def ask_receive(msg: Message, state: FSMContext, bot: Bot):
    if not msg.text:
        await msg.answer("Пожалуйста, отправьте вопрос текстом.")
        return
    data = await state.get_data()
    await state.clear()
    await db.upsert_user(msg.from_user)
    qid = await db.execute(
        "INSERT INTO questions (user_id, product_id, text, created_at) VALUES (?, ?, ?, ?)",
        msg.from_user.id,
        data.get("product_id"),
        msg.text,
        time.time(),
    )
    await msg.answer(T("msg_ask_sent"))
    await notify_admins(
        bot, "🆕 " + await question_text(qid), question_kb(qid, msg.from_user.id)
    )


# ---------- напоминания ----------


async def reminder_loop(bot: Bot) -> None:
    period = config.REMIND_MINUTES * 60
    while True:
        try:
            now = time.time()
            rows = await db.fetch_all(
                "SELECT id, user_id FROM questions WHERE status = 'open' AND "
                "COALESCE(last_reminded, created_at) <= ?",
                now - period,
            )
            for q in rows:
                await db.execute(
                    "UPDATE questions SET last_reminded = ? WHERE id = ?", now, q["id"]
                )
                await notify_admins(
                    bot,
                    "⏰ <b>Нет ответа больше "
                    f"{config.REMIND_MINUTES} мин!</b>\n\n" + await question_text(q["id"]),
                    question_kb(q["id"], q["user_id"]),
                )
        except Exception:  # цикл не должен падать
            log.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(30)
