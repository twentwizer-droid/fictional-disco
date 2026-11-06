import pytest

from bundlehunter.config import ScalpConfig
from bundlehunter.models import MarketEvent
from bundlehunter.scalp import ScalpPosition, ScalpStrategy

M = "MINT"
TRADERS = {"strong": ("Ansem", 85.0), "a1": ("Alpha", 65.0), "b1": ("Beta", 70.0), "weak": ("Weak", 40.0),
           "nodata": ("New", None)}
PANEL = {"bundlers_pct": 0.1, "insiders_pct": 0.05, "larp_score": 10}


def ev(kind, wallet, price, ts, sol=1.0):
    return MarketEvent(kind, M, wallet, ts, sol, sol / price, price, f"{kind}{wallet}{ts}")


def crowd(s, start, price, n=6, sol=0.8, step=1.0):
    """n retail buyers: volume and new buyers confirm the move."""
    out = []
    for i in range(n):
        size = sol * (0.6 + 0.15 * (i % 5))  # real crowds buy different amounts
        out += s.on_event(ev("buy", f"r{start}_{i}", price * (1 + 0.01 * i), start + i * step, size))
    return out


def ready(s, ts=0.0):
    s.set_check(M, True, metrics=PANEL)


def test_no_reason_no_watch():
    s = ScalpStrategy(ScalpConfig(), TRADERS)
    assert s.on_event(ev("buy", "weak", 1e-6, 0)) == []  # score 40 < 60
    assert s.on_event(ev("buy", "nodata", 1e-6, 0)) == []  # never ranked: no score
    assert s.on_event(ev("buy", "random", 1e-6, 0)) == []
    assert not s.watches


def test_strong_wallet_plus_volume_confirmation_enters_with_plan():
    s = ScalpStrategy(ScalpConfig(buy_sol=0.1, exit_mode="fixed"), TRADERS)
    s.on_event(ev("buy", "strong", 1e-6, 0))
    assert s.drain_new_watches() == [M]
    ready(s)
    assert crowd(s, 1, 1e-6, n=3) == []  # 3 buyers, 2.4 SOL: not confirmed yet
    assert "немає підтвердження" in s.watches[M].block_reason
    (buy,) = crowd(s, 10, 1.03e-6, n=4)
    assert buy.side == "buy" and buy.strategy == "scalp" and "Ansem" in buy.reason
    pos = s.on_buy_filled(M, buy.ref_price, 0.1 / buy.ref_price, 0.1, 15, buy.reason)
    assert round(pos.tp, 3) == 0.58 and pos.sl == 0.15  # small size -> take profit near 60%
    assert "TP +58%" in pos.plan and "стоп −15%" in pos.plan


def test_two_smart_people_needed_without_a_strong_one():
    s = ScalpStrategy(ScalpConfig(), TRADERS)
    s.on_event(ev("buy", "a1", 1e-6, 0))
    ready(s)
    assert crowd(s, 1, 1e-6, n=6) == []
    assert "сильних гаманців 1 з 2" in s.watches[M].block_reason
    actions = s.on_event(ev("buy", "b1", 1.05e-6, 8))  # the second smart person completes the reason
    actions += crowd(s, 9, 1.05e-6, n=6)
    assert len(actions) == 1 and "Alpha" in actions[0].reason and "Beta" in actions[0].reason


def test_no_chasing_and_no_vertical_candle():
    s = ScalpStrategy(ScalpConfig(), TRADERS)
    s.on_event(ev("buy", "strong", 1e-6, 0))
    ready(s)
    assert crowd(s, 100, 1.6e-6, n=6) == []  # +60% above the smart entry
    assert "не доганяю" in s.watches[M].block_reason

    s2 = ScalpStrategy(ScalpConfig(max_chase_pct=5.0), TRADERS)
    s2.on_event(ev("buy", "strong", 1e-6, 0))
    ready(s2)
    crowd(s2, 100, 1e-6, n=6, step=2)  # confirmed volume...
    s2.watches[M].pending = False
    out = []
    for i, p in enumerate([1.1e-6, 1.25e-6, 1.4e-6]):  # ...then +40% within 3 seconds
        s2.watches[M].pending = False
        out = s2.on_event(ev("buy", f"x{i}", p, 112 + i))
    assert out == [] and "вертикальна свіча" in s2.watches[M].block_reason


def test_exits_take_profit_stop_trailing_momentum_smart_sell_time():
    def opened(**cfg):
        s = ScalpStrategy(ScalpConfig(**{"exit_mode": "fixed", **cfg}), TRADERS)
        s.on_event(ev("buy", "strong", 1e-6, 0))
        ready(s)
        (buy,) = crowd(s, 1, 1e-6)
        s.on_buy_filled(M, 1e-6, 1e5, 0.1, 10, buy.reason)
        return s

    s = opened()
    (sell,) = s.on_event(ev("buy", "z", 1.6e-6, 20, 0.1))
    assert sell.reason.startswith("тейк-профіт")
    s = opened()
    (sell,) = s.on_event(ev("sell", "z", 0.84e-6, 20, 0.1))
    assert sell.reason.startswith("стоп-лос")
    s = opened()
    s.on_event(ev("buy", "z", 1.35e-6, 20, 0.1))  # above half the take profit
    (sell,) = s.on_event(ev("sell", "z", 1.2e-6, 21, 0.1))
    assert sell.reason.startswith("трейлінг")
    s = opened()
    (sell,) = s.on_event(ev("sell", "strong", 1.02e-6, 20, 0.5))
    assert "сильні гаманці продають" in sell.reason
    s = opened()
    out = []
    for i in range(5):
        out = s.on_event(ev("sell", f"d{i}", 1.0e-6, 100 + i, 1.0))
        if out:
            break
    assert out and out[0].reason.startswith("обсяг згас")
    s = opened()
    (sell,) = s.on_tick(10 + 601)
    assert sell.reason.startswith("час вийшов")


def test_no_revenge_trading_after_losses():
    s = ScalpStrategy(ScalpConfig(max_consecutive_losses=2, cooldown_after_loss_s=600, exit_mode="smart"), TRADERS)
    s.positions["X"] = ScalpPosition("X", 1.0, 0, 0.1, 1, 0.5, 0.15, 0.1, 600, set(), "r")
    s.on_sell_filled("X", 0.8, 0.08, 100, "стоп-лос")
    assert s.loss_streak == 1 and s.risk_block(200) == "охолодження після збитку" and s.risk_block(800) == ""
    s.positions["Y"] = ScalpPosition("Y", 1.0, 0, 0.1, 1, 0.5, 0.15, 0.1, 600, set(), "r")
    s.on_sell_filled("Y", 0.8, 0.08, 900, "стоп-лос")
    assert s.risk_block(1600).startswith("пауза") and "пауза" in s.status(1600)["state"]
    # the block shows up as the reason a confirmed setup is not taken
    s.on_event(ev("buy", "strong", 1e-6, 1700))
    ready(s)
    assert crowd(s, 1701, 1e-6) == [] and s.watches[M].block_reason.startswith("пауза")


def test_skips_are_recorded_with_reasons():
    s = ScalpStrategy(ScalpConfig(entry_window_s=60, exit_mode="smart"), TRADERS)
    s.on_event(ev("buy", "strong", 1e-6, 0))
    ready(s)
    s.on_tick(100)
    assert not s.watches and s.drain_finished() == [M]
    assert "не підтвердилось" in s.status(100)["skipped"][0]["why"]
    s2 = ScalpStrategy(ScalpConfig(), TRADERS)
    s2.on_event(ev("buy", "strong", 1e-6, 0))
    s2.set_check(M, True, metrics={**PANEL, "larp_score": 80})
    s2.on_event(ev("buy", "z", 1e-6, 1))
    assert "LARP" in s2.status(2)["skipped"][0]["why"]


def _candle_entry(cfg):
    s = ScalpStrategy(cfg, TRADERS)
    s.cost_model = lambda size: 0.06
    s.on_event(ev("buy", "strong", 1e-6, 0))
    ready(s)
    (buy,) = crowd(s, 1, 1.0e-6, n=7)
    return s, s.on_buy_filled(M, buy.ref_price, 0.1 / buy.ref_price, 0.1, 8, buy.reason)


def test_candle_mode_is_default_and_lives_one_candle():
    s, pos = _candle_entry(ScalpConfig(buy_sol=0.1))
    assert pos.mode == "candle" and pos.max_hold_s == 60 and pos.sl == 0.08
    assert pos.tp == pytest.approx(0.15) and "одна свічка 60 с" in pos.plan
    assert s.on_tick(30) == []
    (sell,) = s.on_tick(68)  # the candle closed: out, whatever the price
    assert sell.side == "sell" and "свічка закрилась" in sell.reason and sell.fraction == 1.0


def test_candle_mode_takes_profit_and_stops_at_once():
    s, pos = _candle_entry(ScalpConfig(buy_sol=0.1))
    (tp,) = s.on_event(ev("buy", "x", pos.entry_price * 1.16, 10))
    assert "тейк-профіт" in tp.reason and tp.fraction == 1.0
    s, pos = _candle_entry(ScalpConfig(buy_sol=0.1))
    (sl,) = s.on_event(ev("sell", "x", pos.entry_price * 0.91, 10, 0.1))
    assert "стоп-лос" in sl.reason  # no waiting for confirmation inside one candle


def test_candle_tp_never_below_two_times_costs_and_short_cooldown():
    s = ScalpStrategy(ScalpConfig(), TRADERS)
    s.cost_model = lambda size: 0.10
    assert s.take_profit_for(0.1) == pytest.approx(0.20)
    s.positions["X"] = ScalpPosition("X", 1.0, 0, 0.1, 1, 0.5, 0.08, 0.05, 60, set(), "r")
    s.on_sell_filled("X", 0.9, 0.09, 100, "стоп")
    assert s.risk_block(130) == "охолодження після збитку" and s.risk_block(161) == ""
    assert s.status(161)["max_per_hour"] == 30


def test_candle_mode_waits_for_a_green_candle():
    s = ScalpStrategy(ScalpConfig(buy_sol=0.1), TRADERS)
    s.on_event(ev("buy", "strong", 2e-6, 0))
    ready(s)
    assert crowd(s, 1, 1.5e-6, n=7) == []  # plenty of buyers, but the candle is red (2.0 -> 1.5)
    assert s.watches[M].block_reason == "свічка не зелена: чекаю зеленої"
    s.on_tick(181)  # 3 candles without a setup: skip and free the slot
    assert M not in s.watches
