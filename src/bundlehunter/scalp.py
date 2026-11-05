"""Scalping strategy: take a 20-60% slice of a move that strong wallets started and volume confirms.

Built from these rules:

  1. Don't enter without a reason   -> a watch opens only when one of YOUR traders with a good
                                       score buys (bh trader rank gives the score); entry needs
                                       one strong wallet or several different smart ones.
  2. Don't buy the move blind        -> the move must be confirmed: buy volume, buy/sell pressure
                                       and new buyers in the last minute. No entry into a
                                       vertical candle, and never far above where the smart
                                       wallets bought. A missed trade costs 0.
  3. Always have a plan              -> take profit (20-60% by position size), stop loss, trailing
                                       stop, max hold time: fixed BEFORE entry and written into
                                       the trade.
  4. Get out in time                 -> exit when the idea stops working: sells outweigh buys,
                                       the smart wallets sell, the stop or the time limit is hit.
  5. Don't take revenge on the market-> cooldown after a loss, a pause after a losing streak, a
                                       daily loss limit and a max number of trades per hour.
  6. Know when not to trade          -> every skipped or blocked entry says why (dashboard).

Same interface as `Strategy`: it returns Actions and is told about fills, so the runner, the
paper broker and the live broker drive it the same way.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import ScalpConfig
from .models import MarketEvent
from .strategy import Action

log = logging.getLogger(__name__)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Momentum:
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    buyers: int = 0
    trades: int = 0

    @property
    def pressure(self) -> float:
        return self.buy_sol / self.sell_sol if self.sell_sol > 0 else (math.inf if self.buy_sol > 0 else 0.0)


@dataclass
class ScalpWatch:
    mint: str
    started_ts: float
    smart: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # person -> (ts, price, score)
    smart_sold: set[str] = field(default_factory=set)
    trades: deque = field(default_factory=lambda: deque(maxlen=5000))  # (ts, side, sol, wallet, price)
    last_price: float = 0.0
    high: float = 0.0
    check_ok: bool | None = None
    check_reason: str = ""
    metrics: dict[str, float | None] | None = None
    pending: bool = False
    block_reason: str = ""
    done_reason: str = ""

    @property
    def first_smart_price(self) -> float:
        return min((p for _, p, _ in self.smart.values() if p > 0), default=0.0)

    def momentum(self, now: float, window: float) -> Momentum:
        m, buyers = Momentum(), set()
        for ts, side, sol, wallet, _ in self.trades:
            if now - ts > window:
                continue
            m.trades += 1
            if side == "buy":
                m.buy_sol += sol
                buyers.add(wallet)
            else:
                m.sell_sol += sol
        m.buyers = len(buyers)
        return m

    def price_ago(self, now: float, seconds: float) -> float | None:
        """Price of the first trade inside the last `seconds` (what the candle opened at)."""
        for ts, _, _, _, price in self.trades:
            if now - ts <= seconds and price > 0:
                return price
        return None


@dataclass
class ScalpPosition:
    mint: str
    entry_price: float
    entry_ts: float
    size_sol: float
    tokens: float
    tp: float
    sl: float
    trail: float
    max_hold_s: float
    persons: set[str]
    reason_open: str
    high: float = 0.0
    position_id: int | None = None
    closing: bool = False
    cluster_id: str = "scalp"

    @property
    def plan(self) -> str:
        return (f"TP +{self.tp:.0%}, стоп −{self.sl:.0%}, трейлінг {self.trail:.0%} після +{self.tp / 2:.0%}, "
                f"макс {self.max_hold_s / 60:.0f} хв")


class ScalpStrategy:
    name = "scalp"

    def __init__(self, cfg: ScalpConfig, traders: dict[str, tuple[str, float | None]] | None = None) -> None:
        self.cfg = cfg
        self.traders: dict[str, tuple[str, float | None]] = dict(traders or {})  # wallet -> (person, score)
        self.watches: dict[str, ScalpWatch] = {}
        self.positions: dict[str, ScalpPosition] = {}
        self.closed: list[dict[str, object]] = []
        self.realized_by_day: dict[str, float] = defaultdict(float)
        self.skipped: deque = deque(maxlen=50)  # (ts, mint, why): "when NOT to trade" is a decision too
        self.loss_streak = 0
        self.last_loss_ts = float("-inf")  # no loss yet
        self.paused_until = 0.0
        self.entries: deque = deque(maxlen=100)
        self._seen: set[str] = set()
        self._new: list[str] = []
        self._finished: list[str] = []

    # -- plumbing (same interface as Strategy) ---------------------------------------------

    def update_traders(self, traders: dict[str, tuple[str, float | None]]) -> None:
        self.traders = dict(traders)

    def tracked_wallets(self) -> list[str]:
        return []  # trader wallets are streamed by the runner (extra_accounts)

    def drain_new_watches(self) -> list[str]:
        out, self._new = self._new, []
        return out

    def drain_finished(self) -> list[str]:
        out, self._finished = self._finished, []
        return out

    def set_check(self, mint: str, ok: bool, reason: str = "", supply: float | None = None,
                  metrics: dict[str, float | None] | None = None) -> None:
        w = self.watches.get(mint)
        if w:
            w.check_ok, w.check_reason = ok, reason
            if metrics is not None:
                w.metrics = metrics

    def update_metrics(self, mint: str, metrics: dict[str, float | None]) -> None:
        w = self.watches.get(mint)
        if w:
            w.metrics = {**(w.metrics or {}), **metrics}

    def today_pnl(self, ts: float) -> float:
        return self.realized_by_day[_day(ts)]

    def status(self, now: float) -> dict[str, object]:
        cfg = self.cfg
        state = "працює"
        if not cfg.enabled:
            state = "вимкнено"
        elif now < self.paused_until:
            state = f"пауза після {self.loss_streak} збитків поспіль ще {(self.paused_until - now) / 60:.0f} хв"
        elif now - self.last_loss_ts < cfg.cooldown_after_loss_s:
            state = f"охолодження після збитку ще {(cfg.cooldown_after_loss_s - (now - self.last_loss_ts)) / 60:.0f} хв"
        elif self.today_pnl(now) <= -cfg.daily_loss_limit_sol:
            state = "денний ліміт збитку досягнуто"
        closed = self.closed
        return {
            "state": state,
            "today_pnl_sol": round(self.today_pnl(now), 4),
            "trades": len(closed),
            "win_rate": sum(float(c["pnl_sol"]) > 0 for c in closed) / len(closed) if closed else None,
            "loss_streak": self.loss_streak,
            "entries_last_hour": sum(1 for t in self.entries if now - t < 3600),
            "watching": [
                {"mint": w.mint, "smart": sorted(w.smart), "age_s": now - w.started_ts, "blocked": w.block_reason,
                 "in_position": w.mint in self.positions}
                for w in self.watches.values()
            ],
            "skipped": [{"ts": ts, "mint": m, "why": why} for ts, m, why in list(self.skipped)[-15:]],
        }

    # -- events ----------------------------------------------------------------------------

    def on_event(self, ev: MarketEvent) -> list[Action]:
        cfg = self.cfg
        trader = self.traders.get(ev.wallet)
        smart = bool(trader and trader[1] is not None and trader[1] >= cfg.min_trader_score)
        w = self.watches.get(ev.mint)
        if w is None:
            if not (cfg.enabled and smart and ev.kind == "buy" and ev.sol_amount >= cfg.min_smart_buy_sol
                    and ev.mint not in self._seen):
                return []
            w = ScalpWatch(ev.mint, ev.ts)
            self.watches[ev.mint] = w
            self._seen.add(ev.mint)
            self._new.append(ev.mint)
            log.info("scalp watch %s: %s (score %s) bought", ev.mint, trader[0], trader[1])  # type: ignore[index]

        if ev.kind in ("buy", "sell") and ev.price > 0:
            w.trades.append((ev.ts, ev.kind, ev.sol_amount, ev.wallet, ev.price))
            w.last_price = ev.price
            w.high = max(w.high, ev.price)
        if smart and trader:
            person, score = trader[0], float(trader[1] or 0)
            if ev.kind == "buy" and ev.sol_amount >= cfg.min_smart_buy_sol and person not in w.smart:
                w.smart[person] = (ev.ts, ev.price, score)
            elif ev.kind == "sell" and person in w.smart:
                w.smart_sold.add(person)

        pos = self.positions.get(ev.mint)
        return self._exits(w, pos, ev.ts) if pos else self._entry(w, ev.ts)

    def on_tick(self, now: float) -> list[Action]:
        actions: list[Action] = []
        for mint, w in list(self.watches.items()):
            pos = self.positions.get(mint)
            if pos:
                actions += self._exits(w, pos, now)
            elif not w.pending and now - w.started_ts > self.cfg.entry_window_s:
                self._finish(w, f"за {self.cfg.entry_window_s / 60:.0f} хв не підтвердилось: {w.block_reason or '—'}",
                             skipped=True)
        return actions

    # -- decisions -------------------------------------------------------------------------

    def risk_block(self, now: float) -> str:
        cfg = self.cfg
        if now < self.paused_until:
            return f"пауза: {self.loss_streak} збитки поспіль, не мщу ринку"
        if now - self.last_loss_ts < cfg.cooldown_after_loss_s:
            return "охолодження після збитку"
        if self.today_pnl(now) <= -cfg.daily_loss_limit_sol:
            return "денний ліміт збитку"
        if sum(1 for t in self.entries if now - t < 3600) >= cfg.max_trades_per_hour:
            return "забагато угод за годину"
        if len(self.positions) >= cfg.max_open_positions:
            return "максимум відкритих позицій"
        return ""

    def _entry(self, w: ScalpWatch, now: float) -> list[Action]:
        cfg = self.cfg
        if w.pending:
            return []
        if now - w.started_ts > cfg.entry_window_s:
            return self._finish(w, f"час на вхід минув: {w.block_reason or '—'}", skipped=True)
        if w.smart_sold:
            return self._finish(w, "сильні гаманці вже продають: ідеї більше немає", skipped=True)
        if w.check_ok is None:
            w.block_reason = "чекаю перевірки токена"
            return []
        if not w.check_ok:
            return self._finish(w, f"перевірка токена: {w.check_reason}", skipped=True)
        m = w.metrics or {}
        for key, limit, label in (("bundlers_pct", cfg.max_bundlers_pct, "бандлери"),
                                  ("insiders_pct", cfg.max_insiders_pct, "інсайдери")):
            if m.get(key) is not None and m[key] > limit:  # type: ignore[operator]
                w.block_reason = f"{label} тримають {m[key]:.0%}"
                return []
        if m.get("larp_score") is not None and m["larp_score"] > cfg.max_larp_score:  # type: ignore[operator]
            return self._finish(w, f"LARP-оцінка {m['larp_score']:.0f}: наратив позичений", skipped=True)
        risk = self.risk_block(now)
        if risk:
            w.block_reason = risk
            return []

        # 1. reason: who is in
        strong = [p for p, (_, _, sc) in w.smart.items() if sc >= cfg.strong_trader_score]
        if not strong and len(w.smart) < cfg.min_smart_wallets:
            w.block_reason = f"сильних гаманців {len(w.smart)} з {cfg.min_smart_wallets}"
            return []
        # 2. confirmation by volume, not by a green candle
        mo = w.momentum(now, cfg.momentum_window_s)
        if mo.buy_sol < cfg.min_buy_volume_sol or mo.buyers < cfg.min_unique_buyers or mo.pressure < cfg.min_buy_pressure:
            w.block_reason = (f"немає підтвердження: купівлі {mo.buy_sol:.1f} SOL, продажі {mo.sell_sol:.1f}, "
                              f"покупців {mo.buyers} за {cfg.momentum_window_s:.0f} с")
            return []
        base = w.first_smart_price
        if base and w.last_price > base * (1 + cfg.max_chase_pct):
            w.block_reason = f"вже +{w.last_price / base - 1:.0%} від входу сильних: не доганяю"
            return []
        opened = w.price_ago(now, cfg.spike_window_s)
        if opened and w.last_price > opened * (1 + cfg.max_spike_pct):
            w.block_reason = f"вертикальна свіча +{w.last_price / opened - 1:.0%} за {cfg.spike_window_s:.0f} с"
            return []

        w.pending = True
        w.block_reason = ""
        who = ", ".join(sorted(w.smart))
        reason = (f"сильні гаманці: {who}; купівлі {mo.buy_sol:.1f} SOL проти продажів {mo.sell_sol:.1f}, "
                  f"{mo.buyers} покупців за {cfg.momentum_window_s:.0f} с")
        return [Action("buy", w.mint, w.last_price, reason, "scalp", sol_amount=cfg.buy_sol, strategy=self.name)]

    def take_profit_for(self, size_sol: float) -> float:
        cfg = self.cfg
        k = min(max(size_sol / cfg.size_for_min_tp_sol, 0.0), 1.0) if cfg.size_for_min_tp_sol > 0 else 1.0
        return cfg.take_profit_max_pct - (cfg.take_profit_max_pct - cfg.take_profit_min_pct) * k

    def _exits(self, w: ScalpWatch, pos: ScalpPosition, now: float) -> list[Action]:
        if pos.closing or w.last_price <= 0:
            return []
        cfg, p, e = self.cfg, w.last_price, pos.entry_price
        pos.high = max(pos.high, p)
        reason = ""
        if p >= e * (1 + pos.tp):
            reason = f"тейк-профіт +{p / e - 1:.0%} за планом"
        elif p <= e * (1 - pos.sl):
            reason = f"стоп-лос {p / e - 1:.0%}: ідея не спрацювала"
        elif pos.high >= e * (1 + pos.tp / 2) and p <= pos.high * (1 - pos.trail):
            reason = f"трейлінг: −{1 - p / pos.high:.0%} від максимуму, фіксую {p / e - 1:+.0%}"
        elif cfg.exit_on_smart_sell and pos.persons & w.smart_sold:
            reason = "сильні гаманці продають: виходжу разом з ними"
        else:
            mo = w.momentum(now, cfg.momentum_window_s)
            if mo.sell_sol >= cfg.min_buy_volume_sol / 2 and mo.sell_sol >= cfg.exit_sell_pressure * max(mo.buy_sol, 1e-9):
                reason = f"обсяг згас: продажі {mo.sell_sol:.1f} SOL проти купівель {mo.buy_sol:.1f}"
            elif now - pos.entry_ts >= pos.max_hold_s:
                reason = f"час вийшов ({pos.max_hold_s / 60:.0f} хв), рух не підтвердився"
        if not reason:
            return []
        pos.closing = True
        return [Action("sell", pos.mint, p, reason, "scalp", token_amount=pos.tokens, strategy=self.name)]

    def _finish(self, w: ScalpWatch, reason: str, skipped: bool = False) -> list[Action]:
        w.done_reason = reason
        self.watches.pop(w.mint, None)
        self._finished.append(w.mint)
        if skipped:
            self.skipped.append((w.started_ts, w.mint, reason))
        log.info("scalp drop %s: %s", w.mint, reason)
        return []

    # -- fills -----------------------------------------------------------------------------

    def on_buy_filled(self, mint: str, price: float, tokens: float, sol_spent: float, ts: float, reason: str,
                      position_id: int | None = None) -> ScalpPosition:
        w = self.watches[mint]
        w.pending = False
        cfg = self.cfg
        pos = ScalpPosition(mint, price, ts, sol_spent, tokens, self.take_profit_for(sol_spent), cfg.stop_loss_pct,
                            cfg.trailing_pct, cfg.max_hold_s, set(w.smart), reason, price, position_id)
        self.positions[mint] = pos
        self.entries.append(ts)
        return pos

    def on_buy_failed(self, mint: str) -> None:
        w = self.watches.get(mint)
        if w:
            w.pending = False
            self._finish(w, "купівля не вдалася")

    def on_sell_filled(self, mint: str, price: float, sol_received: float, ts: float, reason: str) -> float:
        pos = self.positions.pop(mint)
        pnl = sol_received - pos.size_sol
        self.realized_by_day[_day(ts)] += pnl
        if pnl < 0:
            self.loss_streak += 1
            self.last_loss_ts = ts
            if self.loss_streak >= self.cfg.max_consecutive_losses:
                self.paused_until = ts + self.cfg.pause_after_streak_s
        else:
            self.loss_streak = 0
        self.closed.append({"mint": mint, "entry_price": pos.entry_price, "exit_price": price, "size_sol": pos.size_sol,
                            "pnl_sol": pnl, "held_s": ts - pos.entry_ts, "reason_open": pos.reason_open,
                            "plan": pos.plan, "reason_close": reason})
        w = self.watches.get(mint)
        if w:
            self._finish(w, f"закрито: {reason}")
        return pnl

    def on_sell_failed(self, mint: str) -> None:
        pos = self.positions.get(mint)
        if pos:
            pos.closing = False
