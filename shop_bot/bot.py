import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

import admin
import config
import db
import support
import texts
import user
from middlewares import AccessMiddleware


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if not config.BOT_TOKEN:
        sys.exit("Укажите BOT_TOKEN в файле .env")
    if not config.ADMIN_IDS:
        logging.warning("ADMIN_IDS пуст — админ-панель никому не доступна")

    await db.connect(config.DB_PATH)
    await texts.load()

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessMiddleware())
    dp.callback_query.outer_middleware(AccessMiddleware())
    # порядок важен: кнопки меню → вопросы → админка
    dp.include_routers(user.router, support.router, admin.router)

    await bot.set_my_commands(
        [BotCommand(command="start", description="Главное меню")],
        scope=BotCommandScopeDefault(),
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Главное меню"),
                    BotCommand(command="admin", description="Админ-панель"),
                    BotCommand(command="cancel", description="Отменить действие"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logging.warning("Админ %s ещё не писал боту — команды не установлены", admin_id)

    reminder = asyncio.create_task(support.reminder_loop(bot))
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        reminder.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
