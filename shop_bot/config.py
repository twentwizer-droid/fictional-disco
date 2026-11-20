import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x
}
DB_PATH = os.getenv("DB_PATH", "shop.db")
# Через сколько минут без ответа на вопрос бот напоминает админам
REMIND_MINUTES = int(os.getenv("REMIND_MINUTES", "15"))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS
