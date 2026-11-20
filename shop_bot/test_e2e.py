"""Сквозной тест без Telegram: python test_e2e.py"""
import asyncio, datetime, itertools, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["BOT_TOKEN"] = "123456:TEST"
os.environ["ADMIN_IDS"] = "1"
os.environ["DB_PATH"] = ":memory:"

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import Update, Message, Chat, User, CallbackQuery, Location

import admin, config, db, support, texts, user
from middlewares import AccessMiddleware

ids = itertools.count(1000)
SENT = []

class FakeSession(BaseSession):
    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        payload = method.model_dump(exclude_none=True)
        SENT.append((name, payload))
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)) or name.startswith(("Set", "Delete")):
            return True
        chat_id = payload.get("chat_id", 0)
        return Message(message_id=next(ids), date=datetime.datetime.now(),
                       chat=Chat(id=chat_id, type="private"),
                       from_user=User(id=123456, is_bot=True, first_name="bot"),
                       text=payload.get("text") or payload.get("caption"))
    async def close(self): pass
    async def stream_content(self, *a, **k): yield b""

ADMIN = User(id=1, is_bot=False, first_name="Admin")
CLIENT = User(id=2, is_bot=False, first_name="Ivan", username="ivan")

def msg(u, text=None, **kw):
    return Message(message_id=next(ids), date=datetime.datetime.now(), chat=Chat(id=u.id, type="private"), from_user=u, text=text, **kw)

async def send(dp, bot, u, text=None, **kw):
    SENT.clear()
    await dp.feed_update(bot, Update(update_id=next(ids), message=msg(u, text, **kw)))
    return SENT[:]

async def click(dp, bot, u, data, photo=False):
    SENT.clear()
    m = msg(User(id=123456, is_bot=True, first_name="bot"), "screen")
    m = m.model_copy(update={"chat": Chat(id=u.id, type="private")})
    await dp.feed_update(bot, Update(update_id=next(ids), callback_query=CallbackQuery(id=str(next(ids)), from_user=u, chat_instance="x", data=data, message=m)))
    return SENT[:]

def texts_of(sent):
    return [p.get("text") or p.get("caption") or "" for n, p in sent if n in ("SendMessage", "EditMessageText", "SendPhoto")]

def buttons(sent):
    out = []
    for n, p in sent:
        kb = p.get("reply_markup") or {}
        for row in kb.get("inline_keyboard", []):
            for b in row: out.append((b["text"], b.get("callback_data")))
    return out

def btn(sent, label_part):
    for t, d in buttons(sent):
        if label_part in t: return d
    raise AssertionError(f"no button {label_part!r} in {buttons(sent)}")

async def main():
    await db.connect(":memory:"); await texts.load()
    bot = Bot("123456:TEST", session=FakeSession(), default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.outer_middleware(AccessMiddleware()); dp.callback_query.outer_middleware(AccessMiddleware())
    dp.include_routers(user.router, support.router, admin.router)
    A = lambda d: click(dp, bot, ADMIN, d)

    # --- админ наполняет каталог ---
    await send(dp, bot, ADMIN, "/admin")
    await A("a:add:city:0"); r = await send(dp, bot, ADMIN, "Москва")
    city = btn(r, "Добавить магазин")
    await A(city); r = await send(dp, bot, ADMIN, "Вейп-Шоп на Арбате")
    sid = int(btn(r, "Адрес").split(":")[3])
    await A(f"a:set:shops:{sid}:address"); await send(dp, bot, ADMIN, "ул. Арбат, 10")
    await A(f"a:set:shops:{sid}:hours"); await send(dp, bot, ADMIN, "10:00–22:00")
    await A(f"a:set:shops:{sid}:location"); r = await send(dp, bot, ADMIN, "55.7520, 37.5925")
    assert "55.75200" in texts_of(r)[-1], texts_of(r)
    await A("a:add:cat:0"); r = await send(dp, bot, ADMIN, "Жидкости")
    cat_add = btn(r, "Добавить товар")
    await A(cat_add); r = await send(dp, bot, ADMIN, "Mango Ice")
    var_add = btn(r, "Добавить вариант")
    pid = int(var_add.split(":")[3])
    await A(f"a:set:products:{pid}:description"); await send(dp, bot, ADMIN, "Манго со льдом")
    await A(var_add); r = await send(dp, bot, ADMIN, "30 мл · 20 мг; 450")
    toggle = btn(r, "Вейп-Шоп")
    r = await A(toggle); assert any(t.startswith("✅") for t, _ in buttons(r))
    await A(var_add); r = await send(dp, bot, ADMIN, "bad")
    assert "Формат" in texts_of(r)[0]
    await send(dp, bot, ADMIN, "60 мл · 3 мг; 1 200 ₽")
    # переименование кнопки меню
    r = await A("a:t:btn_catalog"); assert "Сейчас" in texts_of(r)[0]
    r = await send(dp, bot, ADMIN, "🛒 Ассортимент")
    assert texts.T("btn_catalog") == "🛒 Ассортимент"
    # переименование сообщения с HTML
    await A("a:t:msg_promo"); await send(dp, bot, ADMIN, "Скидка 10%", entities=[{"type":"bold","offset":0,"length":6}])
    assert texts.T("msg_promo") == "<b>Скидка</b> 10%", texts.T("msg_promo")

    # --- пользователь ---
    r = await send(dp, bot, CLIENT, "/start"); assert "18" in texts_of(r)[0]
    r = await send(dp, bot, CLIENT, "🛒 Ассортимент"); assert "18" in texts_of(r)[0]  # без 18+ не пускает
    await click(dp, bot, CLIENT, "u:age:yes")
    r = await send(dp, bot, CLIENT, "🛍 Каталог")  # старая подпись тоже работает
    assert any(n == "SendMessage" and "keyboard" in (p.get("reply_markup") or {}) for n, p in r), "клавиатура обновлена"
    t = texts_of(r)[-1]; assert "Выберите магазин" in t, t
    r = await click(dp, bot, CLIENT, btn(r, "Вейп-Шоп"))
    assert "Арбат" in texts_of(r)[0]
    r = await click(dp, bot, CLIENT, btn(r, "Жидкости"))
    r = await click(dp, bot, CLIENT, btn(r, "Mango"))
    card = texts_of(r)[0]; print("\n--- карточка товара ---\n" + card)
    assert "450 ₽" in card and "1 200 ₽" in card and "✅ в наличии" in card and "❌ нет в наличии" in card
    r = await send(dp, bot, CLIENT, "🔥 Акции"); assert "<b>Скидка</b>" in texts_of(r)[0]
    r = await click(dp, bot, CLIENT, "s:cities"); r = await click(dp, bot, CLIENT, btn(r, "Вейп-Шоп"))
    r = await click(dp, bot, CLIENT, btn(r, "карте")); assert r[0][0] == "SendVenue"

    # --- вопрос и ответ ---
    await click(dp, bot, CLIENT, f"u:ask:{pid}")
    r = await send(dp, bot, CLIENT, "Есть ли 50 мг?")
    assert "15 минут" in texts_of(r)[0]
    admin_msg = [p for n, p in r if n == "SendMessage" and p["chat_id"] == 1][0]
    print("\n--- админу пришло ---\n" + admin_msg["text"])
    qid = int(admin_msg["text"].split("#Q")[1].split("<")[0])
    # напоминание через 15 минут
    await db.execute("UPDATE questions SET created_at=? WHERE id=?", time.time() - 16 * 60, qid)
    SENT.clear()
    task = asyncio.create_task(support.reminder_loop(bot)); await asyncio.sleep(0.2); task.cancel()
    assert any("Нет ответа больше 15" in p.get("text", "") for n, p in SENT), SENT
    # повторно сразу не напоминает
    SENT.clear(); task = asyncio.create_task(support.reminder_loop(bot)); await asyncio.sleep(0.2); task.cancel()
    assert not SENT
    # ответ через кнопку
    await A(f"a:ans:{qid}")
    r = await send(dp, bot, ADMIN, "Да, будет завтра")
    to_client = [p for n, p in r if n == "SendMessage" and p["chat_id"] == 2][0]
    print("\n--- клиенту пришло ---\n" + to_client["text"])
    assert "Да, будет завтра" in to_client["text"]
    st = await db.fetch_one("SELECT status FROM questions WHERE id=?", qid); assert st["status"] == "answered"
    # ответ реплаем
    await click(dp, bot, CLIENT, "u:askagain"); r = await send(dp, bot, CLIENT, "Спасибо, а 100 мл?")
    admin_msg = [p for n, p in r if p.get("chat_id") == 1][0]
    reply_to = msg(User(id=123456, is_bot=True, first_name="bot"), admin_msg["text"])
    r = await send(dp, bot, ADMIN, "Нет, только 30 и 60", reply_to_message=reply_to)
    assert any(p.get("chat_id") == 2 and "только 30" in p.get("text","") for n, p in r)

    # --- блокировка ---
    await A("a:ban:2"); r = await send(dp, bot, CLIENT, "🛒 Ассортимент"); assert r == []
    await A("a:ban:2")
    # --- удаление каскадом ---
    await A(f"a:delok:categories:{cat_add.split(':')[3]}")
    assert (await db.fetch_one("SELECT COUNT(*) n FROM variants"))["n"] == 0
    r = await A("a:stats"); print("\n" + texts_of(r)[0])
    r = await A("a:menu"); r = await A("a:tg:menu"); assert any("Ассортимент" in t for t, _ in buttons(r))
    print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ"); await db.close()

asyncio.run(main())
