import pytest

from bundlehunter.config import StrategyConfig
from bundlehunter.models import MarketEvent
from bundlehunter.strategy import Strategy

MINT = "M"
W2C = {"dev": "C1", "b1": "C1", "b2": "C1", "loser": "C2"}
PANEL = {"top10_pct": 0.3, "dev_pct": 0.01, "snipers_pct": 0.05, "insiders_pct": 0.1, "bundlers_pct": 0.3,
         "linked_pct": 0.35}


def ev(kind, wallet, price, ts, tokens=1_000_000.0, sol=None):
    return MarketEvent(kind, MINT, wallet, ts, sol if sol is not None else price * tokens, tokens, price, f"{wallet}{ts}")


def launch(strat, p0=1e-8):
    """Bundle: dev + 2 wallets buy 30M tokens at ~p0. Returns next ts."""
    strat.on_event(ev("create", "dev", p0, 0, 10_000_000))
    strat.set_check(MINT, True, supply=1e9, metrics=PANEL)
    strat.on_event(ev("buy", "b1", p0 * 1.1, 1, 10_000_000))
    strat.on_event(ev("buy", "b2", p0 * 1.2, 2, 10_000_000))
    return 3


def feed(strat, prices, start, wallet="retail"):
    actions = []
    for i, p in enumerate(prices):
        actions += strat.on_event(ev("buy", wallet, p, start + i))
    return actions


def test_only_black_clusters_open_watches():
    s = Strategy(StrategyConfig(), W2C, {"C1"})
    s.on_event(MarketEvent("create", "Other", "loser", 0, 1, 1, 1e-8))
    assert "Other" not in s.watches
    s.on_event(ev("create", "dev", 1e-8, 0))
    assert MINT in s.watches and s.drain_new_watches() == [MINT]


def test_dip_entry_and_follow_bundle_out():
    s = Strategy(StrategyConfig(), W2C, {"C1"})
    t = launch(s)
    # pump to 3e-8 (high) - must not buy the high
    assert feed(s, [1.5e-8, 2e-8, 2.5e-8, 3e-8], t) == []
    # dip 33% to 2e-8, then bounce +6%
    assert feed(s, [2.4e-8, 2e-8], t + 10) == []
    (buy,) = feed(s, [2.12e-8], t + 20)
    assert buy.side == "buy" and buy.sol_amount == 0.1 and "dip" in buy.reason
    s.on_buy_filled(MINT, 2.12e-8, 0.1 / 2.12e-8, 0.1015, t + 20, buy.reason)
    assert feed(s, [2.2e-8], t + 21) == []
    # bundle wallet sells 4M of 30M (13%) -> exit with them
    (sell,) = s.on_event(ev("sell", "b1", 2.1e-8, t + 30, 4_000_000))
    assert sell.side == "sell" and "bundle selling" in sell.reason
    assert s.on_event(ev("sell", "b2", 2e-8, t + 31)) == []  # already closing
    pnl = s.on_sell_filled(MINT, 2.05e-8, 0.096, t + 32, sell.reason)
    assert pnl == pytest.approx(0.096 - 0.1015)
    assert MINT not in s.watches and s.closed[0]["reason_close"].startswith("bundle selling")


def test_never_pay_far_above_bundle_entry():
    s = Strategy(StrategyConfig(max_entry_vs_bundle=2.0), W2C, {"C1"})
    t = launch(s)
    # huge pump to 10x then a 30% dip: still 7x the bundle's entry -> no buy
    assert feed(s, [5e-8, 1e-7, 7e-8, 6.6e-8, 7e-8], t) == []


def test_abort_when_bundle_is_already_dumping():
    s = Strategy(StrategyConfig(), W2C, {"C1"})
    t = launch(s)
    s.drain_finished()
    assert s.on_event(ev("sell", "dev", 1e-8, t, 6_000_000)) == []  # 20% of the bundle's bag
    assert MINT not in s.watches and s.drain_finished() == [MINT]
    assert s.on_event(ev("buy", "b1", 1e-8, t + 1)) == []  # never re-watched


def test_failed_token_check_and_supply_guard():
    s = Strategy(StrategyConfig(), W2C, {"C1"})
    s.on_event(ev("create", "dev", 1e-8, 0))
    s.set_check(MINT, False, "mintAuthority=X")
    s.on_event(ev("buy", "retail", 1e-8, 1))
    assert MINT not in s.watches

    s2 = Strategy(StrategyConfig(max_cluster_supply_pct=0.5), W2C, {"C1"})
    s2.on_event(ev("create", "dev", 1e-8, 0, 600_000_000))
    s2.set_check(MINT, True, supply=1e9, metrics=PANEL)
    s2.on_event(ev("buy", "retail", 1e-8, 1))
    assert MINT not in s2.watches


def test_snipe_mode_buys_right_away_and_stop_loss():
    s = Strategy(StrategyConfig(mode="snipe"), W2C, {"C1"})
    s.on_event(ev("create", "dev", 1e-8, 0, 10_000_000))
    s.set_check(MINT, True, supply=1e9, metrics=PANEL)
    (buy,) = s.on_event(ev("buy", "b1", 1.1e-8, 1, 10_000_000))
    assert buy.reason == "snipe"
    s.on_buy_filled(MINT, 1.1e-8, 0.1 / 1.1e-8, 0.1, 1, "snipe")
    (sell,) = feed(s, [0.7e-8], 2)
    assert sell.reason == "stop loss"


def test_time_based_exit_and_entry_window():
    s = Strategy(StrategyConfig(mode="snipe", max_hold_s=60, entry_window_s=30), W2C, {"C1"})
    s.on_event(ev("create", "dev", 1e-8, 0))
    s.set_check(MINT, True, supply=1e9, metrics=PANEL)
    (buy,) = s.on_event(ev("buy", "retail", 1e-8, 1))
    s.on_buy_filled(MINT, 1e-8, 1e7, 0.1, 1, "snipe")
    (sell,) = s.on_tick(100)
    assert sell.reason == "max hold time"

    s2 = Strategy(StrategyConfig(entry_window_s=30), W2C, {"C1"})
    s2.on_event(ev("create", "dev", 1e-8, 0))
    s2.on_tick(31)
    assert MINT not in s2.watches and s2.drain_finished() == [MINT]


def test_daily_loss_limit_blocks_new_entries():
    s = Strategy(StrategyConfig(mode="snipe", daily_loss_limit_sol=0.05), W2C, {"C1"})
    s.realized_by_day["1970-01-01"] = -0.06
    s.on_event(ev("create", "dev", 1e-8, 0))
    s.set_check(MINT, True, supply=1e9, metrics=PANEL)
    assert s.on_event(ev("buy", "retail", 1e-8, 1)) == []


def test_bad_mode_rejected():
    with pytest.raises(ValueError):
        Strategy(StrategyConfig(mode="yolo"), {}, set())


def test_holder_panel_blocks_entry_until_it_clears():
    s = Strategy(StrategyConfig(mode="snipe"), W2C, {"C1"})
    s.on_event(ev("create", "dev", 1e-8, 0))
    assert s.on_event(ev("buy", "r", 1e-8, 1)) == []  # no panel yet
    s.set_check(MINT, True, supply=1e9, metrics={**PANEL, "insiders_pct": 0.45})
    assert s.on_event(ev("buy", "r", 1e-8, 2)) == []
    assert s.watches[MINT].block_reason == "insiders hold 45% > 30%"
    s.update_metrics(MINT, {**PANEL, "top10_pct": None})  # unknown values do not block
    (buy,) = s.on_event(ev("buy", "r", 1e-8, 3))
    assert buy.side == "buy"


def test_exit_when_insiders_and_bundlers_unload():
    s = Strategy(StrategyConfig(mode="snipe"), W2C, {"C1"})
    s.on_event(ev("create", "dev", 1e-8, 0))
    s.set_check(MINT, True, supply=1e9, metrics=PANEL)
    s.on_event(ev("buy", "r", 1e-8, 1))
    s.on_buy_filled(MINT, 1e-8, 1e7, 0.1, 1, "snipe")
    assert s.positions[MINT].linked_at_entry == 0.35
    s.update_metrics(MINT, {**PANEL, "linked_pct": 0.30})  # -14%: hold
    assert s.on_tick(5) == []
    s.update_metrics(MINT, {**PANEL, "linked_pct": 0.20})  # -43%: out
    (sell,) = s.on_tick(6)
    assert "insiders/bundlers dumping" in sell.reason
