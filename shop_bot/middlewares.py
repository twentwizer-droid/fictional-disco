"""Доступ: блокировка пользователей и обязательное подтверждение 18+."""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

import config
import db
from texts import T
from ui import age_kb


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if not user or config.is_admin(user.id):
            return await handler(event, data)

        row = await db.fetch_one("SELECT age_ok, blocked FROM users WHERE id = ?", user.id)
        if row is None:
            await db.upsert_user(user)
            row = await db.fetch_one("SELECT age_ok, blocked FROM users WHERE id = ?", user.id)

        if row["blocked"]:
            if isinstance(event, CallbackQuery):
                await event.answer()
            return None

        if not row["age_ok"]:
            allowed = (
                isinstance(event, Message) and (event.text or "").startswith("/start")
            ) or (isinstance(event, CallbackQuery) and (event.data or "").startswith("u:age:"))
            if not allowed:
                if isinstance(event, CallbackQuery):
                    await event.answer()
                    await event.message.answer(T("msg_age"), reply_markup=age_kb())
                elif isinstance(event, Message):
                    await event.answer(T("msg_age"), reply_markup=age_kb())
                return None

        return await handler(event, data)
