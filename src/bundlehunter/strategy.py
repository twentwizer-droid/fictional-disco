"""Copy-trading decisions: follow profitable bundle clusters in, and out.

The edge of copying a bundler is NOT the entry (they always get in cheaper than
you - they *are* the first buyers), it is knowing who holds the bag and leaving
when they start to leave. So:

  * a launch/buy by a wallet from a cluster that is "in the black" opens a watch;
  * "dip" mode waits for a pullback from the local high plus a small bounce and
    refuses to pay more than N x the bundle's own average entry (no buying highs);
  * "snipe" mode buys immediately (fastest, but you pay up and eat the most risk);
  * anything that looks like a rug or a dump in progress cancels the watch;
  * once in, we exit as soon as the cluster sells a slice of its bag, or on
    take-profit / stop-loss / trailing stop / max hold time.

The class is synchronous and side-effect free: it returns `Action`s and is told
about fills, so the same code drives live trading, paper trading and backtests.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import StrategyConfig
from .models import MarketEvent

log = logging.getLogger(__name__)


@dataclass
class Action:
    side: str  # "buy" | "sell"
    mint: str
    ref_price: float
    reason: str
    cluster_id: str
    sol_amount: float = 0.0  # buys
    token_amount: float = 0.0  # sells


@dataclass
class Position:
    mint: str
    cluster_id: str
    entry_price: float
    entry_ts: float
    size_sol: float  # SOL spent incl. fees
    tokens: float
    high: float
    cluster_sold_at_entry: float
    reason_open: str
    position_id: int | None = None
    closing: bool = False


@dataclass
class Watch:
    mint: str
    cluster_id: str
    started_ts: float
    creator: str = ""
    cluster_bought: float = 0.0
    cluster_sold: float = 0.0
    cluster_sol_in: float = 0.0
    high: float = 0.0
    low_after_high: float = math.inf
    last_price: float = 0.0
    trades_seen: int = 0
    check_ok: bool | None = None
    check_reason: str = ""
    supply: float | None = None
    pending: bool = False
    done_reason: str = ""

    @property
    def cluster_sold_frac(self) -> float:
        return self.cluster_sold / self.cluster_bought if self.cluster_bought else 0.0

    @property
    def cluster_avg_price(self) -> float:
        return self.cluster_sol_in / self.cluster_bought if self.cluster_bought else 0.0


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


class Strategy:
    def __init__(self, cfg: StrategyConfig, wallet_to_cluster: dict[str, str], black: set[str]) -> None:
        if cfg.mode not in ("dip", "snipe"):
            raise ValueError(f"strategy.mode must be 'dip' or 'snipe', got {cfg.mode!r}")
        self.cfg = cfg
        self.wallet_to_cluster = dict(wallet_to_cluster)
        self.black = set(black)
        self.watches: dict[str, Watch] = {}
        self.positions: dict[str, Position] = {}
        self.realized_by_day: dict[str, float] = defaultdict(float)
        self.closed: list[dict[str, object]] = []
        self._seen: set[str] = set()
        self._new_watches: list[str] = []
        self._finished: list[str] = []

    # -- cluster bookkeeping ---------------------------------------------------

    def update_clusters(self, wallet_to_cluster: dict[str, str], black: set[str]) -> None:
        self.wallet_to_cluster = dict(wallet_to_cluster)
        self.black = set(black)

    def add_wallet(self, wallet: str, cluster_id: str) -> None:
        self.wallet_to_cluster[wallet] = cluster_id

    def tracked_wallets(self) -> list[str]:
        return sorted(w for w, c in self.wallet_to_cluster.items() if c in self.black)

    def drain_new_watches(self) -> list[str]:
        out, self._new_watches = self._new_watches, []
        return out

    def drain_finished(self) -> list[str]:
        out, self._finished = self._finished, []
        return out

    def set_check(self, mint: str, ok: bool, reason: str = "", supply: float | None = None) -> None:
        w = self.watches.get(mint)
        if w:
            w.check_ok, w.check_reason, w.supply = ok, reason, supply

    def today_pnl(self, ts: float) -> float:
        return self.realized_by_day[_day(ts)]

    # -- events ----------------------------------------------------------------

    def on_event(self, ev: MarketEvent) -> list[Action]:
        cid = self.wallet_to_cluster.get(ev.wallet)
        w = self.watches.get(ev.mint)
        if w is None:
            if not (cid in self.black and ev.kind in ("create", "buy") and ev.mint not in self._seen):
                return []
            if len(self.watches) >= self.cfg.max_watches:
                return []
            w = Watch(ev.mint, cid, ev.ts, creator=ev.wallet if ev.kind == "create" else "")
            self.watches[ev.mint] = w
            self._seen.add(ev.mint)
            self._new_watches.append(ev.mint)
            log.info("watch %s: cluster %s %s by %s", ev.mint, cid, ev.kind, ev.wallet)

        if ev.price > 0:
            w.last_price = ev.price
            w.trades_seen += 1
            if ev.price > w.high:
                w.high = w.low_after_high = ev.price
            else:
                w.low_after_high = min(w.low_after_high, ev.price)
        if cid == w.cluster_id:
            if ev.kind in ("create", "buy"):
                w.cluster_bought += ev.token_amount
                w.cluster_sol_in += ev.sol_amount
            elif ev.kind == "sell":
                w.cluster_sold += ev.token_amount

        pos = self.positions.get(ev.mint)
        return self._exits(w, pos, ev.ts) if pos else self._entry(w, ev.ts)

    def on_tick(self, now: float) -> list[Action]:
        """Time-based exits / expiries when no trades arrive."""
        actions: list[Action] = []
        for mint, w in list(self.watches.items()):
            pos = self.positions.get(mint)
            if pos:
                actions += self._exits(w, pos, now)
            elif not w.pending and now - w.started_ts > self.cfg.entry_window_s:
                self._finish(w, "entry window expired")
        return actions

    # -- decisions -------------------------------------------------------------

    def _entry(self, w: Watch, now: float) -> list[Action]:
        cfg = self.cfg
        if w.pending:
            return []
        if now - w.started_ts > cfg.entry_window_s:
            return self._finish(w, "entry window expired")
        if w.cluster_sold_frac >= cfg.abort_if_cluster_sold_pct:
            return self._finish(w, f"bundle already sold {w.cluster_sold_frac:.0%}")
        if cfg.require_token_checks:
            if w.check_ok is None:
                return []
            if not w.check_ok:
                return self._finish(w, f"token check failed: {w.check_reason}")
        if w.supply and w.cluster_bought / w.supply > cfg.max_cluster_supply_pct:
            return self._finish(w, f"bundle holds {w.cluster_bought / w.supply:.0%} of supply")
        if len(self.positions) >= cfg.max_open_positions or w.last_price <= 0:
            return []
        if self.today_pnl(now) <= -cfg.daily_loss_limit_sol:
            return []
        avg = w.cluster_avg_price
        if avg and w.last_price > avg * cfg.max_entry_vs_bundle:
            return []  # too far above the bundle's entry: that is buying the high
        if cfg.mode == "snipe":
            reason = "snipe"
        else:
            if w.trades_seen < cfg.min_trades_before_entry:
                return []
            drawdown = 1 - w.last_price / w.high
            bounce = w.last_price / w.low_after_high - 1
            if drawdown < cfg.dip_pct or bounce < cfg.rebound_pct:
                return []
            reason = f"dip {drawdown:.0%} off high, +{bounce:.0%} off low"
        w.pending = True
        return [Action("buy", w.mint, w.last_price, reason, w.cluster_id, sol_amount=cfg.buy_sol)]

    def _exits(self, w: Watch, pos: Position, now: float) -> list[Action]:
        if pos.closing:
            return []
        cfg, p = self.cfg, w.last_price
        pos.high = max(pos.high, p)
        sold_since = (w.cluster_sold - pos.cluster_sold_at_entry) / w.cluster_bought if w.cluster_bought else 0.0
        reason = None
        if sold_since >= cfg.exit_on_cluster_sell_pct:
            reason = f"bundle selling ({sold_since:.0%} of its bag since entry)"
        elif p >= pos.entry_price * (1 + cfg.take_profit_pct):
            reason = "take profit"
        elif p <= pos.entry_price * (1 - cfg.stop_loss_pct):
            reason = "stop loss"
        elif cfg.trailing_stop_pct and pos.high > pos.entry_price and p <= pos.high * (1 - cfg.trailing_stop_pct):
            reason = "trailing stop"
        elif now - pos.entry_ts >= cfg.max_hold_s:
            reason = "max hold time"
        if not reason:
            return []
        pos.closing = True
        return [Action("sell", w.mint, p, reason, w.cluster_id, token_amount=pos.tokens)]

    def _finish(self, w: Watch, reason: str) -> list[Action]:
        w.done_reason = reason
        self.watches.pop(w.mint, None)
        self._finished.append(w.mint)
        log.info("drop %s: %s", w.mint, reason)
        return []

    # -- fills -----------------------------------------------------------------

    def on_buy_filled(
        self, mint: str, price: float, tokens: float, sol_spent: float, ts: float, reason: str,
        position_id: int | None = None,
    ) -> Position:
        w = self.watches[mint]
        w.pending = False
        pos = Position(mint, w.cluster_id, price, ts, sol_spent, tokens, price, w.cluster_sold, reason, position_id)
        self.positions[mint] = pos
        return pos

    def on_buy_failed(self, mint: str) -> None:
        w = self.watches.get(mint)
        if w:
            w.pending = False
            self._finish(w, "buy failed")

    def on_sell_filled(self, mint: str, price: float, sol_received: float, ts: float, reason: str) -> float:
        pos = self.positions.pop(mint)
        pnl = sol_received - pos.size_sol
        self.realized_by_day[_day(ts)] += pnl
        self.closed.append(
            {
                "mint": mint,
                "cluster_id": pos.cluster_id,
                "entry_price": pos.entry_price,
                "exit_price": price,
                "size_sol": pos.size_sol,
                "pnl_sol": pnl,
                "held_s": ts - pos.entry_ts,
                "reason_open": pos.reason_open,
                "reason_close": reason,
            }
        )
        w = self.watches.get(mint)
        if w:
            self._finish(w, f"closed: {reason}")
        return pnl

    def on_sell_failed(self, mint: str) -> None:
        pos = self.positions.get(mint)
        if pos:
            pos.closing = False  # retry on the next event / tick
