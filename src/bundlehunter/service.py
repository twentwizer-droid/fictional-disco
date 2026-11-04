"""One service object behind the Telegram bot and the web panel.

Interactive requests (token check, narrative) run the same analysis as the CLI, but:
  * wallets get quick funder lookups instead of full profiles (seconds, not minutes);
    the launch stays 'pending' so the background pipeline profiles it fully later;
  * at most 2 run at once, and results are cached for a minute, to protect your RPC plan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .analyzer import Analyzer
from .clusters import ClusterIndex, ClusterStat, build_clusters
from .config import Config
from .holders import HolderScanner, TokenPanel
from .i18n import ua_reason, verdict_ua
from .larp import SOL_ADDRESS, LarpChecker, LarpReport
from .models import MarketEvent
from .narrative import Member, NarrativeAnalyzer, Query, family_to_dict, parse_query
from .rpc import SolanaRpc
from .storage import Store
from .traders import SignalEngine, TraderStats

if TYPE_CHECKING:
    from .runner import BotRunner

log = logging.getLogger(__name__)

MemberCallback = Callable[[Member, int], Awaitable[None]]


@dataclass
class CheckResult:
    mint: str
    panel: TokenPanel | None
    report: LarpReport
    cluster: ClusterStat | None
    cluster_black: bool
    note: str = ""

    def to_dict(self, cfg: Config) -> dict[str, Any]:
        p = self.panel
        return {
            "mint": self.mint,
            "larp": {**self.report.to_dict(), "verdict_ua": verdict_ua(self.report.score)},
            "panel": None if p is None else {
                "symbol": p.symbol,
                "creator": p.creator,
                "create_time": p.create_time,
                "source": p.source,
                "metrics": p.metrics(),
                "groups": {g: {"wallets": len(x.wallets), "held_pct": p.pct(x.held), "initial_pct": p.pct(x.initial)}
                           for g, x in p.groups.items()},
                "top10": [{"owner": o, "pct": p.pct(a)} for o, a in (p.top10 or [])],
            },
            "dev_cluster": self.cluster.to_dict(cfg.selection) if self.cluster else None,
            "dev_cluster_in_the_black": self.cluster_black,
            "note": self.note,
        }


@dataclass
class NarrativeResult:
    query: Query
    members: list[Member]
    pick: tuple[Member | None, str]

    def to_dict(self) -> dict[str, Any]:
        return family_to_dict(self.query, self.members, self.pick)


@dataclass
class NarrativeWatch:
    query: Query
    subscribers: dict[str, MemberCallback] = field(default_factory=dict)
    since: float = field(default_factory=time.time)


class Service:
    MAX_WATCHES_PER_SUBSCRIBER = 5

    def __init__(self, cfg: Config, store: Store, rpc: SolanaRpc, runner: "BotRunner | None" = None) -> None:
        self.cfg, self.store, self.rpc, self.runner = cfg, store, rpc, runner
        self.analyzer = Analyzer(rpc, store, cfg.analyzer)
        self.scanner = HolderScanner(rpc, store, self.analyzer, cfg.clusters)
        self.larp = LarpChecker(rpc, store, cfg.larp, self.analyzer)
        self.narrative = NarrativeAnalyzer(cfg, store, self.larp, self.scanner)
        self.started_at = time.time()
        self._sem = asyncio.Semaphore(2)
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}
        self._index: tuple[float, ClusterIndex] | None = None
        self.watches: dict[str, NarrativeWatch] = {}
        self._tasks: set[asyncio.Task] = set()
        self.signals = SignalEngine(cfg, store, self)  # .send is set by the Telegram bot
        if runner is not None:
            runner.create_listeners.append(self._on_create)
            runner.trade_listeners.append(self.signals.on_trade)
            runner.watch_listeners.append(self.signals.on_bundle_watch)
            runner.extra_accounts |= set(self.signals.traders)

    async def close(self) -> None:
        await self.narrative.close()
        await self.larp.close()

    # -- helpers ---------------------------------------------------------------------

    def index(self, max_age_s: float = 60.0) -> ClusterIndex:
        if self._index is None or time.time() - self._index[0] > max_age_s:
            self._index = (time.time(), build_clusters(self.store, self.cfg.clusters))
        return self._index[1]

    def _cached(self, key: tuple[Any, ...], ttl: float) -> Any:
        hit = self._cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < ttl else None

    def _remember(self, key: tuple[Any, ...], value: Any) -> Any:
        if len(self._cache) > 500:
            self._cache.clear()
        self._cache[key] = (time.time(), value)
        return value

    @staticmethod
    def valid_mint(mint: str) -> str:
        mint = mint.strip()
        if not SOL_ADDRESS.fullmatch(mint):
            raise ValueError("це не схоже на адресу Solana")
        return mint

    # -- requests --------------------------------------------------------------------

    async def check(self, mint: str, ai: bool = False) -> CheckResult:
        mint = self.valid_mint(mint)
        cached = self._cached(("check", mint, ai), 60)
        if cached:
            return cached
        async with self._sem:
            index = self.index()
            panel, note = None, ""
            try:
                panel = await self.scanner.panel(
                    mint, index.wallet_to_cluster, live=True, analyze=True, profile=False, final=False
                )
            except Exception as exc:  # not a pump.fun-style launch, history too long, RPC error...
                note = f"немає аналізу бандла: {exc}"
            index = self.index(max_age_s=0)  # the analysis may have added roles and funders
            cid = index.wallet_to_cluster.get(panel.creator or "") if panel else None
            cluster = index.clusters.get(cid) if cid else None
            black = bool(cluster and not cluster.black_reasons(self.cfg.selection))
            report = await self.larp.check(mint, panel=panel, cluster=cluster, live=True, ai=ai)
        return self._remember(("check", mint, ai), CheckResult(mint, panel, report, cluster, black, note))

    async def narrative_search(self, text: str, limit: int | None = None) -> NarrativeResult:
        q = parse_query(text)
        if not (q.terms or q.twitter or q.website or q.seed_mint):
            raise ValueError("напиши наратив: слова, посилання на X / сайт або адресу токена")
        key = ("narrative", q.raw.lower(), limit)
        cached = self._cached(key, 120)
        if cached:
            return cached
        async with self._sem:
            members = await self.narrative.find(q)
            if members:
                await self.narrative.analyze(members, limit)
            pick = self.narrative.pick(members, lang="ua") if members else (None, "нічого не знайдено")
        return self._remember(key, NarrativeResult(q, members, pick))

    def clusters(self, black_only: bool = True, top: int = 20) -> list[ClusterStat]:
        index = self.index()
        if black_only:
            return index.black(self.cfg.selection)[:top]
        rows = [c for c in index.clusters.values() if len(c.launches) >= 2 or c.created]
        return sorted(rows, key=lambda c: c.realized_pnl, reverse=True)[:top]

    def cluster(self, key: str) -> ClusterStat | None:
        index = self.index()
        key = key.strip()
        return index.clusters.get(index.wallet_to_cluster.get(key, key))

    def positions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.store.positions()
        for r in rows:
            tok = self.store.token(r["mint"]) or {}
            r["symbol"] = tok.get("symbol") or ""
            r["reason_open_ua"] = ua_reason(r["reason_open"])
            r["reason_close_ua"] = ua_reason(r["reason_close"])
        return list(reversed(rows))[:limit]

    def status(self) -> dict[str, Any]:
        index = self.index()
        rows = self.store.positions()
        realized = {"paper": 0.0, "live": 0.0}
        for r in rows:
            if r["closed_at"]:
                realized["live" if r["live"] else "paper"] += r["pnl_sol"] or 0.0
        out: dict[str, Any] = {
            "uptime_s": time.time() - self.started_at,
            "mode": "off",
            "tokens": self.store.token_counts(),
            "wallets": self.store.wallet_count(),
            "clusters": len(index.clusters),
            "black_clusters": len(index.black(self.cfg.selection)),
            "positions_open": sum(1 for r in rows if not r["closed_at"]),
            "positions_closed": sum(1 for r in rows if r["closed_at"]),
            "realized_pnl_sol": realized,
            "narrative_watches": [w.query.raw for w in self.watches.values()],
            "traders": len(self.signals.traders),
            "signals_sent": self.signals.sent,
            "watching": [],
            "tracked_wallets": 0,
        }
        if self.runner is not None:
            strat = self.runner.strategy
            out["mode"] = "live" if self.runner.broker.live else "paper"
            out["tracked_wallets"] = len(strat.tracked_wallets())
            out["watching"] = [
                {
                    "mint": w.mint,
                    "symbol": (self.store.token(w.mint) or {}).get("symbol") or "",
                    "cluster_id": w.cluster_id,
                    "age_s": time.time() - w.started_ts,
                    "blocked": w.block_reason,
                    "blocked_ua": ua_reason(w.block_reason),
                    "in_position": w.mint in strat.positions,
                }
                for w in strat.watches.values()
            ]
        return out

    # -- your traders ---------------------------------------------------------------

    async def add_trader(self, address: str, label: str = "") -> TraderStats:
        """Add to the watchlist, start streaming its trades, load its history for PnL stats."""
        address = self.valid_mint(address)  # same base58 shape as a mint
        self.store.add_trader(address, label.strip()[:40])
        self.signals.load()
        if self.runner is not None:
            self.runner.extra_accounts.add(address)
            await self.runner.refresh_accounts()
        async with self._sem:
            await self.analyzer.profile_wallet(address, max_txs=self.cfg.signals.trader_history_txs)
        return self.signals.stats(address, max_age_s=0)

    async def remove_trader(self, address: str) -> bool:
        ok = self.store.remove_trader(address.strip())
        self.signals.load()
        if self.runner is not None:
            self.runner.extra_accounts.discard(address.strip())
            await self.runner.refresh_accounts()
        return ok

    def traders(self) -> list[TraderStats]:
        rows = [self.signals.stats(t["address"]) for t in self.store.traders()]
        return sorted(rows, key=lambda t: t.realized_pnl, reverse=True)

    async def trader(self, address: str) -> TraderStats:
        address = self.valid_mint(address)
        if not self.store.wallet(address):
            async with self._sem:
                await self.analyzer.profile_wallet(address, max_txs=self.cfg.signals.trader_history_txs)
        st = self.signals.stats(address, max_age_s=0)
        st.label = st.label or next((t["label"] for t in self.store.traders() if t["address"] == address), "") or ""
        return st

    # -- live narrative watches (fed by the runner's create events) ------------------

    def add_watch(self, text: str, subscriber: str, callback: MemberCallback) -> Query:
        q = parse_query(text)
        if not (q.terms or q.twitter or q.website):
            raise ValueError("для стеження потрібні слова або посилання на X / сайт")
        mine = [k for k, w in self.watches.items() if subscriber in w.subscribers]
        if len(mine) >= self.MAX_WATCHES_PER_SUBSCRIBER:
            raise ValueError(f"максимум {self.MAX_WATCHES_PER_SUBSCRIBER} стежень, зупини якесь через /unwatch")
        key = q.raw.lower()
        self.watches.setdefault(key, NarrativeWatch(q)).subscribers[subscriber] = callback
        return q

    def remove_watches(self, subscriber: str) -> int:
        removed = 0
        for key in list(self.watches):
            if self.watches[key].subscribers.pop(subscriber, None):
                removed += 1
            if not self.watches[key].subscribers:
                del self.watches[key]
        return removed

    def watches_of(self, subscriber: str) -> list[str]:
        return [w.query.raw for w in self.watches.values() if subscriber in w.subscribers]

    def _on_create(self, ev: MarketEvent) -> None:
        for key, w in list(self.watches.items()):
            q = w.query
            if q.terms and not (q.twitter or q.website) and not self.narrative.matches_create(q, ev):
                continue  # cheap name / ticker filter before spawning anything

            async def deliver(member: Member, family: int, key: str = key) -> None:
                watch = self.watches.get(key)
                for cb in list(watch.subscribers.values()) if watch else []:
                    try:
                        await cb(member, family)
                    except Exception:
                        log.exception("watch callback failed")

            task = asyncio.create_task(self.narrative.on_create(q, ev, deliver))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
