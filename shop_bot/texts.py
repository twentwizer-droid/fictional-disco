"""Все тексты и подписи кнопок бота. Любое значение админ меняет прямо в боте."""

import db

GROUPS = {
    "menu": "🏠 Кнопки главного меню",
    "nav": "🔘 Кнопки навигации",
    "msg": "💬 Сообщения",
    "misc": "⚙️ Прочее",
}

# key: (группа, что это, значение по умолчанию)
DEFAULTS: dict[str, tuple[str, str, str]] = {
    # --- главное меню ---
    "btn_catalog": ("menu", "Каталог", "🛍 Каталог"),
    "btn_shops": ("menu", "Магазины", "🏬 Магазины"),
    "btn_promo": ("menu", "Акции", "🔥 Акции"),
    "btn_ask": ("menu", "Задать вопрос", "💬 Задать вопрос"),
    "btn_about": ("menu", "О нас", "ℹ️ О нас"),
    # --- навигация ---
    "btn_back": ("nav", "Назад", "⬅️ Назад"),
    "btn_cancel": ("nav", "Отмена", "✖️ Отмена"),
    "btn_age_yes": ("nav", "Подтвердить 18+", "✅ Мне есть 18 лет"),
    "btn_age_no": ("nav", "Нет 18", "❌ Мне нет 18"),
    "btn_change_shop": ("nav", "Сменить магазин", "🔄 Сменить магазин"),
    "btn_ask_product": ("nav", "Спросить о товаре", "❓ Спросить о товаре"),
    "btn_map": ("nav", "Показать на карте", "📍 Показать на карте"),
    "btn_shop_catalog": ("nav", "Каталог этого магазина", "🛍 Ассортимент магазина"),
    "btn_write_more": ("nav", "Написать ещё", "💬 Написать ещё"),
    # --- сообщения ---
    "msg_age": (
        "msg",
        "Проверка возраста",
        "🔞 Бот содержит информацию о никотиносодержащей продукции.\n\n"
        "Подтвердите, что вам исполнилось 18 лет.",
    ),
    "msg_age_denied": (
        "msg",
        "Отказ (нет 18)",
        "Извините, информация доступна только лицам старше 18 лет.",
    ),
    "msg_welcome": (
        "msg",
        "Приветствие",
        "👋 Добро пожаловать!\n\n"
        "Здесь вы можете посмотреть ассортимент, цены и наличие в наших магазинах.",
    ),
    "msg_choose_city": ("msg", "Выбор города", "🏙 Выберите город:"),
    "msg_choose_shop": ("msg", "Выбор магазина", "🏬 Выберите магазин:"),
    "msg_choose_category": ("msg", "Выбор категории", "📂 Выберите категорию:"),
    "msg_choose_product": ("msg", "Выбор товара", "📦 Выберите товар:"),
    "msg_empty": ("msg", "Пусто", "Здесь пока ничего нет."),
    "msg_variants_title": ("msg", "Заголовок вариантов", "Варианты и цены:"),
    "msg_in_stock": ("msg", "Есть в наличии", "✅ в наличии"),
    "msg_out_stock": ("msg", "Нет в наличии", "❌ нет в наличии"),
    "msg_product_footer": (
        "msg",
        "Подпись под товаром",
        "🛒 Купить можно в магазине. Продажа только лицам старше 18 лет "
        "при предъявлении документа, удостоверяющего личность.",
    ),
    "msg_promo": ("msg", "Текст акций", "🔥 Актуальные акции скоро появятся здесь."),
    "msg_about": (
        "msg",
        "О нас",
        "ℹ️ Этот бот — витрина наших магазинов: ассортимент, цены и наличие.\n"
        "Продажа осуществляется только в магазинах.",
    ),
    "msg_ask_prompt": (
        "msg",
        "Просьба написать вопрос",
        "✍️ Напишите ваш вопрос одним сообщением:",
    ),
    "msg_ask_sent": (
        "msg",
        "Вопрос отправлен",
        "✅ Вопрос отправлен! Продавец ответит здесь, в боте, в течение 15 минут.",
    ),
    "msg_answer_prefix": ("msg", "Заголовок ответа", "💬 Ответ магазина:"),
    "msg_cancelled": ("msg", "Отменено", "Отменено."),
    # --- прочее ---
    "currency": ("misc", "Валюта", "₽"),
}

_cache: dict[str, str] = {}
_aliases: dict[str, str] = {}


async def load() -> None:
    for key, (_, _, value) in DEFAULTS.items():
        await db.execute(
            "INSERT OR IGNORE INTO texts (key, value) VALUES (?, ?)", key, value
        )
    _cache.clear()
    for row in await db.fetch_all("SELECT key, value FROM texts"):
        _cache[row["key"]] = row["value"]
    _aliases.clear()
    for row in await db.fetch_all("SELECT label, key FROM aliases"):
        _aliases[row["label"]] = row["key"]


def T(key: str) -> str:
    return _cache.get(key) or DEFAULTS[key][2]


def is_plain(key: str) -> bool:
    """Кнопки и валюта хранятся простым текстом (без HTML-разметки)."""
    return key.startswith("btn_") or key == "currency"


async def set_text(key: str, value: str) -> None:
    old = T(key)
    if key.startswith("btn_") and old != value:
        await db.execute(
            "INSERT OR REPLACE INTO aliases (label, key) VALUES (?, ?)", old, key
        )
        _aliases[old] = key
    await db.execute("UPDATE texts SET value = ? WHERE key = ?", value, key)
    _cache[key] = value


async def reset(key: str) -> None:
    await set_text(key, DEFAULTS[key][2])


def resolve_button(label: str | None) -> str | None:
    """По подписи кнопки (текущей или старой) возвращает её ключ."""
    if not label:
        return None
    for key, value in _cache.items():
        if key.startswith("btn_") and value == label:
            return key
    return _aliases.get(label)
