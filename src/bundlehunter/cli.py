"""Command line interface: `bh <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

from .analyzer import Analyzer, LaunchReport
from .backtest import run_backtest
from .clusters import build_clusters
from .config import Config, load_config
from .execution import LiveBroker, PaperBroker, load_keypair
from .feeds import PumpPortalFeed
from .clusters import ClusterIndex
from .holders import LABELS, HolderScanner, TokenPanel, format_panel
from .larp import LarpChecker, format_report
from .narrative import HEADER, NarrativeAnalyzer, family_to_dict, format_family, member_line, parse_query
from .rpc import SolanaRpc
from .runner import BotRunner
from .storage import Store
from .strategy import Strategy


def _rpc(cfg: Config) -> SolanaRpc:
    return SolanaRpc(cfg.rpc.url, cfg.rpc.max_concurrency, cfg.rpc.requests_per_second, cfg.rpc.timeout_s)


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M") if t else "-"


def _print_launch(r: LaunchReport) -> None:
    if r.status == "error":
        print(f"{r.mint}: ERROR {r.note}")
        return
    pct = f"{r.bundle_supply_pct:.1%}" if r.bundle_supply_pct is not None else "?"
    print(
        f"{r.mint}: creator {r.creator} slot {r.create_slot} ({_ts(r.create_time)} UTC)\n"
        f"  bundle: {len(r.bundle_wallets)} wallets + creator, {r.bundle_sol:.3f} SOL, {pct} of supply; "
        f"snipers: {len(r.sniper_wallets)}"
    )
    for w in r.bundle_wallets:
        print(f"    bundle {w}")


async def cmd_analyze(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    rpc = _rpc(cfg)
    try:
        an = Analyzer(rpc, store, cfg.analyzer)
        for mint in args.mints:
            _print_launch(await an.analyze_launch(mint, profile=not args.no_profile, full=args.full))
    finally:
        await rpc.close()


async def cmd_seed(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    rpc = _rpc(cfg)
    try:
        an = Analyzer(rpc, store, cfg.analyzer)
        for wallet in args.wallets:
            print(f"seeding from {wallet} ...")
            for r in await an.seed_wallet(wallet, args.max_launches):
                _print_launch(r)
    finally:
        await rpc.close()


async def cmd_scan(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    if args.min_age is not None:
        cfg.analyzer.min_age_s = args.min_age
    rpc = _rpc(cfg)
    try:
        results = await Analyzer(rpc, store, cfg.analyzer).process_pending(args.limit)
        analyzed = [r for r in results if isinstance(r, LaunchReport)]
        print(f"processed {len(results)} pending launches, deep-analyzed {len(analyzed)}")
        for r in analyzed:
            _print_launch(r)
    finally:
        await rpc.close()


async def _load_panel(
    cfg: Config, store: Store, rpc: SolanaRpc, an: Analyzer, args: argparse.Namespace
) -> tuple[TokenPanel | None, ClusterIndex, str]:
    """Analyze the launch if needed, rebuild clusters, compute the holder panel."""
    note = ""
    tok = store.token(args.mint)
    if not args.db_only and (args.refresh or not tok or tok["status"] not in ("ok", "partial")):
        report = await an.analyze_launch(args.mint, profile=not args.fast)
        if report.status == "error":
            note = report.note
    index = build_clusters(store, cfg.clusters)
    try:
        panel = await HolderScanner(rpc, store, an, cfg.clusters).panel(
            args.mint, index.wallet_to_cluster, live=not args.db_only, analyze=False
        )
    except ValueError as exc:
        return None, index, note or str(exc)
    return panel, index, note


async def cmd_token(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    """Axiom-style panel: Top 10 H. / Dev H. / Snipers H. / Insiders / Bundlers."""
    rpc = _rpc(cfg)
    try:
        panel, index, note = await _load_panel(cfg, store, rpc, Analyzer(rpc, store, cfg.analyzer), args)
    finally:
        await rpc.close()
    if panel is None:
        sys.exit(f"{args.mint}: {note}")

    blocked = Strategy(cfg.strategy, {}, set()).panel_block(panel.metrics())
    cluster = index.clusters.get(panel.cluster_id or "")
    if args.json:
        out = {
            "mint": panel.mint,
            "creator": panel.creator,
            "supply": panel.supply,
            "source": panel.source,
            "metrics": panel.metrics(),
            "groups": {
                g: {"wallets": sorted(grp.wallets), "held": grp.held, "initial": grp.initial}
                for g, grp in panel.groups.items()
            },
            "top10": panel.top10,
            "dev_cluster": cluster.to_dict(cfg.selection) if cluster else None,
            "entry_filters": blocked or "ok",
        }
        print(json.dumps(out, indent=2))
        return
    print(format_panel(panel))
    print()
    if cluster:
        reasons = cluster.black_reasons(cfg.selection)
        print(
            f"Dev cluster {cluster.cluster_id}: {len(cluster.wallets)} wallets, {len(cluster.launches)} launches, "
            f"win {cluster.win_rate:.0%}, pnl {cluster.realized_pnl:+.2f} SOL -> "
            + ("IN THE BLACK (copied)" if not reasons else "not copied: " + "; ".join(reasons))
        )
    print(f"Entry filters: {'blocked - ' + blocked if blocked else 'pass'}")
    if args.wallets:
        funders = store.funders_of(sorted(panel.holdings) or sorted(set().union(*(g.wallets for g in panel.groups.values()))))
        for g in ("dev", "bundlers", "snipers", "insiders"):
            grp = panel.groups[g]
            if not grp.wallets:
                continue
            print(f"\n{LABELS[g]}:")
            for w in sorted(grp.wallets, key=lambda x: -panel.holdings.get(x, 0.0)):
                held = panel.pct(panel.holdings.get(w, 0.0))
                print(f"  {w}  held {held:.2%}  funder={funders.get(w, '?')}" if held is not None else f"  {w}")
        if panel.top10:
            print("\nTop 10 holders (excl. curve/pools/burn):")
            for owner, amount in panel.top10:
                tags = [LABELS[g] for g in ("dev", "bundlers", "snipers", "insiders") if owner in panel.groups[g].wallets]
                print(f"  {owner}  {panel.pct(amount):.2%}  {', '.join(tags)}")


async def cmd_check(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    """LARP check: narrative, socials, copycats, website, holders, dev history (+ optional AI verdict)."""
    rpc = _rpc(cfg)
    live = not args.db_only
    an = Analyzer(rpc, store, cfg.analyzer)
    larp = LarpChecker(rpc if live else None, store, cfg.larp, an)
    try:
        panel, index, note = (None, None, "") if args.no_panel else await _load_panel(cfg, store, rpc, an, args)
        cluster = index.clusters.get(panel.cluster_id or "") if (panel and index) else None
        report = await larp.check(args.mint, panel, cluster, live=live, ai=args.ai)
    finally:
        await larp.close()
        await rpc.close()
    if args.json:
        out = report.to_dict()
        out["holder_panel"] = panel.metrics() if panel else None
        print(json.dumps(out, indent=2, default=str))
        return
    if panel:
        print(format_panel(panel))
        print()
    elif note and not args.no_panel:
        print(f"(no holder panel: {note})\n")
    print(format_report(report))


async def cmd_narrative(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    """Narrative + bundle analysis: every token riding a narrative, original vs copies, bundle state."""
    q = parse_query(args.query)
    if not (q.terms or q.twitter or q.website or q.seed_mint):
        sys.exit("give a narrative: words, an X / website link, or a token address")
    live = not args.db_only
    rpc = _rpc(cfg)
    an = Analyzer(rpc, store, cfg.analyzer)
    larp = LarpChecker(rpc if live else None, store, cfg.larp, an)
    na = NarrativeAnalyzer(cfg, store, larp, HolderScanner(rpc, store, an, cfg.clusters))
    try:
        if args.watch:
            print(f'watching new pump.fun launches for "{q.label}" (Ctrl+C to stop)\n{HEADER}')

            def show(m, family_size: int) -> None:
                target = "  <-- COPY-TRADE TARGET" if m.cluster_black and m.narrative_label not in ("LARP", "COPY") else ""
                print(member_line(family_size, m) + target, flush=True)

            await na.watch(q, PumpPortalFeed(cfg.feed.pumpportal_ws), show)
            return
        members = await na.find(q, use_dex=live and not args.no_dex, live=live)
        if not members:
            print(f'no tokens found for "{q.label}". Run `bh run` for a while to record launches, '
                  "or drop --no-dex / --db-only to search DexScreener too.")
            return
        await na.analyze(members, args.limit, live=live)
        pick = na.pick(members)
    finally:
        await na.close()
        await larp.close()
        await rpc.close()
    if args.json:
        print(json.dumps(family_to_dict(q, members, pick), indent=2, default=str))
    else:
        print(format_family(q, members, pick))


def cmd_clusters(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    index = build_clusters(store, cfg.clusters)
    clusters = list(index.clusters.values())
    if not args.all:
        clusters = [c for c in clusters if len(c.launches) >= 2 or c.created]
    clusters.sort(key=lambda c: c.realized_pnl, reverse=True)
    clusters = clusters[: args.top]
    if args.json:
        with open(args.json, "w") as fh:
            json.dump([c.to_dict(cfg.selection) for c in clusters], fh, indent=2)
        print(f"wrote {len(clusters)} clusters to {args.json}")
    hdr = f"{'cluster':12} {'wallets':>7} {'launches':>8} {'closed':>6} {'win%':>5} {'pnl SOL':>9} {'1st sell':>8} {'exit':>7}  black"
    print(hdr)
    print("-" * len(hdr))
    for c in clusters:
        mfs, mex = c.median_first_sell_s, c.median_exit_s
        black = "YES" if not c.black_reasons(cfg.selection) else "-"
        print(
            f"{c.cluster_id:12} {len(c.wallets):>7} {len(c.launches):>8} {len(c.closed):>6} {c.win_rate:>5.0%} "
            f"{c.realized_pnl:>9.2f} {(f'{mfs:.0f}s' if mfs is not None else '-'):>8} "
            f"{(f'{mex:.0f}s' if mex is not None else '-'):>7}  {black}"
        )
    print(f"\n{len(index.black(cfg.selection))} of {len(index.clusters)} clusters are in the black (see [selection] in config)")


def cmd_cluster(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    index = build_clusters(store, cfg.clusters)
    cid = index.wallet_to_cluster.get(args.id, args.id)
    c = index.clusters.get(cid)
    if not c:
        sys.exit(f"no cluster or wallet {args.id!r}")
    d = c.to_dict(cfg.selection)
    if args.json:
        print(json.dumps(d, indent=2))
        return
    funders = store.funders()
    print(f"{cid}: {len(c.wallets)} wallets, {len(c.launches)} launches ({c.created} created by them)")
    print(f"  in the black: {d['in_the_black']}  {'; '.join(d['rejected_because']) if d['rejected_because'] else ''}")
    print(f"  realized {c.realized_pnl:+.3f} SOL, open {c.unrealized_pnl:+.3f} SOL, win rate {c.win_rate:.0%}")
    print(f"  WHEN: launch hours UTC {dict(sorted(c.active_hours.items()))}; "
          f"median first sell {c.median_first_sell_s}s, median exit {c.median_exit_s}s")
    print(f"  WITH WHOM: partner clusters {dict(c.partners.most_common(5))}")
    print("  WALLETS:")
    for w in c.wallets:
        print(f"    {w}  funder={funders.get(w, '?')}")
    print("  WHICH TOKENS:")
    for t in d["tokens"]:  # type: ignore[union-attr]
        print(
            f"    {_ts(t['created_at'])}  {t['mint']}  in {t['sol_in']:.3f}  out {t['sol_out']:.3f}  "
            f"pnl {t['pnl']:+.3f}  {'closed' if t['closed'] else 'open'}  first sell +{t['first_sell_after_s']}s"
        )


def cmd_backtest(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    if args.mode:
        cfg.strategy.mode = args.mode
    res = run_backtest(store, cfg, args.split)
    print(
        f"train launches {res.train_launches}, test launches {res.test_launches}, "
        f"clusters in the black (train) {res.black_clusters}, watched {res.watched}"
    )
    for t in res.trades:
        print(
            f"  {t['mint']}  {t['cluster_id']}  pnl {float(t['pnl_sol']):+.4f} SOL  held {float(t['held_s']):.0f}s  "
            f"in: {t['reason_open']}  out: {t['reason_close']}"
        )
    print(f"trades {len(res.trades)}, win rate {res.win_rate:.0%}, total pnl {res.pnl:+.4f} SOL")


async def cmd_run(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    trade_rpc = _rpc(cfg)
    analysis_rpc = _rpc(cfg)
    if args.live:
        if os.environ.get("BH_LIVE_TRADING") != "1":
            sys.exit("refusing to trade live: set BH_LIVE_TRADING=1 as well as --live (start with small buy_sol)")
        broker = LiveBroker(cfg.execution, trade_rpc, load_keypair())
        bal = await trade_rpc.get_balance(broker.pubkey) / 1e9
        logging.warning("LIVE TRADING from %s (balance %.4f SOL), buy_sol=%s", broker.pubkey, bal, cfg.strategy.buy_sol)
    else:
        broker = PaperBroker(cfg.execution.paper_slippage_pct, cfg.execution.paper_fee_sol)
        logging.info("paper trading (no real orders). Use --live to trade for real.")
    analyzer = None if args.no_analyze else Analyzer(analysis_rpc, store, cfg.analyzer)
    # the holder panel and LARP check are on the trading path, so they use the trading RPC client
    trade_analyzer = Analyzer(trade_rpc, store, cfg.analyzer)
    scanner = HolderScanner(trade_rpc, store, trade_analyzer, cfg.clusters)
    larp = LarpChecker(trade_rpc, store, cfg.larp, trade_analyzer)
    runner = BotRunner(
        cfg, store, trade_rpc, broker, PumpPortalFeed(cfg.feed.pumpportal_ws), analyzer, scanner, larp
    )
    try:
        await runner.run()
    finally:
        await larp.close()
        await trade_rpc.close()
        await analysis_rpc.close()


def cmd_positions(cfg: Config, store: Store, args: argparse.Namespace) -> None:
    rows = store.positions()
    total = 0.0
    for p in rows:
        total += p["pnl_sol"] or 0
        status = f"pnl {p['pnl_sol']:+.4f}" if p["closed_at"] else "OPEN"
        print(
            f"#{p['id']} {'LIVE ' if p['live'] else 'paper'} {_ts(p['opened_at'])} {p['mint']} {p['cluster_id']} "
            f"{p['size_sol']:.3f} SOL {status}  [{p['reason_open']} -> {p['reason_close'] or ''}]"
        )
    print(f"{len(rows)} positions, realized pnl {total:+.4f} SOL")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bh", description="Solana memecoin bundle analyzer and copy-trader")
    p.add_argument("-c", "--config", help="TOML config file (default: ./config.toml if present)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("analyze", help="find the bundle of one or more launches and profile its wallets")
    s.add_argument("mints", nargs="+")
    s.add_argument("--full", action="store_true", help="also store the token's early trade history (for backtests)")
    s.add_argument("--no-profile", action="store_true", help="skip wallet profiling (faster, no funders)")

    s = sub.add_parser("seed", help="start from known bundler wallets and analyze their launches")
    s.add_argument("wallets", nargs="+")
    s.add_argument("--max-launches", type=int, default=20)

    s = sub.add_parser("scan", help="process launches recorded by `bh run` (prefilter + analyze)")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--min-age", type=float, default=None, help="seconds since launch (default from config)")

    s = sub.add_parser("token", help="Axiom-style holder panel: top 10 / dev / snipers / insiders / bundlers")
    s.add_argument("mint")
    s.add_argument("--wallets", action="store_true", help="list the wallets behind every number")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help="re-analyze the launch first")
    s.add_argument("--fast", action="store_true", help="skip full wallet profiles (funders via quick lookups only)")
    s.add_argument("--db-only", action="store_true", help="no RPC: holdings estimated from stored trades")

    s = sub.add_parser("check", help="LARP check: narrative, socials, copycats, website, holders, dev history")
    s.add_argument("mint")
    s.add_argument("--ai", action="store_true", help="add a Claude verdict on the narrative (needs ANTHROPIC_API_KEY)")
    s.add_argument("--json", action="store_true")
    s.add_argument("--refresh", action="store_true", help="re-analyze the launch first")
    s.add_argument("--fast", action="store_true", help="skip full wallet profiles")
    s.add_argument("--no-panel", action="store_true", help="skip the holder panel (no launch analysis)")
    s.add_argument("--db-only", action="store_true", help="no network: only what is stored")

    s = sub.add_parser("narrative", help="narrative + bundle analysis: all tokens of a narrative, original vs copies")
    s.add_argument("query", help='words ("trump cat"), an X / website link, or a token address')
    s.add_argument("--limit", type=int, default=None, help="tokens to analyze in depth (default [narrative] max_analyze)")
    s.add_argument("--watch", action="store_true", help="stay live and report every new launch that joins the narrative")
    s.add_argument("--no-dex", action="store_true", help="only your recorded launches, no DexScreener search")
    s.add_argument("--db-only", action="store_true", help="no network at all")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("clusters", help="rank bundler clusters by realized PnL")
    s.add_argument("--top", type=int, default=30)
    s.add_argument("--all", action="store_true", help="include single-launch clusters")
    s.add_argument("--json", help="write full cluster details to this file")

    s = sub.add_parser("cluster", help="details for one cluster (id or any wallet in it)")
    s.add_argument("id")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("backtest", help="walk-forward backtest of the strategy on stored launches")
    s.add_argument("--split", type=float, default=0.7)
    s.add_argument("--mode", choices=["dip", "snipe"])

    s = sub.add_parser("run", help="live loop: record launches, auto-analyze, copy-trade (paper by default)")
    s.add_argument("--live", action="store_true", help="send real transactions (also needs BH_LIVE_TRADING=1)")
    s.add_argument("--no-analyze", action="store_true", help="do not analyze recorded launches in the background")

    sub.add_parser("positions", help="list the bot's paper/live positions")
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    handlers = {
        "analyze": cmd_analyze,
        "seed": cmd_seed,
        "scan": cmd_scan,
        "token": cmd_token,
        "check": cmd_check,
        "narrative": cmd_narrative,
        "clusters": cmd_clusters,
        "cluster": cmd_cluster,
        "backtest": cmd_backtest,
        "run": cmd_run,
        "positions": cmd_positions,
    }
    try:
        result = handlers[args.cmd](cfg, store, args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except KeyboardInterrupt:
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
