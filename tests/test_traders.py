import asyncio

from factory import trade
from solders.keypair import Keypair

from bundlehunter.config import Config
from bundlehunter.larp import Finding, LarpReport, TokenMeta
from bundlehunter.models import MarketEvent, ParsedTx
from bundlehunter.service import CheckResult
from bundlehunter.storage import Store
from bundlehunter.traders import SignalEngine, fmt_analysis, trader_stats

T0 = 1_700_000_000


def _addr() -> str:
    return str(Keypair().pubkey())


def _store_with_history(wallet: str) -> Store:
    s = Store(":memory:")
    trades = []
    for i in range(6):  # 5 winners, 1 loser, all closed
        out = 0.5 if i == 5 else 2.0
        trades += [trade(f"b{i}", f"M{i}", wallet, "buy", 1.0, 1e6, i * 10, T0 + i * 1000),
                   trade(f"s{i}", f"M{i}", wallet, "sell", out, 1e6, i * 10 + 5, T0 + i * 1000 + 600)]
    trades.append(trade("open", "M_open", wallet, "buy", 0.5, 1e6, 100, T0 + 9000))  # still holding
    trades.append(trade("old", "M_old", wallet, "sell", 3.0, 1e6, 101, T0 + 9100))  # bought before history
    s.add_parsed([ParsedTx("x", 0, None, False, "", [], trades=trades)])
    return s


def test_trader_stats():
    w = _addr()
    st = trader_stats(_store_with_history(w), w, "Alpha")
    assert len(st.tokens) == 7 and len(st.closed) == 6  # M_old ignored
    assert abs(st.realized_pnl - (5 * 1.0 - 0.5)) < 1e-9 and abs(st.win_rate - 5 / 6) < 1e-9
    assert st.median_hold_s == 600 and st.verdict == "в плюсі"
    assert "PnL +4.5 SOL" in st.line() and st.to_dict()["recent"][0]["mint"] == "M_open"


class FakeService:
    def __init__(self):
        self.checked = []

    async def check(self, mint):
        self.checked.append(mint)
        report = LarpReport(mint, TokenMeta(symbol="CAT"), [Finding("red", "copied_twitter", "en", "скопійовані соцмережі")])
        return CheckResult(mint, None, report, None, False, "немає аналізу бандла")

    def index(self):
        class I:
            clusters = {}
        return I()


def _engine(store, traders):
    for addr, label in traders:
        store.add_trader(addr, label)
    out = []

    async def send(text):
        out.append(text)

    return SignalEngine(Config(), store, FakeService(), send), out


def ev(kind, mint, wallet, sol, ts):
    return MarketEvent(kind, mint, wallet, ts, sol, 1e6, sol / 1e6, f"{kind}{wallet}{ts}")


def test_signals_buy_confluence_sell_and_filters():
    a, b, stranger = _addr(), _addr(), _addr()
    store = _store_with_history(a)
    store.upsert_token("MINT", symbol="<CAT>")
    eng, out = _engine(store, [(a, "Alpha"), (b, "Beta")])

    async def go():
        eng.on_trade(ev("buy", "MINT", stranger, 5.0, T0))  # not your trader
        eng.on_trade(ev("buy", "MINT", a, 0.05, T0))  # too small
        eng.on_trade(ev("buy", "MINT", a, 1.0, T0 + 1))  # signal 1 + analysis
        eng.on_trade(ev("buy", "MINT", a, 1.0, T0 + 2))  # adding to the same position: silent
        eng.on_trade(ev("buy", "MINT", b, 0.5, T0 + 300))  # confluence signal
        eng.on_trade(ev("sell", "OTHER", b, 1.0, T0 + 400))  # never signalled: silent
        eng.on_trade(ev("sell", "MINT", a, 2.0, T0 + 500))  # exit signal
        await asyncio.sleep(0.01)

    asyncio.run(go())
    buys = [m for m in out if "купив" in m or "в одному токені" in m]
    assert len(buys) == 2
    assert "Трейдер купив" in buys[0] and "Alpha" in buys[0] and "&lt;CAT&gt;" in buys[0] and "в плюсі" in buys[0]
    assert "2 твоїх трейдери в одному токені" in buys[1] and "Також купили" in buys[1] and "Alpha" in buys[1]
    assert sum("Трейдер продає" in m for m in out) == 1
    analyses = [m for m in out if m.startswith("🔎")]
    assert len(analyses) == 1 and "скопійовані соцмережі" in analyses[0]  # once per token
    assert eng.service.checked == ["MINT"] and eng.sent == 3  # signals; analyses are follow-ups


def test_bundle_signal_mentions_your_traders():
    a = _addr()
    store = Store(":memory:")
    eng, out = _engine(store, [(a, "Alpha")])

    async def go():
        eng.on_trade(ev("buy", "MINT", a, 1.0, T0))
        eng.on_bundle_watch("MINT", "C-x")
        await asyncio.sleep(0.01)

    asyncio.run(go())
    bundle = next(m for m in out if "бандл-група" in m)
    assert "Твої трейдери вже тут: Alpha" in bundle


def test_fmt_analysis_short():
    report = LarpReport("M", TokenMeta(symbol="X"), [])
    text = fmt_analysis(CheckResult("M", None, report, None, False, ""))
    assert "LARP 0/100" in text
