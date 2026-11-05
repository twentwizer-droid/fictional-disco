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
        out += s.on_event(ev("buy", f"r{start}_{i}", price * (1 + 0.01 * i), start + i * step, sol))
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
    s = ScalpStrategy(ScalpConfig(buy_sol=0.1), TRADERS)
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
        s = ScalpStrategy(ScalpConfig(**cfg), TRADERS)
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
    s = ScalpStrategy(ScalpConfig(max_consecutive_losses=2, cooldown_after_loss_s=600), TRADERS)
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
    s = ScalpStrategy(ScalpConfig(entry_window_s=60), TRADERS)
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
