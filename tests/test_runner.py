import asyncio
from pathlib import Path

from factory import add_launch

from bundlehunter.config import Config, load_config
from bundlehunter.execution import PaperBroker
from bundlehunter.models import MarketEvent
from bundlehunter.runner import BotRunner
from bundlehunter.storage import Store

T0 = 1_700_000_000
MINT = "NewMint"


class FakeFeed:
    def __init__(self, events):
        self._events = events
        self.accounts, self.tokens, self.unsubscribed = set(), set(), set()

    async def set_accounts(self, wallets):
        self.accounts = set(wallets)

    async def subscribe_new_tokens(self):
        pass

    async def subscribe_tokens(self, mints):
        self.tokens |= set(mints)

    async def unsubscribe_tokens(self, mints):
        self.unsubscribed |= set(mints)

    async def events(self):
        for ev in self._events:
            await asyncio.sleep(0.01)  # let spawned checks / orders run
            yield ev


class FakeScanner:
    def __init__(self):
        self.calls = []

    async def panel(self, mint, wallet_to_cluster, **kw):
        from bundlehunter.holders import Group, TokenPanel

        self.calls.append((mint, kw))
        groups = {g: Group(held=1e8) for g in ("dev", "bundlers", "snipers", "insiders", "linked")}
        groups["dev"].held = 0.0
        return TokenPanel(mint, 1e9, "dev", None, groups, {}, [("w", 1e8)], "rpc")

    async def refresh(self, panel):
        return panel


class FakeLarp:
    def __init__(self):
        self.checked, self.recorded = [], []

    async def check(self, mint, live=True):
        from bundlehunter.larp import Finding, LarpReport, TokenMeta

        self.checked.append(mint)
        return LarpReport(mint, TokenMeta(), [Finding("yellow", "x", "y")])

    async def store_offchain(self, mint, uri):
        self.recorded.append((mint, uri))


class FakeRpc:
    async def get_parsed_account(self, mint):
        info = {"mintAuthority": None, "freezeAuthority": None, "supply": "1000000000000000", "decimals": 6}
        return {"data": {"parsed": {"info": info}}}


def ev(kind, wallet, price, i, tokens=1_000_000.0):
    return MarketEvent(kind, MINT, wallet, T0 + i, price * tokens, tokens, price, f"sig{i}{wallet}")


def test_runner_records_watches_and_trades_end_to_end():
    store = Store(":memory:")
    for i in range(3):
        add_launch(store, f"X{i}", "dev", ["b1", "b2"], T0 - 10_000 + i * 100, i * 1000, 1.0, 3.0)
    events = [
        MarketEvent("create", MINT, "dev", T0, 0.1, 10_000_000, 1e-8, "sig0dev", uri="https://ipfs.example/x"),
        ev("buy", "b1", 1.1e-8, 1, 10_000_000),
        ev("buy", "b1", 1.1e-8, 1, 10_000_000),  # duplicate (token + account subscription): ignored
        *[ev("buy", f"r{i}", p, 2 + i) for i, p in enumerate([1.5e-8, 2e-8, 3e-8, 2.4e-8, 2e-8, 2.12e-8])],
        ev("sell", "b1", 2.1e-8, 20, 3_000_000),  # bundle starts selling -> we exit
    ]
    feed = FakeFeed(events)
    cfg = Config()
    cfg.strategy.panel_delay_s = 0
    scanner = FakeScanner()
    larp = FakeLarp()
    runner = BotRunner(cfg, store, FakeRpc(), PaperBroker(), feed, scanner=scanner, larp=larp)

    async def go():
        await runner.rebuild()
        assert {"dev", "b1", "b2"} <= feed.accounts
        await runner._consume()
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(go())
    assert store.token(MINT)["creator"] == "dev"  # launch recorded for later analysis
    assert scanner.calls == [(MINT, {"live": True, "profile": False, "final": False, "token_program": None,
                                     "supply": 1e9})]
    assert MINT in feed.tokens and MINT in feed.unsubscribed
    (pos,) = store.positions()
    assert pos["reason_open"].startswith("dip") and pos["reason_close"].startswith("bundle selling")
    assert pos["closed_at"] and pos["pnl_sol"] is not None
    assert runner.strategy.watches.get(MINT) is None
    assert larp.checked == [MINT] and larp.recorded == [(MINT, "https://ipfs.example/x")]


def test_larp_score_blocks_entry():
    from bundlehunter.config import StrategyConfig
    from bundlehunter.strategy import Strategy

    s = Strategy(StrategyConfig(mode="snipe", max_larp_score=60), {"dev": "C1"}, {"C1"})
    s.on_event(MarketEvent("create", MINT, "dev", 0, 0, 0, 1e-8))
    s.set_check(MINT, True, supply=1e9, metrics={"larp_score": 75})
    assert s.on_event(MarketEvent("buy", MINT, "r", 1, 0.1, 1e7, 1e-8)) == []
    assert s.watches[MINT].block_reason == "LARP score 75 > 60"
    s.update_metrics(MINT, {"bundlers_pct": 0.1})  # panel refresh keeps the larp score
    assert s.watches[MINT].metrics["larp_score"] == 75


def test_example_config_is_valid(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    cfg = load_config(root / "config.example.toml")
    assert cfg.strategy.mode == "dip" and cfg.analyzer.min_create_slot_txs == 3


def test_runner_streams_trader_wallets_and_forwards_their_trades():
    store = Store(":memory:")
    feed = FakeFeed([ev("buy", "trader1", 1e-8, 1), ev("buy", "random", 1e-8, 2)])
    cfg = Config()
    runner = BotRunner(cfg, store, FakeRpc(), PaperBroker(), feed, scanner=FakeScanner(), larp=FakeLarp())
    runner.extra_accounts.add("trader1")
    seen = []
    runner.trade_listeners.append(lambda e: seen.append(e.wallet))

    async def go():
        await runner.rebuild()
        await runner._consume()

    asyncio.run(go())
    assert "trader1" in feed.accounts and seen == ["trader1", "random"]  # the engine filters by watchlist
