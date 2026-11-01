"""Ukrainian wording for the Telegram bot and the web panel (the CLI and logs stay English)."""

from __future__ import annotations

import re

_LABELS = {"top 10": "топ-10", "dev": "розробник", "snipers": "снайпери", "insiders": "інсайдери", "bundlers": "бандлери"}

_REASONS: list[tuple[str, str]] = [
    (r"^dip (\d+%) off high, \+(\d+%) off low$", r"просадка \1 від максимуму, відскок +\2"),
    (r"^snipe$", "снайп (одразу за бандлом)"),
    (r"^bundle selling \((\d+%) of its bag since entry\)$", r"бандл продає (\1 своїх токенів після нашого входу)"),
    (r"^insiders/bundlers dumping \(holdings -(\d+%) since entry\)$", r"інсайдери й бандлери скидають (−\1 від нашого входу)"),
    (r"^take profit$", "тейк-профіт"),
    (r"^stop loss$", "стоп-лос"),
    (r"^trailing stop$", "трейлінг-стоп"),
    (r"^max hold time$", "максимальний час утримання"),
    (r"^end of data$", "кінець даних"),
    (r"^LARP score (\d+) > (\d+)$", r"LARP-оцінка \1 > \2"),
    (r"^bundle already sold (\d+%)$", r"бандл уже продав \1"),
    (r"^bundle holds (\d+%) of supply$", r"бандл тримає \1 пропозиції"),
    (r"^token check failed: (.*)$", r"перевірка токена не пройдена: \1"),
    (r"^holder panel failed: (.*)$", r"панель холдерів не вдалася: \1"),
    (r"^entry window expired$", "час на вхід минув"),
    (r"^buy failed$", "купівля не вдалася"),
]


def ua_reason(text: str | None) -> str:
    """Translate a strategy reason ('dip 28% off high, +6% off low', 'stop loss', ...)."""
    if not text:
        return ""
    if text.startswith("closed: "):
        return "закрито: " + ua_reason(text[len("closed: "):])
    m = re.match(r"^(top 10|dev|snipers|insiders|bundlers) hold (\d+%) > (\d+%)$", text)
    if m:
        return f"{_LABELS[m.group(1)]} тримають {m.group(2)} > {m.group(3)}"
    for pattern, repl in _REASONS:
        if re.match(pattern, text):
            return re.sub(pattern, repl, text)
    return text


def verdict_ua(score: int) -> str:
    if score >= 60:
        return "високий ризик: схоже на LARP або підготовку рагу"
    if score >= 30:
        return "підозріло"
    return "серйозних червоних прапорців немає"
