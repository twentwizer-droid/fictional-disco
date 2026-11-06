import asyncio
import base64
import json

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from factory import add_launch
from solders.keypair import Keypair

from bundlehunter.config import Config
from bundlehunter.holders import Group, TokenPanel
from bundlehunter.larp import Finding, LarpReport, TokenMeta
from bundlehunter.models import MarketEvent
from bundlehunter.narrative import Member
from bundlehunter.service import CheckResult, Service
from bundlehunter.storage import Store
from bundlehunter.telegram import HELP, TelegramBot, fmt_check, fmt_positions, fmt_status, split_message
from bundlehunter.web import check_bind, create_app

T0 = 1_700_000_000


def _addr() -> str:
    return str(Keypair().pubkey())


def _panel(mint: str, creator: str) -> TokenPanel:
    groups = {g: Group(held=1e8, initial=2e8) for g in ("dev", "bundlers", "snipers", "insiders", "linked")}
    return TokenPanel(mint, 1e9, creator, None, groups, {}, [("w", 1e8)], "rpc", symbol="CAT")


class FakeScanner:
    def __init__(self):
        self.calls = 0

    async def panel(self, mint, w2c, **kw):
        self.calls += 1
        assert kw["profile"] is False and kw["final"] is False  # quick path, full analysis later
        return _panel(mint, "dev")


class FakeLarp:
    async def check(self, mint, panel=None, cluster=None, live=True, ai=False):
        return LarpReport(mint, TokenMeta(symbol="<b>CAT</b>", name="Cat & Co"), [Finding("red", "copied_twitter", "copied <socials>")])

    async def close(self):
        pass


def _service() -> Service:
    store = Store(":memory:")
    for i in range(3):
        add_launch(store, f"X{i}", "dev", ["b1", "b2"], T0 + i * 100, i * 1000, 1.0, 3.0)
    svc = Service(Config(), store, rpc=None)
    svc.scanner, svc.larp = FakeScanner(), FakeLarp()
    return svc


# -- service -------------------------------------------------------------------------------


def test_service_check_caches_and_links_dev_cluster():
    svc = _service()
    mint = _addr()
    r = asyncio.run(svc.check(mint))
    assert isinstance(r, CheckResult) and r.cluster and r.cluster_black
    asyncio.run(svc.check(mint))
    assert svc.scanner.calls == 1  # cached
    d = r.to_dict(svc.cfg)
    assert d["dev_cluster_in_the_black"] and d["panel"]["groups"]["bundlers"]["held_pct"] == pytest.approx(0.1)
    json.dumps(d, default=str)
    with pytest.raises(ValueError):
        asyncio.run(svc.check("not a mint"))


def test_service_narrative_watch_delivers_matching_launches():
    svc = _service()
    got = []

    async def on_create(q, ev, deliver):
        await deliver(Member(ev.mint, ev.name, ev.symbol), 3)

    svc.narrative.on_create = on_create

    async def cb(member, family):
        got.append((member.mint, family))

    async def go():
        svc.add_watch("trump cat", "chat1", cb)
        svc._on_create(MarketEvent("create", "M1", "d", 0, 0, 0, 0, name="TrumpCat", symbol="TCAT"))
        svc._on_create(MarketEvent("create", "M2", "d", 0, 0, 0, 0, name="Dog", symbol="DOG"))  # filtered cheaply
        await asyncio.sleep(0.01)

    asyncio.run(go())
    assert got == [("M1", 3)]
    assert svc.watches_of("chat1") == ["trump cat"]
    assert svc.remove_watches("chat1") == 1 and not svc.watches
    with pytest.raises(ValueError):
        svc.add_watch(_addr(), "chat1", cb)  # a bare address is not a live narrative


def test_service_status_and_clusters():
    svc = _service()
    s = svc.status()
    assert s["mode"] == "off" and s["black_clusters"] == 1 and s["tokens"]["ok"] == 3
    assert [c.cluster_id for c in svc.clusters()] == ["C-b1"]
    assert svc.cluster("b2").cluster_id == "C-b1"


# -- telegram ------------------------------------------------------------------------------


class Sent:
    def __init__(self):
        self.messages = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/botTOKEN/")
        body = json.loads(request.content)
        if request.url.path.endswith("/sendMessage"):
            self.messages.append((body["chat_id"], body["text"]))
            assert body["parse_mode"] == "HTML"
        return httpx.Response(200, json={"ok": True, "result": {}})


def _bot(svc):
    sent = Sent()
    bot = TelegramBot("TOKEN", {42}, svc, http=httpx.AsyncClient(transport=httpx.MockTransport(sent.handler)))
    return bot, sent


def test_telegram_rejects_strangers_and_tells_them_their_id():
    bot, sent = _bot(_service())
    asyncio.run(bot.handle({"chat": {"id": 999}, "from": {"id": 999}, "text": "/status"}))
    ((chat, text),) = sent.messages
    assert chat == 999 and "999" in text and "Доступ закритий" in text


def test_telegram_check_escapes_token_text():
    svc = _service()
    bot, sent = _bot(svc)
    mint = _addr()
    asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "text": mint}))  # plain address = /check
    assert len(sent.messages) == 2 and "Перевіряю" in sent.messages[0][1]
    report = sent.messages[1][1]
    assert "&lt;b&gt;CAT&lt;/b&gt;" in report and "Cat &amp; Co" in report and "copied &lt;socials&gt;" in report
    assert "в плюсі, бот її копіює" in report and mint in report


def test_telegram_help_status_positions_and_unknown():
    bot, sent = _bot(_service())
    for text in ("/start", "/status", "/positions", "/nope"):
        asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "text": text}))
    texts = [t for _, t in sent.messages]
    assert texts[0] == HELP and "Стан бота" in texts[1] and texts[2] == "Угод ще не було." and "Не знаю" in texts[3]


def test_telegram_helpers():
    assert TelegramBot.parse_ids("123, -100456;x") == {123, -100456}
    parts = split_message("\n".join(["a" * 100] * 100), limit=1000)
    assert all(len(p) <= 1000 for p in parts) and "".join(parts).count("a") == 10_000
    assert "відкрита" in fmt_positions([{"mint": "M" * 44, "live": 0, "size_sol": 0.1, "closed_at": None, "pnl_sol": None,
                                         "reason_open": "dip", "reason_close": None, "symbol": "X"}])
    report = LarpReport("M", TokenMeta(), [])
    assert "Ризик 0/100" in fmt_check(CheckResult("M", None, report, None, False, "no panel"), Config().selection)
    s = _service().status()
    assert "paper" not in fmt_status({**s, "mode": "off"}).split("\n")[1]


# -- web -----------------------------------------------------------------------------------


def _basic(user: str, password: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_web_requires_password_and_serves_api():
    svc = _service()

    async def go():
        async with TestClient(TestServer(create_app(svc, password="pw"))) as client:
            assert (await client.get("/api/status")).status == 401
            auth = _basic("me", "pw")
            assert (await client.get("/api/status", headers=_basic("me", "wrong"))).status == 401
            r = await client.get("/api/status", headers=auth)
            assert r.status == 200 and (await r.json())["black_clusters"] == 1
            r = await client.get("/", headers=auth)
            assert r.status == 200 and "bundlehunter" in await r.text()
            assert "script-src 'self'" in r.headers["Content-Security-Policy"]
            r = await client.get("/static/app.js", headers=auth)
            assert r.status == 200 and "textContent" in await r.text()
            assert (await client.get("/static/secret.py", headers=auth)).status == 404
            r = await client.get("/api/check/nope", headers=auth)
            assert r.status == 400 and "Solana" in (await r.json())["error"]
            r = await client.get(f"/api/check/{_addr()}", headers=auth)
            assert r.status == 200 and (await r.json())["larp"]["score"] == 25
            r = await client.get("/api/clusters", headers=auth)
            assert [c["cluster_id"] for c in await r.json()] == ["C-b1"]
            assert (await client.get("/api/cluster/b1", headers=auth)).status == 200
            assert (await client.get("/api/cluster/zzz", headers=auth)).status == 404

    asyncio.run(go())


def test_web_refuses_open_network_without_password():
    check_bind("127.0.0.1", None)
    check_bind("0.0.0.0", "pw")
    with pytest.raises(SystemExit):
        check_bind("0.0.0.0", None)


def test_ukrainian_wording():
    from bundlehunter.i18n import ua_reason

    assert ua_reason("dip 28% off high, +6% off low") == "просадка 28% від максимуму, відскок +6%"
    assert ua_reason("bundle selling (12% of its bag since entry)").startswith("бандл продає (12%")
    assert ua_reason("insiders hold 45% > 30%") == "інсайдери тримають 45% > 30%"
    assert ua_reason("closed: stop loss") == "закрито: стоп-лос"
    assert ua_reason("something new") == "something new"
    f = Finding("red", "x", "english", "українською")
    assert f.text_ua == "українською" and Finding("red", "x", "only english").text_ua == "only english"


def test_telegram_trader_commands():
    from bundlehunter.traders import TraderStats

    svc = _service()
    added = []

    async def add_trader(wallet, label=""):
        added.append((wallet, label))
        return TraderStats(wallet, label)

    svc.add_trader = add_trader
    bot, sent = _bot(svc)
    w = _addr()
    asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "text": f"/addtrader {w} Alpha"}))
    asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "text": "/traders"}))
    assert added == [(w, "Alpha")]
    assert "Трейдера додано" in sent.messages[1][1] and "мало даних" in sent.messages[1][1]
    assert "Трейдерів ще немає" in sent.messages[2][1]  # fake add didn't store it
    assert svc.signals.send == bot.notify_html  # signals go out through the bot


def test_telegram_imports_a_wallet_file():
    import json as _json

    svc = _service()
    w1, w2, w3 = _addr(), _addr(), _addr()
    export = _json.dumps([
        {"trackedWalletAddress": w1, "name": "Alpha 120k", "emoji": "😀"},
        {"trackedWalletAddress": w2, "name": "Bybit", "emoji": "🏦"},
        {"trackedWalletAddress": w3, "name": "Yugi Bundle", "emoji": "✂️"},
    ]).encode("utf-16")
    sent = Sent()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/w.txt"}})
        if request.url.path == "/file/botTOKEN/documents/w.txt":
            return httpx.Response(200, content=export)
        return sent.handler(request)

    bot = TelegramBot("TOKEN", {42}, svc, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "document": {"file_id": "F", "file_size": 100}}))
    report = sent.messages[-1][1]
    assert "Додано в стеження: <b>1</b>" in report and "біржа (CEX): 1" in report and "Yugi (1)" in report
    assert [t["address"] for t in svc.store.traders()] == [w1] and svc.signals.traders == {w1: "Alpha 120k"}


def test_telegram_paper_commands():
    svc = _service()
    bot, sent = _bot(svc)
    for text in ("/paper", "/paper deposit 1.5", "/paper reset 2", "/paper withdraw 0.5", "/paper deposit x"):
        asyncio.run(bot.handle({"chat": {"id": 42}, "from": {"id": 42}, "text": text}))
    texts = [t for _, t in sent.messages]
    assert "1.0000 SOL" in texts[0] and "2.5000 SOL" in texts[1] and "2.0000 SOL" in texts[2]
    assert "1.5000 SOL" in texts[3] and "приклад" in texts[4]


def test_web_learn_needs_intent_and_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # export/ and data/ are relative to where the bot runs
    svc = _service()

    async def go():
        async with TestClient(TestServer(create_app(svc))) as client:
            assert await (await client.get("/api/learned")).json() is None
            assert (await client.post("/api/learn", json={})).status == 403
            r = await client.post("/api/learn", json={}, headers={"X-BH-Action": "1"})
            body = await r.json()
            assert r.status == 200 and "папки немає" in body["text"] and body["learned"]["promoted"] is False

    asyncio.run(go())
