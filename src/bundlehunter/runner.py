"""The automatic loop: record launches -> analyze them -> rebuild clusters -> copy-trade.

One process, one websocket, one SQLite writer:
  * every pump.fun create is stored (status=pending);
  * a background worker scans pending launches once they are `min_age_s` old,
    deep-analyzes the bundled ones and profiles their wallets;
  * clusters are rebuilt periodically; wallets of clusters in the black are
    subscribed on the feed;
  * the strategy sees every relevant event and its orders go to the broker.
Analysis uses its own RPC client so it never queues in front of trading calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

from .analyzer import Analyzer
from .clusters import build_clusters
from .config import Config
from .constants import LAMPORTS_PER_SOL
from .execution import Broker
from .feeds import PumpPortalFeed
from .models import MarketEvent
from .parser import parse_transaction
from .rpc import SolanaRpc
from .storage import Store
from .strategy import Action, Strategy

log = logging.getLogger(__name__)


class BotRunner:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        trade_rpc: SolanaRpc,
        broker: Broker,
        feed: PumpPortalFeed,
        analyzer: Analyzer | None = None,
    ) -> None:
        self.cfg, self.store, self.rpc, self.broker, self.feed, self.analyzer = (
            cfg, store, trade_rpc, broker, feed, analyzer,
        )
        self.strategy = Strategy(cfg.strategy, {}, set())
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._recent_creates: OrderedDict[str, MarketEvent] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def rebuild(self) -> None:
        index = build_clusters(self.store, self.cfg.clusters)
        black = index.black(self.cfg.selection)
        self.strategy.update_clusters(index.wallet_to_cluster, {c.cluster_id for c in black})
        await self.feed.set_accounts(self.strategy.tracked_wallets())
        log.info(
            "clusters: %d total, %d in the black, tracking %d wallets",
            len(index.clusters), len(black), len(self.strategy.tracked_wallets()),
        )

    async def run(self) -> None:
        await self.rebuild()
        await self.feed.subscribe_new_tokens()
        loops = [self._consume(), self._ticker()]
        if self.analyzer:
            loops.append(self._analysis_loop())
        await asyncio.gather(*loops)

    # -- feed ------------------------------------------------------------------

    def _dedupe(self, ev: MarketEvent) -> bool:
        key = (ev.signature, ev.wallet, ev.kind)
        if ev.signature and key in self._seen:
            return False
        self._seen[key] = None
        if len(self._seen) > 50_000:
            self._seen.popitem(last=False)
        return True

    async def _consume(self) -> None:
        async for ev in self.feed.events():
            if not self._dedupe(ev):
                continue
            if ev.kind == "create":
                if self.cfg.feed.record_launches:
                    self.store.upsert_token(
                        ev.mint, creator=ev.wallet, create_time=ev.ts, name=ev.name, symbol=ev.symbol,
                        source="pumpportal",
                    )
                self._recent_creates[ev.mint] = ev
                if len(self._recent_creates) > 5000:
                    self._recent_creates.popitem(last=False)
                if self.cfg.feed.funder_lookup_on_create and ev.wallet not in self.strategy.wallet_to_cluster:
                    self._spawn(self._funder_lookup(ev))
            self._handle(ev)

    def _handle(self, ev: MarketEvent) -> None:
        for action in self.strategy.on_event(ev):
            self._spawn(self._execute(action))
        self._sync()

    def _sync(self) -> None:
        new = self.strategy.drain_new_watches()
        if new:
            self._spawn(self.feed.subscribe_tokens(new))
            for mint in new:
                self._spawn(self._check_token(mint))
        done = self.strategy.drain_finished()
        if done:
            self._spawn(self.feed.unsubscribe_tokens(done))

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(2)
            for action in self.strategy.on_tick(time.time()):
                self._spawn(self._execute(action))
            self._sync()

    # -- checks ----------------------------------------------------------------

    async def _check_token(self, mint: str) -> None:
        """Mint and freeze authority must be revoked (no infinite mint, no frozen sells)."""
        for _ in range(5):
            try:
                acct = await self.rpc.get_parsed_account(mint)
            except Exception as exc:
                log.debug("check %s: %s", mint, exc)
                acct = None
            if acct:
                info = acct["data"]["parsed"]["info"]
                mint_auth, freeze = info.get("mintAuthority"), info.get("freezeAuthority")
                supply = int(info["supply"]) / 10 ** int(info["decimals"])
                ok = mint_auth is None and freeze is None
                reason = "" if ok else f"mintAuthority={mint_auth} freezeAuthority={freeze}"
                self.strategy.set_check(mint, ok, reason, supply)
                return
            await asyncio.sleep(0.5)
        self.strategy.set_check(mint, False, "mint account not found")

    async def _funder_lookup(self, ev: MarketEvent) -> None:
        """A fresh dev wallet funded by a tracked bundler wallet = that bundler is launching."""
        try:
            sigs, complete = await self.rpc.signatures(ev.wallet, max_pages=1, page_size=50)
            if not complete:
                return
            min_lamports = int(self.cfg.analyzer.min_funding_sol * LAMPORTS_PER_SOL)
            for s in [s for s in reversed(sigs) if not s.get("err")][:3]:
                tx = await self.rpc.get_transaction(s["signature"])
                if not tx:
                    continue
                ptx = parse_transaction(tx, s["signature"])
                funder = next(
                    (t.src for t in ptx.sol_transfers if t.dst == ev.wallet and t.lamports >= min_lamports), None
                )
                if funder:
                    cid = self.strategy.wallet_to_cluster.get(funder)
                    if cid in self.strategy.black:
                        log.info("dev %s of %s funded by %s (cluster %s)", ev.wallet, ev.mint, funder, cid)
                        self.strategy.add_wallet(ev.wallet, cid)
                        self._handle(ev)
                    return
        except Exception as exc:
            log.debug("funder lookup %s: %s", ev.wallet, exc)

    # -- orders ----------------------------------------------------------------

    async def _execute(self, a: Action) -> None:
        try:
            if a.side == "buy":
                fill = await self.broker.buy(a.mint, a.sol_amount, a.ref_price)
                if not fill:
                    self.strategy.on_buy_failed(a.mint)
                    return
                pid = self.store.open_position(
                    mint=a.mint, cluster_id=a.cluster_id, live=int(self.broker.live), opened_at=fill.ts,
                    entry_price=fill.price, size_sol=fill.sol, tokens=fill.tokens, entry_sig=fill.signature,
                    reason_open=a.reason,
                )
                self.strategy.on_buy_filled(a.mint, fill.price, fill.tokens, fill.sol, fill.ts, a.reason, pid)
                log.info("BUY %s %.4f SOL @ %.3e (%s) cluster %s", a.mint, fill.sol, fill.price, a.reason, a.cluster_id)
            else:
                pos = self.strategy.positions.get(a.mint)
                fill = await self.broker.sell(a.mint, a.token_amount, a.ref_price)
                if not fill:
                    self.strategy.on_sell_failed(a.mint)
                    return
                pnl = self.strategy.on_sell_filled(a.mint, fill.price, fill.sol, fill.ts, a.reason)
                if pos and pos.position_id is not None:
                    self.store.close_position(
                        pos.position_id, closed_at=fill.ts, exit_price=fill.price, exit_sol=fill.sol,
                        exit_sig=fill.signature, reason_close=a.reason, pnl_sol=pnl,
                    )
                log.info("SELL %s -> %.4f SOL, pnl %+.4f (%s)", a.mint, fill.sol, pnl, a.reason)
        except Exception as exc:
            log.exception("order %s %s failed: %s", a.side, a.mint, exc)
            (self.strategy.on_buy_failed if a.side == "buy" else self.strategy.on_sell_failed)(a.mint)
        finally:
            self._sync()

    # -- background analysis -----------------------------------------------------

    async def _analysis_loop(self) -> None:
        assert self.analyzer is not None
        last_rebuild = time.monotonic()
        while True:
            await asyncio.sleep(self.cfg.auto.analyze_every_s)
            try:
                results = await self.analyzer.process_pending(self.cfg.auto.analyze_batch)
                if results:
                    log.info("analyzed %d pending launches", len(results))
                if time.monotonic() - last_rebuild >= self.cfg.auto.rebuild_clusters_every_s:
                    await self.rebuild()
                    last_rebuild = time.monotonic()
            except Exception:
                log.exception("analysis loop error")
