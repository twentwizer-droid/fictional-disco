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
from typing import Awaitable, Callable

from .analyzer import Analyzer
from .clusters import build_clusters
from .config import Config
from .constants import LAMPORTS_PER_SOL
from .execution import Broker
from .feeds import PumpPortalFeed
from .holders import HolderScanner, TokenPanel, format_panel
from .i18n import ua_reason
from .larp import LarpChecker
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
        scanner: HolderScanner | None = None,
        larp: LarpChecker | None = None,
    ) -> None:
        self.cfg, self.store, self.rpc, self.broker, self.feed, self.analyzer = (
            cfg, store, trade_rpc, broker, feed, analyzer,
        )
        if cfg.strategy.require_holder_panel and scanner is None:
            raise ValueError("strategy.require_holder_panel needs a HolderScanner")
        self.scanner = scanner
        if cfg.strategy.require_larp_check and larp is None:
            raise ValueError("strategy.require_larp_check needs a LarpChecker")
        self.larp = larp
        self._meta_pending = 0
        self.notify: Callable[[str], Awaitable[None]] | None = None  # e.g. Telegram: trades, watches
        self.create_listeners: list[Callable[[MarketEvent], None]] = []  # e.g. narrative watches
        self.trade_listeners: list[Callable[[MarketEvent], None]] = []  # e.g. trader signals
        self.watch_listeners: list[Callable[[str, str], None]] = []  # (mint, cluster_id): bundle signals
        self.extra_accounts: set[str] = set()  # wallets to stream besides the clusters (your traders)
        self.started_at = time.time()
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
        await self.refresh_accounts()
        log.info(
            "clusters: %d total, %d in the black, tracking %d wallets",
            len(index.clusters), len(black), len(self.strategy.tracked_wallets()),
        )

    async def refresh_accounts(self) -> None:
        await self.feed.set_accounts(set(self.strategy.tracked_wallets()) | self.extra_accounts)

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
                for listener in self.create_listeners:
                    try:
                        listener(ev)
                    except Exception:
                        log.exception("create listener failed")
                if self.cfg.feed.fetch_metadata and self.larp and ev.uri and self._meta_pending < 200:
                    self._spawn(self._record_metadata(ev.mint, ev.uri))
                if self.cfg.feed.funder_lookup_on_create and ev.wallet not in self.strategy.wallet_to_cluster:
                    self._spawn(self._funder_lookup(ev))
            else:
                for listener in self.trade_listeners:
                    try:
                        listener(ev)
                    except Exception:
                        log.exception("trade listener failed")
            self._handle(ev)

    def _token_name(self, mint: str) -> str:
        tok = self.store.token(mint) or {}
        return f"{tok.get('symbol') or '?'} {mint[:6]}…{mint[-4:]}"

    def _say(self, text: str) -> None:
        if self.notify and self.cfg.telegram.notify_trades:
            self._spawn(self.notify(text))

    async def _record_metadata(self, mint: str, uri: str) -> None:
        """Keep every launch's socials so the LARP check can spot copies of them later."""
        assert self.larp is not None
        self._meta_pending += 1
        try:
            await self.larp.store_offchain(mint, uri)
        except Exception as exc:
            log.debug("metadata %s: %s", mint, exc)
        finally:
            self._meta_pending -= 1

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
                w = self.strategy.watches.get(mint)
                for listener in self.watch_listeners:
                    try:
                        listener(mint, w.cluster_id if w else "")
                    except Exception:
                        log.exception("watch listener failed")
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
        """Mint + freeze authority must be revoked, then the Axiom-style holder panel."""
        acct = None
        for _ in range(5):
            try:
                acct = await self.rpc.get_parsed_account(mint)
            except Exception as exc:
                log.debug("check %s: %s", mint, exc)
            if acct:
                break
            await asyncio.sleep(0.5)
        if not acct:
            self.strategy.set_check(mint, False, "mint account not found")
            return
        info = acct["data"]["parsed"]["info"]
        mint_auth, freeze = info.get("mintAuthority"), info.get("freezeAuthority")
        supply = int(info["supply"]) / 10 ** int(info["decimals"])
        if mint_auth is not None or freeze is not None:
            self.strategy.set_check(mint, False, f"mintAuthority={mint_auth} freezeAuthority={freeze}", supply)
            return
        use_panel = bool(self.scanner and self.cfg.strategy.require_holder_panel)
        use_larp = bool(self.larp and self.cfg.strategy.require_larp_check)
        if not (use_panel or use_larp):
            self.strategy.set_check(mint, True, "", supply)
            return

        async def panel_job() -> TokenPanel:
            assert self.scanner is not None
            await asyncio.sleep(self.cfg.strategy.panel_delay_s)
            return await self.scanner.panel(
                mint, self.strategy.wallet_to_cluster, live=True, profile=False, final=False,
                token_program=acct.get("owner"), supply=supply,
            )

        # holder panel and LARP check run in parallel
        panel_res, larp_res = await asyncio.gather(
            panel_job() if use_panel else asyncio.sleep(0, None),
            self.larp.check(mint, live=True) if use_larp and self.larp else asyncio.sleep(0, None),
            return_exceptions=True,
        )
        if isinstance(panel_res, BaseException):
            self.strategy.set_check(mint, False, f"holder panel failed: {panel_res}", supply)
            return
        metrics: dict[str, float | None] = panel_res.metrics() if panel_res else {}
        if use_larp:
            if isinstance(larp_res, BaseException):
                log.warning("LARP check %s failed: %s", mint, larp_res)
                metrics["larp_score"] = None  # best effort: a failed check does not block
            elif larp_res is not None:
                metrics["larp_score"] = larp_res.score
                log.info("larp %s: score %d (%s)", mint, larp_res.score, larp_res.verdict)
        if panel_res:
            log.info("panel %s", format_panel(panel_res, compact=True))
            self._spawn(self._refresh_panel(mint, panel_res))
        self.strategy.set_check(mint, True, "", supply, metrics)

    async def _refresh_panel(self, mint: str, panel: TokenPanel) -> None:
        assert self.scanner is not None
        while mint in self.strategy.watches:
            await asyncio.sleep(self.cfg.strategy.panel_refresh_s)
            if mint not in self.strategy.watches:
                return
            try:
                panel = await self.scanner.refresh(panel)
                self.strategy.update_metrics(mint, panel.metrics())
                log.debug("panel %s", format_panel(panel, compact=True))
            except Exception as exc:
                log.debug("panel refresh %s: %s", mint, exc)

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
                self._say(
                    f"🟢 КУПІВЛЯ{'' if self.broker.live else ' (paper)'} {self._token_name(a.mint)}\n"
                    f"{fill.sol:.4f} SOL @ {fill.price:.3e}\nпричина: {ua_reason(a.reason)}\nгрупа: {a.cluster_id}"
                )
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
                size = pos.size_sol if pos else 0.0
                pct = f" ({pnl / size:+.0%})" if size else ""
                self._say(
                    f"{'✅' if pnl >= 0 else '🔻'} ПРОДАЖ{'' if self.broker.live else ' (paper)'} {self._token_name(a.mint)}\n"
                    f"отримано {fill.sol:.4f} SOL, PnL {pnl:+.4f} SOL{pct}\nпричина: {ua_reason(a.reason)}"
                )
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
