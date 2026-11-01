"""Walk-forward backtest on analyzed launches stored in the database.

Clusters are scored on the oldest `split` share of launches only, then the
strategy is replayed on the newer launches - so a cluster is never selected
using the very launches it is being tested on.

The replay can only see trades that were fetched. Analyze launches with
`bh analyze --full` to store the whole early price path; otherwise the price
path is just the bundle/sniper buys plus the bundle wallets' later sells.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .clusters import build_clusters
from .config import Config
from .execution import PaperBroker
from .models import MarketEvent
from .storage import Store
from .strategy import Strategy


@dataclass
class BacktestResult:
    train_launches: int
    test_launches: int
    black_clusters: int
    watched: int
    trades: list[dict[str, object]] = field(default_factory=list)

    @property
    def pnl(self) -> float:
        return sum(float(t["pnl_sol"]) for t in self.trades)

    @property
    def win_rate(self) -> float:
        return sum(float(t["pnl_sol"]) > 0 for t in self.trades) / len(self.trades) if self.trades else 0.0


def run_backtest(store: Store, cfg: Config, split: float = 0.7) -> BacktestResult:
    launches = [t for t in store.tokens(["ok", "partial"]) if t.get("create_time")]
    launches.sort(key=lambda t: t["create_time"])
    n_train = int(len(launches) * split)
    train, test = launches[:n_train], launches[n_train:]

    index = build_clusters(store, cfg.clusters, mints=[t["mint"] for t in train])
    black = {c.cluster_id for c in index.black(cfg.selection)}
    strat = Strategy(cfg.strategy, index.wallet_to_cluster, black)
    broker = PaperBroker(cfg.execution.paper_slippage_pct, cfg.execution.paper_fee_sol)
    supply = {t["mint"]: t.get("supply") for t in test}

    events: list[MarketEvent] = []
    for tok in test:
        events.append(MarketEvent("create", tok["mint"], tok.get("creator") or "", float(tok["create_time"]), 0, 0, 0))
    for t in store.trades(mints=[tok["mint"] for tok in test]):
        if t.block_time is None:
            continue
        events.append(
            MarketEvent(t.side, t.mint, t.wallet, float(t.block_time), t.sol_amount, t.token_amount, t.price, t.signature)
        )
    # stable order: by time, creates first within a second
    events.sort(key=lambda e: (e.ts, e.kind != "create"))

    watched = 0
    last_price: dict[str, float] = {}

    def execute(actions, ts: float) -> None:
        for a in actions:
            if a.side == "buy":
                f = broker.fill_buy(a.mint, a.sol_amount, a.ref_price, ts)
                strat.on_buy_filled(a.mint, f.price, f.tokens, f.sol, ts, a.reason)
            else:
                f = broker.fill_sell(a.mint, a.token_amount, a.ref_price, ts)
                strat.on_sell_filled(a.mint, f.price, f.sol, ts, a.reason)

    for ev in events:
        if ev.price > 0:
            last_price[ev.mint] = ev.price
        execute(strat.on_tick(ev.ts), ev.ts)
        actions = strat.on_event(ev)
        for mint in strat.drain_new_watches():
            watched += 1
            # pump.fun revokes mint/freeze authority at create; like live, the entry can
            # only happen from the next event on (the check takes an RPC round trip)
            strat.set_check(mint, True, supply=supply.get(mint))
        strat.drain_finished()
        execute(actions, ev.ts)

    end = events[-1].ts if events else 0.0
    for mint, pos in list(strat.positions.items()):
        f = broker.fill_sell(mint, pos.tokens, last_price.get(mint, pos.entry_price), end)
        strat.on_sell_filled(mint, f.price, f.sol, end, "end of data")

    return BacktestResult(len(train), len(test), len(black), watched, strat.closed)
