"""Клавиатуры, фильтры и вспомогательные функции."""

from html import escape

from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Filter
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

import config
from texts import T, resolve_button


def e(value) -> str:
    return escape(str(value or ""))


def ikb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


def main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=T("btn_catalog")), KeyboardButton(text=T("btn_shops"))],
            [KeyboardButton(text=T("btn_promo")), KeyboardButton(text=T("btn_ask"))],
            [KeyboardButton(text=T("btn_about"))],
        ],
        resize_keyboard=True,
    )


def age_kb() -> InlineKeyboardMarkup:
    return ikb([[(T("btn_age_yes"), "u:age:yes")], [(T("btn_age_no"), "u:age:no")]])


def price(value: int) -> str:
    return f"{value:,}".replace(",", " ") + f" {T('currency')}"


class MenuButton(Filter):
    """Срабатывает на кнопку главного меню — по текущей или старой подписи."""

    def __init__(self, key: str):
        self.key = key

    async def __call__(self, message: Message) -> bool:
        return resolve_button(message.text) == self.key


class IsAdmin(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and config.is_admin(event.from_user.id)


async def show(
    call: CallbackQuery,
    text: str,
    kb: InlineKeyboardMarkup | None,
    photo: str | None = None,
) -> None:
    """Показать экран вместо текущего сообщения (с учётом фото)."""
    msg = call.message
    if photo:
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        if len(text) <= 1000:
            await msg.answer_photo(photo, caption=text, reply_markup=kb)
        else:
            await msg.answer_photo(photo)
            await msg.answer(text, reply_markup=kb)
        return
    if msg.photo:
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass
        await msg.answer(text, reply_markup=kb)
        return
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as err:
        if "not modified" not in str(err):
            await msg.answer(text, reply_markup=kb)
