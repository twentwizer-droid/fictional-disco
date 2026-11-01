"""Group wallets into bundler clusters and score each cluster's track record.

Links (union-find edges), strongest first:
  * funding: wallet <- funder, unless the funder is a hub (CEX hot wallet, faucet...)
  * token transfers wallet -> wallet without a SOL leg (consolidating the bag)
  * repeated co-bundling: two groups in the same launch bundle >= N times with
    high overlap (a sniper bot appears next to everybody: low overlap)

Per cluster we answer the three questions: WHICH tokens they launch/bundle,
WHEN (launch hours, how fast they sell) and WITH WHOM (other clusters that
show up in the same launches) - plus whether they are in the black.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations

from .config import ClusterConfig, SelectionConfig
from .models import Trade
from .storage import Store

LAUNCH_ROLES = ("creator", "bundle")


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class LaunchStat:
    mint: str
    created_at: int | None
    sol_in: float = 0.0
    sol_out: float = 0.0
    tokens_bought: float = 0.0
    tokens_sold: float = 0.0
    first_buy_price: float | None = None
    first_sell_at: int | None = None
    exit_at: int | None = None
    closed: bool = False
    unrealized_sol: float = 0.0

    @property
    def pnl(self) -> float:
        return self.sol_out - self.sol_in

    @property
    def avg_entry(self) -> float:
        return self.sol_in / self.tokens_bought if self.tokens_bought else 0.0


@dataclass
class ClusterStat:
    cluster_id: str
    wallets: list[str]
    launches: list[LaunchStat] = field(default_factory=list)
    created: int = 0
    other_mints: int = 0  # tokens they traded but did not launch/bundle
    active_hours: Counter = field(default_factory=Counter)
    partners: Counter = field(default_factory=Counter)

    @property
    def closed(self) -> list[LaunchStat]:
        return [ls for ls in self.launches if ls.closed]

    @property
    def realized_pnl(self) -> float:
        return sum(ls.pnl for ls in self.closed)

    @property
    def unrealized_pnl(self) -> float:
        return sum(ls.pnl + ls.unrealized_sol for ls in self.launches if not ls.closed)

    @property
    def win_rate(self) -> float:
        closed = self.closed
        return sum(ls.pnl > 0 for ls in closed) / len(closed) if closed else 0.0

    @property
    def median_first_sell_s(self) -> float | None:
        xs = [ls.first_sell_at - ls.created_at for ls in self.launches if ls.first_sell_at and ls.created_at]
        return statistics.median(xs) if xs else None

    @property
    def median_exit_s(self) -> float | None:
        xs = [ls.exit_at - ls.created_at for ls in self.launches if ls.exit_at and ls.created_at]
        return statistics.median(xs) if xs else None

    @property
    def avg_bundle_sol(self) -> float:
        return statistics.mean(ls.sol_in for ls in self.launches) if self.launches else 0.0

    def black_reasons(self, sel: SelectionConfig) -> list[str]:
        """Empty list == cluster is 'in the black' and worth copying."""
        reasons = []
        if len(self.launches) < sel.min_launches:
            reasons.append(f"launches {len(self.launches)} < {sel.min_launches}")
        if len(self.closed) < sel.min_closed_launches:
            reasons.append(f"closed {len(self.closed)} < {sel.min_closed_launches}")
        if self.win_rate < sel.min_win_rate:
            reasons.append(f"win rate {self.win_rate:.0%} < {sel.min_win_rate:.0%}")
        if self.realized_pnl < sel.min_total_pnl_sol:
            reasons.append(f"pnl {self.realized_pnl:.2f} < {sel.min_total_pnl_sol}")
        mfs = self.median_first_sell_s
        if mfs is not None and mfs < sel.min_median_first_sell_s:
            reasons.append(f"dumps after {mfs:.0f}s median (too fast to follow)")
        if len(self.wallets) > sel.max_wallets:
            reasons.append(f"{len(self.wallets)} wallets (over-merged?)")
        return reasons

    def to_dict(self, sel: SelectionConfig | None = None) -> dict[str, object]:
        d: dict[str, object] = {
            "cluster_id": self.cluster_id,
            "wallets": self.wallets,
            "launches": len(self.launches),
            "created": self.created,
            "closed": len(self.closed),
            "win_rate": round(self.win_rate, 3),
            "realized_pnl_sol": round(self.realized_pnl, 4),
            "unrealized_pnl_sol": round(self.unrealized_pnl, 4),
            "avg_bundle_sol": round(self.avg_bundle_sol, 4),
            "median_first_sell_s": self.median_first_sell_s,
            "median_exit_s": self.median_exit_s,
            "active_hours_utc": dict(sorted(self.active_hours.items())),
            "partners": dict(self.partners.most_common(10)),
            "tokens": [
                {
                    "mint": ls.mint,
                    "created_at": ls.created_at,
                    "sol_in": round(ls.sol_in, 4),
                    "sol_out": round(ls.sol_out, 4),
                    "pnl": round(ls.pnl, 4),
                    "closed": ls.closed,
                    "first_sell_after_s": (ls.first_sell_at - ls.created_at)
                    if ls.first_sell_at and ls.created_at
                    else None,
                }
                for ls in sorted(self.launches, key=lambda x: x.created_at or 0)
            ],
        }
        if sel is not None:
            reasons = self.black_reasons(sel)
            d["in_the_black"] = not reasons
            d["rejected_because"] = reasons
        return d


@dataclass
class ClusterIndex:
    wallet_to_cluster: dict[str, str]
    clusters: dict[str, ClusterStat]

    def black(self, sel: SelectionConfig) -> list[ClusterStat]:
        out = [c for c in self.clusters.values() if not c.black_reasons(sel)]
        return sorted(out, key=lambda c: c.realized_pnl, reverse=True)


def build_clusters(store: Store, cfg: ClusterConfig, mints: list[str] | None = None) -> ClusterIndex:
    roles = store.roles(mints)
    tokens = {t["mint"]: t for t in store.tokens()}
    launch_wallets: dict[str, set[str]] = defaultdict(set)
    all_role_wallets: dict[str, set[str]] = defaultdict(set)  # mint -> every wallet with a role
    for mint, wallet, role in roles:
        all_role_wallets[mint].add(wallet)
        if role in LAUNCH_ROLES:
            launch_wallets[mint].add(wallet)
    interesting = set().union(*launch_wallets.values()) if launch_wallets else set()

    uf = UnionFind()
    for w in interesting:
        uf.find(w)

    # 1. funding links (skip hubs: exchanges / services funding many unrelated wallets)
    funders = store.funders()
    funded_count = Counter(funders.values())
    hubs = {f for f, n in funded_count.items() if n > cfg.hub_funder_max_wallets} | set(cfg.ignore_funders)
    for wallet, funder in funders.items():
        if funder not in hubs and (wallet in interesting or funder in interesting):
            uf.union(wallet, funder)
    # fresh wallets funded by a node already in a group (e.g. the mother wallet) join it:
    # that is how a brand-new dev wallet is recognised before its first launch is analyzed
    for wallet, funder in funders.items():
        if funder not in hubs and funder in uf.parent:
            uf.union(wallet, funder)

    # 2. token transfers between wallets (no SOL leg)
    inbound = Counter(dst for _, _, dst in store.transfers())
    for _, src, dst in store.transfers():
        if (src in interesting or dst in interesting) and inbound[dst] <= cfg.hub_funder_max_wallets:
            uf.union(src, dst)

    # 3. repeated co-bundling, measured between the groups formed so far (so fresh dev
    #    wallets tied together by their funder still match the bundle wallets they reuse).
    #    A real partner shows up *together* with the group (high Jaccard overlap); a
    #    sniper bot shows up next to everybody (low overlap). Repeat until stable.
    for _ in range(3):
        pair_count: Counter = Counter()
        launches_per_group: Counter = Counter()
        for ws in launch_wallets.values():
            groups = sorted({uf.find(w) for w in ws})
            launches_per_group.update(groups)
            pair_count.update(combinations(groups, 2))
        merged = False
        for (a, b), n in pair_count.items():
            jaccard = n / (launches_per_group[a] + launches_per_group[b] - n)
            if n >= cfg.min_co_launches and jaccard >= cfg.min_co_jaccard and uf.find(a) != uf.find(b):
                uf.union(a, b)
                merged = True
        if not merged:
            break

    members: dict[str, list[str]] = defaultdict(list)
    for w in uf.parent:
        members[uf.find(w)].append(w)
    wallet_to_cluster: dict[str, str] = {}
    clusters: dict[str, ClusterStat] = {}
    for ws in members.values():
        if not set(ws) & interesting:
            continue
        cid = "C-" + min(ws)[:8]
        clusters[cid] = ClusterStat(cid, sorted(ws))
        for w in ws:
            wallet_to_cluster[w] = cid

    _score(store, clusters, wallet_to_cluster, launch_wallets, all_role_wallets, tokens, cfg)
    return ClusterIndex(wallet_to_cluster, clusters)


def _score(
    store: Store,
    clusters: dict[str, ClusterStat],
    wallet_to_cluster: dict[str, str],
    launch_wallets: dict[str, set[str]],
    all_role_wallets: dict[str, set[str]],
    tokens: dict[str, dict],
    cfg: ClusterConfig,
) -> None:
    trades_by_cluster_mint: dict[tuple[str, str], list[Trade]] = defaultdict(list)
    mints_by_cluster: dict[str, set[str]] = defaultdict(set)
    for t in store.trades(wallets=list(wallet_to_cluster)):
        cid = wallet_to_cluster[t.wallet]
        trades_by_cluster_mint[(cid, t.mint)].append(t)
        mints_by_cluster[cid].add(t.mint)
    last_price = store.last_prices()

    launches_by_cluster: dict[str, set[str]] = defaultdict(set)
    for mint, ws in launch_wallets.items():
        for w in ws:
            launches_by_cluster[wallet_to_cluster[w]].add(mint)

    for cid, c in clusters.items():
        for mint in launches_by_cluster[cid]:
            tok = tokens.get(mint) or {}
            created_at = tok.get("create_time")
            ls = LaunchStat(mint, int(created_at) if created_at else None)
            if tok.get("creator") in c.wallets:
                c.created += 1
            for t in trades_by_cluster_mint.get((cid, mint), []):
                if t.side == "buy":
                    ls.sol_in += -t.cash_flow
                    ls.tokens_bought += t.token_amount
                    if ls.first_buy_price is None:
                        ls.first_buy_price = t.price
                else:
                    ls.sol_out += t.cash_flow
                    ls.tokens_sold += t.token_amount
                    if ls.first_sell_at is None:
                        ls.first_sell_at = t.block_time
                    if ls.exit_at is None and ls.tokens_bought and ls.tokens_sold >= cfg.closed_sold_frac * ls.tokens_bought:
                        ls.exit_at = t.block_time
            ls.closed = bool(ls.tokens_bought) and ls.tokens_sold >= cfg.closed_sold_frac * ls.tokens_bought
            if not ls.closed:
                ls.unrealized_sol = max(ls.tokens_bought - ls.tokens_sold, 0.0) * last_price.get(mint, 0.0)
            c.launches.append(ls)
            if ls.created_at:
                c.active_hours[datetime.fromtimestamp(ls.created_at, timezone.utc).hour] += 1
            for other in all_role_wallets.get(mint, ()):
                ocid = wallet_to_cluster.get(other)
                if ocid and ocid != cid:
                    c.partners[ocid] += 1
        c.other_mints = len(mints_by_cluster[cid] - launches_by_cluster[cid])
