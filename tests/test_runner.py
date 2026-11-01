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
        ev("create", "dev", 1e-8, 0, 10_000_000),
        ev("buy", "b1", 1.1e-8, 1, 10_000_000),
        ev("buy", "b1", 1.1e-8, 1, 10_000_000),  # duplicate (token + account subscription): ignored
        *[ev("buy", f"r{i}", p, 2 + i) for i, p in enumerate([1.5e-8, 2e-8, 3e-8, 2.4e-8, 2e-8, 2.12e-8])],
        ev("sell", "b1", 2.1e-8, 20, 3_000_000),  # bundle starts selling -> we exit
    ]
    feed = FakeFeed(events)
    cfg = Config()
    runner = BotRunner(cfg, store, FakeRpc(), PaperBroker(), feed)

    async def go():
        await runner.rebuild()
        assert {"dev", "b1", "b2"} <= feed.accounts
        await runner._consume()
        for _ in range(20):
            await asyncio.sleep(0.01)

    asyncio.run(go())
    assert store.token(MINT)["creator"] == "dev"  # launch recorded for later analysis
    assert MINT in feed.tokens and MINT in feed.unsubscribed
    (pos,) = store.positions()
    assert pos["reason_open"].startswith("dip") and pos["reason_close"].startswith("bundle selling")
    assert pos["closed_at"] and pos["pnl_sol"] is not None
    assert runner.strategy.watches.get(MINT) is None


def test_example_config_is_valid(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.chdir(root)
    cfg = load_config(root / "config.example.toml")
    assert cfg.strategy.mode == "dip" and cfg.analyzer.min_create_slot_txs == 3
