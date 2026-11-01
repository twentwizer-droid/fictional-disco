"""Axiom-style holder panel for one token: Top 10 H., Dev H., Snipers H., Insiders, Bundlers.

Our definitions. They are transparent approximations, not Axiom's exact (unpublished) formulas:

  Dev       the creator wallet.
  Bundlers  wallets that bought in the create slot (inside the dev's Jito bundle), or in an
            early slot where >= `bundle_min_wallets_per_slot` wallets bought at once.
  Snipers   every other wallet that bought in the launch window (create slot + `early_slots`).
  Insiders  wallets other than the dev that are linked to the dev:
              - same cluster as the dev (funding / transfers / repeated co-bundling), or
              - funded by the dev, by a bundler, or by the dev's own funder, or
              - the dev's funder itself, or
              - received this token by transfer from the dev / bundlers / other insiders
                (the "distribution" wallets bundlers use to make holders look organic).
  Top 10    the 10 largest holders, excluding the bonding curve, AMM pools and lockers
            (program-owned, off-curve addresses) and the burn address.

Every figure is "% of total supply held now". Dev, bundlers, snipers and insiders also show
"% at launch" (bought in the launch window; for insiders, plus tokens received from linked
wallets). Categories can overlap (a bundle wallet funded by the dev is a bundler and an
insider). `linked` = dev + bundlers + insiders without double counting. When that number
falls, the people behind the launch are selling.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable

from .config import ClusterConfig
from .models import Trade
from .storage import Store

if TYPE_CHECKING:
    from .analyzer import Analyzer
    from .rpc import SolanaRpc

BURN_ADDRESSES = frozenset({"1nc1nerator11111111111111111111111111111111", "11111111111111111111111111111111"})
GROUPS = ("dev", "bundlers", "snipers", "insiders", "linked")
LABELS = {"dev": "Dev H.", "snipers": "Snipers H.", "insiders": "Insiders", "bundlers": "Bundlers", "linked": "Linked (all)"}


@dataclass
class Group:
    wallets: set[str] = field(default_factory=set)
    initial: float = 0.0  # tokens bought in the launch window (+ received from linked wallets, for insiders)
    held: float | None = None  # tokens held now


@dataclass
class TokenPanel:
    mint: str
    supply: float | None
    creator: str | None
    create_time: float | None
    groups: dict[str, Group]
    holdings: dict[str, float]
    top10: list[tuple[str, float]] | None
    source: str  # "rpc" (live balances) | "db" (estimated from stored trades)
    symbol: str = ""
    cluster_id: str | None = None
    token_program: str | None = None
    updated_at: float = field(default_factory=time.time)

    def pct(self, amount: float | None) -> float | None:
        return amount / self.supply if self.supply and amount is not None else None

    def metrics(self) -> dict[str, float | None]:
        m = {f"{g}_pct": self.pct(self.groups[g].held) for g in GROUPS}
        m["top10_pct"] = self.pct(sum(a for _, a in self.top10)) if self.top10 is not None else None
        return m


def classify(
    roles: dict[str, str],
    trades: list[Trade],
    transfers: list[tuple[str, str, float, int]],
    funders: dict[str, str],
    hubs: set[str],
    wallet_to_cluster: dict[str, str],
    window_end_slot: int | None = None,
) -> dict[str, Group]:
    """Pure function: put every wallet that touched the token into the panel groups."""
    creator = next((w for w, r in roles.items() if r == "creator"), None)
    groups = {g: Group() for g in GROUPS}
    if creator:
        groups["dev"].wallets.add(creator)
    groups["bundlers"].wallets = {w for w, r in roles.items() if r == "bundle"}
    groups["snipers"].wallets = {w for w, r in roles.items() if r == "sniper"}
    core = groups["dev"].wallets | groups["bundlers"].wallets

    dev_cluster = wallet_to_cluster.get(creator) if creator else None
    dev_funder = funders.get(creator) if creator else None
    if dev_funder in hubs:
        dev_funder = None

    def linked(w: str) -> bool:
        if w == creator:
            return False
        if dev_cluster and wallet_to_cluster.get(w) == dev_cluster:
            return True
        if dev_funder and w == dev_funder:
            return True
        f = funders.get(w)
        return bool(f) and f not in hubs and (f in core or f == dev_funder)

    candidates = {t.wallet for t in trades} | {d for _, d, _, _ in transfers} | set(roles)
    insiders = {w for w in candidates if linked(w)}
    received: dict[str, float] = defaultdict(float)
    sources = core | insiders
    for _ in range(5):  # follow the distribution chain a few hops
        grew = False
        for src, dst, _amount, _slot in transfers:
            if src in sources and dst != creator:
                if dst not in sources:
                    sources.add(dst)
                    insiders.add(dst)
                    grew = True
        if not grew:
            break
    for src, dst, amount, _ in transfers:
        if src in sources and dst in insiders:
            received[dst] += amount
    groups["insiders"].wallets = insiders
    groups["linked"].wallets = core | insiders

    window_buys: dict[str, float] = defaultdict(float)
    for t in trades:
        if t.side == "buy" and (window_end_slot is None or t.slot <= window_end_slot):
            window_buys[t.wallet] += t.token_amount
    for g in groups.values():
        g.initial = sum(window_buys.get(w, 0.0) for w in g.wallets)
    # insiders also count what they were handed; for "linked" those are internal moves (no double count)
    groups["insiders"].initial += sum(received.get(w, 0.0) for w in insiders)
    return groups


def db_holdings(trades: Iterable[Trade], transfers: Iterable[tuple[str, str, float, int]]) -> dict[str, float]:
    """Holdings estimated from stored trades and transfers (only as complete as the data)."""
    bal: dict[str, float] = defaultdict(float)
    for t in trades:
        bal[t.wallet] += t.token_amount if t.side == "buy" else -t.token_amount
    for src, dst, amount, _ in transfers:
        bal[src] -= amount
        bal[dst] += amount
    return {w: max(v, 0.0) for w, v in bal.items()}


def _is_wallet(owner: str) -> bool:
    """On-curve = a normal keypair wallet; off-curve = PDA (bonding curve, AMM pool, locker...)."""
    if owner in BURN_ADDRESSES:
        return False
    try:
        from solders.pubkey import Pubkey

        return Pubkey.from_string(owner).is_on_curve()
    except Exception:
        return True


class HolderScanner:
    def __init__(self, rpc: "SolanaRpc", store: Store, analyzer: "Analyzer", cluster_cfg: ClusterConfig) -> None:
        self.rpc, self.store, self.analyzer, self.cluster_cfg = rpc, store, analyzer, cluster_cfg

    async def mint_info(self, mint: str) -> tuple[str | None, float | None]:
        acct = await self.rpc.get_parsed_account(mint)
        if not acct:
            return None, None
        info = acct["data"]["parsed"]["info"]
        return acct.get("owner"), int(info["supply"]) / 10 ** int(info["decimals"])

    async def rpc_holdings(self, mint: str, wallets: Iterable[str], token_program: str | None) -> dict[str, float]:
        from solders.pubkey import Pubkey
        from solders.token.associated import get_associated_token_address

        program = Pubkey.from_string(token_program) if token_program else None
        mint_pk = Pubkey.from_string(mint)
        ata_to_wallet: dict[str, str] = {}
        for w in wallets:
            try:
                ata_to_wallet[str(get_associated_token_address(Pubkey.from_string(w), mint_pk, program))] = w
            except Exception:
                continue  # not a valid address
        atas = list(ata_to_wallet)
        out = {w: 0.0 for w in ata_to_wallet.values()}
        for ata, acct in zip(atas, await self.rpc.get_multiple_accounts(atas), strict=False):
            try:
                out[ata_to_wallet[ata]] = float(acct["data"]["parsed"]["info"]["tokenAmount"]["uiAmountString"])
            except (TypeError, KeyError, ValueError):
                pass  # no account = holds nothing
        return out

    async def top_holders(self, mint: str, n: int = 10) -> list[tuple[str, float]]:
        largest = await self.rpc.get_token_largest_accounts(mint)
        accounts = await self.rpc.get_multiple_accounts([a["address"] for a in largest])
        by_owner: dict[str, float] = defaultdict(float)
        for row, acct in zip(largest, accounts, strict=False):
            try:
                owner = acct["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                continue
            if _is_wallet(owner):
                by_owner[owner] += float(row.get("uiAmountString") or 0)
        return sorted(by_owner.items(), key=lambda kv: kv[1], reverse=True)[:n]

    async def panel(
        self,
        mint: str,
        wallet_to_cluster: dict[str, str],
        live: bool = True,
        analyze: bool = True,
        profile: bool = True,
        final: bool = True,
        token_program: str | None = None,
        supply: float | None = None,
    ) -> TokenPanel:
        tok = self.store.token(mint)
        roles = {w: r for _, w, r in self.store.roles([mint])}
        if analyze and (not roles or not tok or tok["status"] not in ("ok", "partial")):
            await self.analyzer.analyze_launch(mint, profile=profile, final=final)
            tok = self.store.token(mint)
            roles = {w: r for _, w, r in self.store.roles([mint])}
        if not roles:
            raise ValueError(f"{mint}: launch not analyzed (run `bh analyze {mint}`)")

        trades = self.store.trades(mints=[mint])
        transfers = self.store.transfers_for_mint(mint)
        if live:  # cheap funder lookups, dev first, then bundlers, then snipers
            order = {"creator": 0, "bundle": 1, "sniper": 2}
            await self.analyzer.quick_funders(sorted(roles, key=lambda w: order.get(roles[w], 3)))
        candidates = set(roles) | {t.wallet for t in trades} | {d for _, d, _, _ in transfers}
        funders = self.store.funders_of(sorted(candidates))
        counts = self.store.funded_counts(sorted(set(funders.values())))
        hubs = {f for f, n in counts.items() if n > self.cluster_cfg.hub_funder_max_wallets}
        hubs |= set(self.cluster_cfg.ignore_funders)

        create_slot = (tok or {}).get("create_slot")
        window_end = max((t.slot for t in trades if t.wallet in roles and t.side == "buy"), default=None)
        if create_slot is not None and window_end is not None:
            window_end = max(window_end, create_slot)
        groups = classify(roles, trades, transfers, funders, hubs, wallet_to_cluster, window_end)
        creator = next(iter(groups["dev"].wallets), None)
        p = TokenPanel(
            mint=mint,
            supply=supply or (tok or {}).get("supply"),
            creator=creator,
            create_time=(tok or {}).get("create_time"),
            groups=groups,
            holdings={},
            top10=None,
            source="db",
            symbol=(tok or {}).get("symbol") or "",
            cluster_id=wallet_to_cluster.get(creator) if creator else None,
            token_program=token_program,
        )
        if live:
            return await self.refresh(p)
        p.holdings = db_holdings(trades, transfers)
        self._fill(p)
        return p

    async def refresh(self, p: TokenPanel) -> TokenPanel:
        """Live balances + top 10 (3-4 RPC calls); group membership stays as computed."""
        if p.token_program is None or p.supply is None:
            program, supply = await self.mint_info(p.mint)
            p.token_program = p.token_program or program
            p.supply = p.supply or supply
        everyone = set().union(*(g.wallets for g in p.groups.values()))
        p.holdings = await self.rpc_holdings(p.mint, everyone, p.token_program)
        p.top10 = await self.top_holders(p.mint)
        p.source, p.updated_at = "rpc", time.time()
        self._fill(p)
        return p

    @staticmethod
    def _fill(p: TokenPanel) -> None:
        for g in p.groups.values():
            g.held = sum(p.holdings.get(w, 0.0) for w in g.wallets)


def _fmt_pct(x: float | None) -> str:
    return f"{x:6.1%}" if x is not None else "     -"


def format_panel(p: TokenPanel, compact: bool = False) -> str:
    m = p.metrics()
    if compact:
        return (
            f"{p.symbol or p.mint[:8]}: top10 {_fmt_pct(m['top10_pct']).strip()} | dev {_fmt_pct(m['dev_pct']).strip()} | "
            f"snipers {_fmt_pct(m['snipers_pct']).strip()} | insiders {_fmt_pct(m['insiders_pct']).strip()} | "
            f"bundlers {_fmt_pct(m['bundlers_pct']).strip()} ({p.source})"
        )
    age = f"{(time.time() - p.create_time) / 60:.0f} min ago" if p.create_time else "?"
    lines = [
        f"{p.symbol + ' ' if p.symbol else ''}{p.mint}",
        f"created {age} by {p.creator}   holdings: {'live (RPC)' if p.source == 'rpc' else 'estimated from stored trades'}",
        "",
        f"{'':14}{'wallets':>8}{'held now':>10}{'at launch':>11}",
    ]
    if p.top10 is not None:
        lines.append(f"{'Top 10 H.':14}{len(p.top10):>8}{_fmt_pct(m['top10_pct']):>10}{'':>11}")
    for g in ("dev", "snipers", "insiders", "bundlers", "linked"):
        grp = p.groups[g]
        lines.append(f"{LABELS[g]:14}{len(grp.wallets):>8}{_fmt_pct(p.pct(grp.held)):>10}{_fmt_pct(p.pct(grp.initial)):>11}")
    return "\n".join(lines)
