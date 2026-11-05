"""Your trader watchlist and the signal bot.

You add the wallets of traders you found (GMGN, Axiom, Cielo, your own digging). The bot:
  * pulls each trader's history and computes their real PnL per token, so you can see
    whether a wallet is actually in the plus before you trust it;
  * streams their trades live and sends a Telegram signal on every buy (and on the sell of
    a token it signalled), with a stronger signal when several of your traders buy the same
    token within `confluence_window_s`;
  * sends a signal when a bundle group that is in the black launches / buys a token;
  * follows each signal with the bundle + LARP analysis of the token (bundlers, insiders,
    top 10, dev group, red flags), so you see "who else is in" before you act.
Signals only: nothing here places orders.
"""

from __future__ import annotations

import asyncio
import html
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .config import Config
from .i18n import verdict_ua
from .models import MarketEvent
from .storage import Store

if TYPE_CHECKING:
    from .service import CheckResult, Service

log = logging.getLogger(__name__)


def e(x: Any) -> str:
    return html.escape(str(x), quote=False)


def short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if len(addr) > 10 else addr


# -- stats ------------------------------------------------------------------------------


@dataclass
class TokenResult:
    mint: str
    sol_in: float = 0.0
    sol_out: float = 0.0
    bought: float = 0.0
    sold: float = 0.0
    first_buy: float | None = None
    exit_at: float | None = None
    closed: bool = False

    @property
    def pnl(self) -> float:
        return self.sol_out - self.sol_in


@dataclass
class TraderStats:
    address: str
    label: str = ""
    tokens: list[TokenResult] = field(default_factory=list)
    last_trade: float | None = None

    @property
    def closed(self) -> list[TokenResult]:
        return [t for t in self.tokens if t.closed]

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    @property
    def win_rate(self) -> float:
        c = self.closed
        return sum(t.pnl > 0 for t in c) / len(c) if c else 0.0

    @property
    def median_hold_s(self) -> float | None:
        xs = [t.exit_at - t.first_buy for t in self.closed if t.exit_at and t.first_buy]
        return statistics.median(xs) if xs else None

    @property
    def avg_buy_sol(self) -> float:
        return statistics.mean(t.sol_in for t in self.tokens) if self.tokens else 0.0

    # -- deep metrics ---------------------------------------------------------------------

    @property
    def rois(self) -> list[float]:
        return [t.pnl / t.sol_in for t in self.closed if t.sol_in > 0]

    @property
    def avg_roi(self) -> float | None:
        return statistics.mean(self.rois) if self.rois else None

    @property
    def profit_factor(self) -> float | None:
        """Sum of wins / sum of losses: above 1.5 is a real edge, below 1 loses money."""
        wins = sum(t.pnl for t in self.closed if t.pnl > 0)
        losses = -sum(t.pnl for t in self.closed if t.pnl < 0)
        if not self.closed:
            return None
        return min(wins / losses, 99.0) if losses > 0 else (99.0 if wins > 0 else 0.0)

    @property
    def avg_win_sol(self) -> float:
        w = [t.pnl for t in self.closed if t.pnl > 0]
        return statistics.mean(w) if w else 0.0

    @property
    def avg_loss_sol(self) -> float:
        lo = [t.pnl for t in self.closed if t.pnl < 0]
        return statistics.mean(lo) if lo else 0.0

    @property
    def best(self) -> TokenResult | None:
        return max(self.closed, key=lambda t: t.pnl, default=None)

    @property
    def worst(self) -> TokenResult | None:
        return min(self.closed, key=lambda t: t.pnl, default=None)

    @property
    def trades_per_day(self) -> float:
        times = [t.first_buy for t in self.tokens if t.first_buy]
        if len(times) < 2:
            return float(len(times))
        days = max((max(times) - min(times)) / 86400, 1.0)
        return len(times) / days

    @property
    def style(self) -> str:
        h = self.median_hold_s
        if h is None:
            return "невідомо"
        if h < 60:
            return "бот / снайпер"  # out in under a minute: impossible to follow
        if h < 600:
            return "скальпер"
        if h < 6 * 3600:
            return "інтрадей"
        return "холдер"

    @property
    def score(self) -> float | None:
        """0-100: win rate 40%, profit factor 30%, realized PnL 20%, sample size 10%. None under 5 closed trades."""
        if len(self.closed) < 5:
            return None
        pf = self.profit_factor or 0.0
        return round(100 * (0.4 * self.win_rate + 0.3 * min(pf, 3.0) / 3.0
                            + 0.2 * min(max(self.realized_pnl, 0.0) / 50.0, 1.0) + 0.1 * min(len(self.closed) / 30, 1.0)), 1)

    @property
    def verdict(self) -> str:
        if len(self.closed) < 5:
            return "мало даних"
        return "в плюсі" if self.realized_pnl > 0 and self.win_rate >= 0.5 else "в мінусі"

    @property
    def name(self) -> str:
        return self.label or short(self.address)

    def line(self) -> str:
        """One-line summary for signals."""
        if not self.closed:
            return "історії ще немає"
        score = f"score {self.score:.0f} · " if self.score is not None else ""
        return (f"{score}PnL {self.realized_pnl:+.1f} SOL · {self.win_rate:.0%} в плюс · "
                f"{len(self.closed)} угод · {self.style}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address, "label": self.label, "verdict": self.verdict,
            "realized_pnl_sol": round(self.realized_pnl, 4), "win_rate": round(self.win_rate, 3),
            "tokens": len(self.tokens), "closed": len(self.closed), "median_hold_s": self.median_hold_s,
            "avg_buy_sol": round(self.avg_buy_sol, 4), "last_trade": self.last_trade,
            "score": self.score, "style": self.style, "profit_factor": self.profit_factor,
            "avg_roi": self.avg_roi, "avg_win_sol": round(self.avg_win_sol, 4), "avg_loss_sol": round(self.avg_loss_sol, 4),
            "best": {"mint": self.best.mint, "pnl": round(self.best.pnl, 4)} if self.best else None,
            "worst": {"mint": self.worst.mint, "pnl": round(self.worst.pnl, 4)} if self.worst else None,
            "trades_per_day": round(self.trades_per_day, 2),
            "recent": [
                {"mint": t.mint, "sol_in": round(t.sol_in, 4), "sol_out": round(t.sol_out, 4), "pnl": round(t.pnl, 4),
                 "closed": t.closed, "first_buy": t.first_buy}
                for t in sorted(self.tokens, key=lambda t: t.first_buy or 0, reverse=True)[:20]
            ],
        }


def trader_stats(store: Store, address: str, label: str = "", closed_frac: float = 0.9) -> TraderStats:
    """PnL per token from the stored trades of one wallet (tokens bought before the history window are skipped)."""
    per: dict[str, TokenResult] = {}
    st = TraderStats(address, label)
    for t in store.trades(wallets=[address]):
        st.last_trade = max(st.last_trade or 0, t.block_time or 0) or None
        r = per.setdefault(t.mint, TokenResult(t.mint))
        if t.side == "buy":
            r.sol_in += -t.cash_flow
            r.bought += t.token_amount
            r.first_buy = r.first_buy or t.block_time
        elif r.bought:  # a sell of something bought inside the window
            r.sol_out += t.cash_flow
            r.sold += t.token_amount
            if r.exit_at is None and r.sold >= closed_frac * r.bought:
                r.exit_at = t.block_time
    for r in per.values():
        if r.bought:
            r.closed = r.sold >= closed_frac * r.bought
            st.tokens.append(r)
    return st


# -- signals ----------------------------------------------------------------------------

Sender = Callable[[str], Awaitable[None]]


class SignalEngine:
    ANALYSIS_TTL_S = 600

    def __init__(self, cfg: Config, store: Store, service: "Service | None" = None, send: Sender | None = None) -> None:
        self.cfg, self.store, self.service, self.send = cfg, store, service, send
        self.traders: dict[str, str] = {}
        self._buys: dict[str, dict[str, float]] = defaultdict(dict)  # mint -> trader -> last buy ts
        self._analyzed: dict[str, float] = {}
        self._sold: set[tuple[str, str]] = set()
        self._stats: dict[str, tuple[float, TraderStats]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.sent = 0
        self.load()

    def load(self) -> None:
        self.traders = {t["address"]: t["label"] or "" for t in self.store.traders()}

    def stats(self, address: str, max_age_s: float = 300) -> TraderStats:
        hit = self._stats.get(address)
        if hit and time.time() - hit[0] < max_age_s:
            return hit[1]
        st = trader_stats(self.store, address, self.traders.get(address, ""), self.cfg.clusters.closed_sold_frac)
        self._stats[address] = (time.time(), st)
        return st

    def _spawn(self, coro: Awaitable[Any]) -> None:
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:  # no running loop (e.g. sync tests)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _emit(self, text: str) -> None:
        if self.send:
            self.sent += 1
            self._spawn(self.send(text))

    def _token(self, mint: str) -> str:
        sym = (self.store.token(mint) or {}).get("symbol") or "?"
        return f"<b>{e(sym)}</b> <code>{e(mint)}</code>"

    # -- inputs -------------------------------------------------------------------------

    def on_trade(self, ev: MarketEvent) -> None:
        if ev.wallet not in self.traders or ev.kind not in ("buy", "sell"):
            return
        sc = self.cfg.signals
        name = e(self.traders[ev.wallet] or short(ev.wallet))
        me = self._person(ev.wallet)
        if ev.kind == "buy":
            if not sc.trader_buys or ev.sol_amount < sc.min_trader_buy_sol:
                return
            window = self._buys[ev.mint]
            already = any(self._person(w) == me for w in window)  # this person (any of their wallets) is in
            others: dict[str, float] = {}  # other people who bought within the window -> first buy ts
            for w, ts in window.items():
                p = self._person(w)
                if p != me and ev.ts - ts <= sc.confluence_window_s:
                    others[p] = min(ts, others.get(p, ts))
            window[ev.wallet] = ev.ts
            if already:
                return  # same person adding, or buying with another of their wallets: no new signal
            lines = [
                "📡 <b>Трейдер купив</b>" if not others else f"🔥 <b>{len(others) + 1} твоїх трейдери в одному токені</b>",
                f"👤 <b>{name}</b> <code>{e(short(ev.wallet))}</code> · {e(self.stats(ev.wallet).line())}",
                f"🪙 {self._token(ev.mint)}",
                f"💰 {ev.sol_amount:.2f} SOL",
            ]
            if others:
                mins = (ev.ts - min(others.values())) / 60
                lines.append("👥 Також купили (за " + f"{mins:.0f} хв): " + ", ".join(e(p) for p in others))
            self._emit("\n".join(lines))
            self._follow_up(ev.mint)
        else:
            bought = any(self._person(w) == me for w in self._buys.get(ev.mint, {}))
            if not sc.trader_sells or not bought or (me, ev.mint) in self._sold:
                return
            self._sold.add((me, ev.mint))
            self._emit(f"🔻 <b>Трейдер продає</b>\n👤 <b>{name}</b> · {self._token(ev.mint)}\n💰 {ev.sol_amount:.2f} SOL")

    def _person(self, wallet: str) -> str:
        """Wallets with the same name are one person (people run many wallets)."""
        label = (self.traders.get(wallet) or "").strip()
        return label if label else short(wallet)

    def on_bundle_watch(self, mint: str, cluster_id: str) -> None:
        if not self.cfg.signals.bundle_signals:
            return
        lines = ["📦 <b>Прибуткова бандл-група зайшла в токен</b>", f"🪙 {self._token(mint)}"]
        c = self.service.index().clusters.get(cluster_id) if self.service and cluster_id else None
        if c:
            lines.append(f"👥 група {e(c.cluster_id)}: {len(c.launches)} запусків · {c.win_rate:.0%} в плюс · "
                         f"{c.realized_pnl:+.1f} SOL · зазвичай продає через {c.median_first_sell_s or 0:.0f} с")
        traders_in = [w for w in self._buys.get(mint, {})]
        if traders_in:
            lines.append("📡 Твої трейдери вже тут: " + ", ".join(e(self.traders.get(w) or short(w)) for w in traders_in))
        self._emit("\n".join(lines))
        self._follow_up(mint)

    # -- analysis follow-up ---------------------------------------------------------------

    def _follow_up(self, mint: str) -> None:
        if not (self.cfg.signals.analyze and self.service):
            return
        now = time.time()
        if now - self._analyzed.get(mint, 0) < self.ANALYSIS_TTL_S:
            return
        self._analyzed[mint] = now
        self._spawn(self._analyze(mint))

    async def _analyze(self, mint: str) -> None:
        assert self.service is not None
        try:
            res = await self.service.check(mint)
        except Exception as exc:
            log.warning("signal analysis %s: %s", mint, exc)
            return
        if self.send:
            await self.send(fmt_analysis(res))


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "–"


def fmt_analysis(r: "CheckResult") -> str:
    rep = r.report
    lines = [f"🔎 <b>Аналіз {e(rep.meta.symbol or '?')}</b> <code>{e(r.mint)}</code>"]
    if r.panel:
        m = r.panel.metrics()
        lines.append(f"📦 бандлери {_pct(m['bundlers_pct'])} · інсайдери {_pct(m['insiders_pct'])} · "
                     f"снайпери {_pct(m['snipers_pct'])} · топ-10 {_pct(m['top10_pct'])} · dev {_pct(m['dev_pct'])}")
    elif r.note:
        lines.append(f"<i>{e(r.note)}</i>")
    if r.cluster:
        lines.append(f"👥 група розробника {e(r.cluster.cluster_id)}: {r.cluster.realized_pnl:+.1f} SOL"
                     + (" — в плюсі ✅" if r.cluster_black else ""))
    icon = "🔴" if rep.score >= 60 else "🟡" if rep.score >= 30 else "🟢"
    lines.append(f"{icon} LARP {rep.score}/100 — {e(verdict_ua(rep.score))}")
    reds = [f for f in rep.findings if f.level == "red"][:3]
    lines += [f"   🔴 {e(f.text_ua)}" for f in reds]
    return "\n".join(lines)
