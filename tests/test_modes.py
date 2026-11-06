import asyncio

import pytest

from bundlehunter.config import ScalpConfig
from bundlehunter.execution import PaperBroker
from bundlehunter.models import MarketEvent
from bundlehunter.scalp import ScalpStrategy
from bundlehunter.storage import Store

M = "MINT"
TRADERS = {"strong": ("Ansem", 85.0)}
PANEL = {"bundlers_pct": 0.1, "insiders_pct": 0.05, "larp_score": 10, "linked_pct": 0.12, "linked_initial_pct": 0.14}


def ev(kind, wallet, price, ts, sol=1.0):
    return MarketEvent(kind, M, wallet, ts, sol, sol / price, price, f"{kind}{wallet}{ts}")


def opened(mode, **cfg):
    s = ScalpStrategy(ScalpConfig(exit_mode=mode, **cfg), TRADERS)
    s.on_event(ev("buy", "strong", 1e-6, 0))
    s.set_check(M, True, metrics=PANEL)
    buy = []
    for i in range(6):
        buy += s.on_event(ev("buy", f"r{i}", 1e-6 * (1 + 0.004 * i), 1 + i, 0.8 + 0.1 * i))
    (b,) = buy
    s.on_buy_filled(M, 1e-6, 1e5, 0.1, 10, b.reason)
    return s


def tick(s, price, ts, side="buy", sol=0.05):
    return s.on_event(ev(side, f"t{ts}", price, ts, sol))


def test_smart_partial_take_then_break_even_then_trailing_ladder():
    s = opened("smart")
    pos = s.positions[M]
    assert pos.stop_price == pytest.approx(0.85e-6)
    (tp1,) = tick(s, 1.62e-6, 20)
    assert tp1.fraction == 0.5 and tp1.token_amount == 5e4 and tp1.reason.startswith("TP1")
    s.on_partial_filled(M, 1.6e-6, 0.08, 5e4, 20)
    assert pos.tokens == 5e4 and pos.stop_price >= pos.be_price
    tick(s, 2.4e-6, 30)  # doubled: wider runner trailing (25%)
    assert pos.stop_price == pytest.approx(2.4e-6 * 0.75)
    assert tick(s, 1.7e-6, 31, "sell") == []  # first tick under the stop: wait for confirmation
    (out,) = tick(s, 1.7e-6, 36, "sell")
    assert out.reason.startswith("трейлінг") and out.fraction == 1.0
    pnl = s.on_sell_filled(M, 1.7e-6, 0.085, 36, out.reason)
    assert pnl == pytest.approx(0.08 + 0.085 - 0.1)


def test_smart_stop_ignores_a_wick_but_not_a_crash():
    s = opened("smart")
    assert tick(s, 0.84e-6, 20, "sell") == []  # wick under -15%
    assert tick(s, 0.95e-6, 21) == []  # back above: the timer resets
    assert tick(s, 0.84e-6, 22, "sell") == []
    (out,) = tick(s, 0.84e-6, 27, "sell")  # 5 s under the stop
    assert out.reason.startswith("стоп-лос")
    s2 = opened("smart")
    (crash,) = tick(s2, 0.6e-6, 20, "sell")  # -40% at once
    assert crash.reason.startswith("аварійний")


def test_never_lose_holds_through_a_dip_then_locks_break_even():
    s = opened("never_lose", never_lose_arm_pct=0.10)
    pos = s.positions[M]
    for i, p in enumerate([0.9e-6, 0.8e-6, 0.75e-6]):  # -25%: no stop yet, only the emergency one
        assert tick(s, p, 20 + i * 10, "sell") == []
    tick(s, 1.2e-6, 100)  # +20% > costs + 10%: armed
    assert pos.armed and pos.stop_price >= pos.be_price
    out = []
    for t in range(120, 130):
        out = tick(s, 1.02e-6, t, "sell", 0.01)
        if out:
            break
    assert out and out[0].ref_price >= 1.0e-6 and pos.stop_price >= pos.be_price  # out at or above break-even


def test_never_lose_ignores_a_broken_thesis_while_red_and_acts_on_it_once_green():
    s = opened("never_lose")
    for i in range(6):  # the smart wallet sells and volume dies while we are red: hold
        assert s.on_event(ev("sell", "strong" if i == 0 else f"d{i}", 0.78e-6, 60 + i, 1.0)) == []
    (out,) = tick(s, 1.15e-6, 100)  # back above break-even: now the broken thesis closes it, green
    assert "сильні гаманці продають" in out.reason and out.ref_price >= s.positions[M].be_price


def test_never_lose_is_not_magic():
    s = opened("never_lose")
    (crash,) = tick(s, 0.65e-6, 20, "sell")
    assert crash.reason.startswith("аварійний")  # a rug is still a rug
    s2 = opened("never_lose", never_lose_max_wait_s=600)
    tick(s2, 0.9e-6, 20, "sell")
    (late,) = s2.on_tick(10 + 601)
    assert "беззбиток не прийшов" in late.reason


def test_take_profit_never_below_three_times_costs():
    s = ScalpStrategy(ScalpConfig(take_profit_max_pct=0.12, take_profit_min_pct=0.12, exit_mode="smart"), TRADERS)
    s.cost_model = lambda size: 0.06
    assert s.take_profit_for(0.1) == pytest.approx(0.18)  # 3 x 6%
    b = PaperBroker(2.0, 0.0007, 1.25)
    assert b.round_trip_cost_pct(0.1) == pytest.approx(0.025 + 0.04 + 0.014)


def test_staircase_chart_and_two_step_bundle_are_refused():
    s = ScalpStrategy(ScalpConfig(), TRADERS)
    s.on_event(ev("buy", "strong", 1e-6, 0))
    s.set_check(M, True, metrics=PANEL)
    out = []
    for i in range(14):  # identical 0.3 SOL buys, price stepping up, no sells
        out += s.on_event(ev("buy", f"bot{i}", 1e-6 * (1 + 0.003 * i), 1 + i, 0.3))
    assert out == [] and "сходинкою" in s.watches[M].block_reason

    s2 = ScalpStrategy(ScalpConfig(), TRADERS)
    s2.on_event(ev("buy", "strong", 1e-6, 0))
    s2.set_check(M, True, metrics={**PANEL, "linked_pct": 0.05, "linked_initial_pct": 0.3})
    s2.on_event(ev("buy", "x", 1e-6, 1))
    assert M not in s2.watches and "двохідку" in s2.status(2)["skipped"][0]["why"]


def test_paper_account_pays_and_refuses():
    store = Store(":memory:")
    b = PaperBroker(2.0, 0.0007, 1.25, store=store, start_balance=0.1)
    assert store.paper_balance() == pytest.approx(0.1)

    async def go():
        fill = await b.buy("M", 0.05, 1e-6)
        assert fill and store.paper_balance() == pytest.approx(0.1 - (0.05 * 1.0125 + 0.0007))
        assert await b.buy("M", 0.2, 1e-6) is None  # not enough paper money
        await b.sell("M", fill.tokens, 1.2e-6)

    asyncio.run(go())
    kinds = [r["kind"] for r in store.paper_ledger()]
    assert kinds == ["sell", "buy", "reset"]
    store.paper_move("deposit", 1.0)
    assert store.paper_balance() > 1.0
    assert store.paper_move("reset", 5.0, absolute=True) == 5.0


def test_bad_exit_mode_rejected():
    with pytest.raises(ValueError):
        ScalpStrategy(ScalpConfig(exit_mode="yolo"))


def test_wallet_key_is_validated_stored_private_and_loaded(tmp_path, monkeypatch):
    from solders.keypair import Keypair

    from bundlehunter import execution

    kp = Keypair()
    path = tmp_path / "wallet.key"
    address = execution.save_keypair(str(kp), str(path))
    assert address == str(kp.pubkey()) and (path.stat().st_mode & 0o777) == 0o600
    assert str(kp) not in path.read_text()  # stored as bytes, not the base58 string
    monkeypatch.delenv("BH_PRIVATE_KEY", raising=False)
    monkeypatch.setenv("BH_KEYPAIR_PATH", str(path))
    assert str(execution.load_keypair().pubkey()) == address
    with pytest.raises(ValueError):
        execution.save_keypair("not a key", str(tmp_path / "x.key"))


def test_signal_quality_tracks_price_after_5_minutes():
    from bundlehunter.config import Config
    from bundlehunter.traders import SignalEngine

    store = Store(":memory:")
    store.add_trader("w1", "Alpha")
    eng = SignalEngine(Config(), store)
    eng.cfg.signals.analyze = False
    eng.on_trade(MarketEvent("buy", M, "w1", 0, 1.0, 1e6, 1e-6, "a"))
    eng.on_trade(MarketEvent("buy", M, "x", 100, 0.1, 1e5, 1.3e-6, "b"))  # too early for the 5 min mark
    eng.on_trade(MarketEvent("buy", M, "x", 301, 0.1, 1e5, 1.5e-6, "c"))
    q = eng.quality()
    assert q["signals"] == 1 and q["after_5m"]["n"] == 1
    assert q["after_5m"]["avg"] == pytest.approx(0.5) and q["after_5m"]["up_share"] == 1.0


def test_stake_is_capped_by_free_balance_and_live_loss_cap_stops_buying():
    from bundlehunter.config import Config
    from bundlehunter.runner import BotRunner
    from bundlehunter.strategy import Action

    class Feed:
        async def set_accounts(self, w):
            pass

    store = Store(":memory:")
    cfg = Config()
    cfg.strategy.require_holder_panel = cfg.strategy.require_larp_check = False
    broker = PaperBroker(2.0, 0.0007, 1.25, store=store, start_balance=0.2)
    runner = BotRunner(cfg, store, None, broker, Feed())
    a = Action("buy", M, 1e-6, "r", "scalp", sol_amount=1.0, strategy="scalp")
    size, _ = asyncio.run(runner._size(a))
    assert size == pytest.approx((0.2 - 0.02) * 0.25)  # 25% of the free balance, 0.02 kept for fees
    store.paper_move("reset", 0.02, absolute=True)
    size, why = asyncio.run(runner._size(a))
    assert size == 0 and "мало коштів" in why

    broker.live = True  # pretend live: the all-time loss cap
    store.open_position(mint="X", live=1, opened_at=1, size_sol=2, closed_at=2, pnl_sol=-1.5)
    said = []
    runner.notify = lambda t: said.append(t) or asyncio.sleep(0)

    async def go():
        size, why = await runner._size(a)
        await asyncio.sleep(0)
        return size, why

    size, why = asyncio.run(go())
    assert size == 0 and "ліміт" in why and any("LIVE зупинено" in t for t in said)
