"""Narrative + bundle analysis: every token riding one narrative, and which one to look at.

Input is a narrative in any form:
  * free text          "trump cat"         -> tokens whose name / ticker / description match
  * a tweet / X link   x.com/user/status/1 -> tokens that link that tweet or account
  * a website          https://site.xyz    -> tokens that link that site
  * a token address    <MINT>              -> that token's own narrative (ticker + links)

Tokens come from your recorded launches (`bh run`) and, optionally, DexScreener search.
For the tokens that matter (the first ones and the biggest ones) it then runs:
  * the LARP check   - original, copy or LARP (copied socials, borrowed site, famous account...)
  * bundle analysis  - who bundled the launch, how much they still hold, insiders, and whether
                       the dev group is one of your clusters "in the black"
and picks the token that is the real narrative token, with the bundle situation spelled out.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from .clusters import ClusterIndex, ClusterStat, build_clusters
from .config import Config
from .larp import SOL_ADDRESS, LarpChecker, LarpReport, _ago, _short, norm_link, twitter_kind
from .models import MarketEvent
from .storage import Store

if TYPE_CHECKING:
    from .holders import HolderScanner, TokenPanel

log = logging.getLogger(__name__)


@dataclass
class Query:
    raw: str
    terms: list[str] = field(default_factory=list)
    twitter: str = ""  # normalized x.com link
    website: str = ""  # normalized site
    seed_mint: str = ""

    @property
    def label(self) -> str:
        return self.raw if len(self.raw) <= 60 else self.raw[:57] + "..."


@dataclass
class Member:
    mint: str
    name: str = ""
    symbol: str = ""
    create_time: float | None = None
    sources: set[str] = field(default_factory=set)
    twitter: str = ""
    website: str = ""
    market_cap: float | None = None  # USD, DexScreener
    volume_h24: float | None = None
    larp: LarpReport | None = None
    metrics: dict[str, float | None] | None = None  # holder panel
    linked_initial_pct: float | None = None
    linked_held_pct: float | None = None
    cluster: ClusterStat | None = None
    cluster_black: bool = False
    first: bool = False
    error: str = ""

    @property
    def narrative_label(self) -> str:
        score = self.larp.score if self.larp else None
        if score is not None and score >= 60:
            return "LARP"
        copied = bool(self.larp and any(f.code.startswith(("copycat", "copied_")) for f in self.larp.findings))
        if self.first:
            return "ORIGINAL" if score is not None and score < 30 else "FIRST, SUSPICIOUS"
        return "COPY" if copied else "SIDE TOKEN"

    @property
    def bundle_label(self) -> str:
        m = self.metrics or {}
        bundlers, insiders = m.get("bundlers_pct"), m.get("insiders_pct")
        if self.cluster_black:
            return "PROFITABLE BUNDLER"
        if (bundlers or 0) >= 0.30 or (insiders or 0) >= 0.20:
            return "HEAVY BUNDLE"
        if self.linked_initial_pct and self.linked_held_pct is not None and self.linked_held_pct < 0.3 * self.linked_initial_pct:
            return "BUNDLE SOLD"
        if bundlers is not None and bundlers < 0.10 and (insiders or 0) < 0.10:
            return "CLEAN"
        return "-" if bundlers is None else "MODERATE"


def parse_query(raw: str) -> Query:
    q = Query(raw=raw.strip())
    text = q.raw
    if SOL_ADDRESS.fullmatch(text):
        q.seed_mint = text
        return q
    if re.match(r"^(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?$", text, re.I):
        link = norm_link(text)
        if link.startswith("x.com/"):
            q.twitter = link
        else:
            q.website = link.split("/")[0]
        return q
    q.terms = [t for t in (w.strip("$#.,!?").lower() for w in text.split()) if len(t) >= 2]
    return q


def _matches_terms(terms: list[str], *texts: str) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    squashed = blob.replace(" ", "")
    return bool(terms) and all(t in blob or t in squashed for t in terms)


class NarrativeAnalyzer:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        larp: LarpChecker,
        scanner: "HolderScanner | None" = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg, self.store, self.larp, self.scanner = cfg, store, larp, scanner
        self.http = http or httpx.AsyncClient(timeout=10)

    async def close(self) -> None:
        await self.http.aclose()

    # -- finding the family ----------------------------------------------------------

    async def dexscreener(self, text: str) -> list[dict[str, Any]]:
        try:
            resp = await self.http.get(self.cfg.narrative.dexscreener_url, params={"q": text})
            resp.raise_for_status()
            pairs = resp.json().get("pairs") or []
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("dexscreener search failed: %s", exc)
            return []
        best: dict[str, dict[str, Any]] = {}
        for p in pairs:
            if p.get("chainId") != "solana":
                continue
            mint = (p.get("baseToken") or {}).get("address")
            if not mint:
                continue
            # keep the most liquid pair per token
            if mint not in best or ((p.get("liquidity") or {}).get("usd") or 0) > ((best[mint].get("liquidity") or {}).get("usd") or 0):
                best[mint] = p
        return list(best.values())

    def _remember_dex(self, p: dict[str, Any]) -> None:
        base = p.get("baseToken") or {}
        info = p.get("info") or {}
        socials = {s.get("type"): s.get("url") for s in info.get("socials") or [] if isinstance(s, dict)}
        sites = [w.get("url") for w in info.get("websites") or [] if isinstance(w, dict) and w.get("url")]
        tok = self.store.token(base["address"]) or {}
        created = (p.get("pairCreatedAt") or 0) / 1000 or None
        self.store.upsert_token(
            base["address"],
            name=tok.get("name") or base.get("name"),
            symbol=tok.get("symbol") or base.get("symbol"),
            twitter=tok.get("twitter") or norm_link(socials.get("twitter")) or None,
            telegram=tok.get("telegram") or norm_link(socials.get("telegram")) or None,
            website=tok.get("website") or (norm_link(sites[0]) if sites else None),
            image=tok.get("image") or norm_link(info.get("imageUrl")) or None,
            create_time=None if tok.get("create_time") else created,  # pair time: only if nothing better
            source=tok.get("source") or "dexscreener",
        )

    async def find(self, q: Query, use_dex: bool = True, live: bool = True) -> list[Member]:
        if q.seed_mint:
            meta = await self.larp.metadata(q.seed_mint, live=live)
            q.terms = [t for t in (meta.symbol.strip("$").lower(),) if len(t) >= 2]
            kind, _ = twitter_kind(meta.twitter) if meta.twitter else ("", None)
            if kind in ("tweet", "profile", "community"):
                q.twitter = norm_link(meta.twitter)
            site = norm_link(meta.website).split("/")[0]
            if site and site not in ("x.com", "t.me"):
                q.website = site

        dex: dict[str, dict[str, Any]] = {}
        if use_dex and self.cfg.narrative.dexscreener:
            text = " ".join(q.terms) or (q.twitter.split("/")[1] if q.twitter.count("/") >= 1 else q.website)
            for p in await self.dexscreener(text) if text else []:
                base = p.get("baseToken") or {}
                info = p.get("info") or {}
                links = [norm_link(s.get("url")) for s in info.get("socials") or [] if isinstance(s, dict)]
                links += [norm_link(w.get("url")) for w in info.get("websites") or [] if isinstance(w, dict)]
                by_terms = _matches_terms(q.terms, base.get("name", ""), base.get("symbol", ""))
                by_link = any(x and (x == q.twitter or x.split("/")[0] == q.website) for x in links)
                if by_terms or by_link:
                    dex[base["address"]] = p
                    self._remember_dex(p)

        rows: dict[str, dict[str, Any]] = {}
        for r in self.store.search_tokens(q.terms) if q.terms else []:
            rows[r["mint"]] = r
        if q.twitter:
            for r in self.store.tokens_with("twitter", q.twitter, ""):
                rows[r["mint"]] = r
        if q.website:
            for r in self.store.db.execute("SELECT * FROM tokens WHERE website = ? OR website LIKE ?",
                                           (q.website, q.website + "/%")):
                rows[r["mint"]] = dict(r)
        if q.seed_mint:
            rows.setdefault(q.seed_mint, self.store.token(q.seed_mint) or {"mint": q.seed_mint})
        for mint in dex:
            rows.setdefault(mint, self.store.token(mint) or {"mint": mint})

        members = []
        for mint, r in rows.items():
            m = Member(
                mint=mint, name=r.get("name") or "", symbol=r.get("symbol") or "", create_time=r.get("create_time"),
                twitter=r.get("twitter") or "", website=r.get("website") or "",
            )
            if r.get("source") != "dexscreener":
                m.sources.add("db")
            if mint in dex:
                m.sources.add("dexscreener")
                m.market_cap = dex[mint].get("marketCap") or dex[mint].get("fdv")
                m.volume_h24 = (dex[mint].get("volume") or {}).get("h24")
            members.append(m)
        return members

    # -- analysis ----------------------------------------------------------------------

    def _choose(self, members: list[Member], limit: int) -> list[Member]:
        """The first launches (who started it) + the biggest ones (where the money is)."""
        by_time = sorted(members, key=lambda m: m.create_time or float("inf"))
        by_size = sorted(members, key=lambda m: -(m.market_cap or 0))
        chosen: dict[str, Member] = {}
        for m in by_time[:3] + by_size + by_time[3:]:
            if len(chosen) >= limit:
                break
            chosen.setdefault(m.mint, m)
        return list(chosen.values())

    async def analyze(self, members: list[Member], limit: int | None = None, live: bool = True) -> ClusterIndex:
        limit = limit or self.cfg.narrative.max_analyze
        index = build_clusters(self.store, self.cfg.clusters)
        sem = asyncio.Semaphore(3)

        async def one(m: Member) -> None:
            async with sem:
                panel: TokenPanel | None = None
                if self.scanner is not None:
                    try:
                        panel = await self.scanner.panel(
                            m.mint, index.wallet_to_cluster, live=live, analyze=live, profile=False, final=False
                        )
                    except Exception as exc:  # not a pump.fun launch, too old, RPC error...
                        m.error = f"no bundle analysis: {exc}"
                if panel is not None:
                    m.metrics = panel.metrics()
                    linked = panel.groups["linked"]
                    m.linked_initial_pct, m.linked_held_pct = panel.pct(linked.initial), panel.pct(linked.held)
                try:
                    m.larp = await self.larp.check(m.mint, panel=panel, live=live)
                except Exception as exc:
                    m.error = (m.error + "; " if m.error else "") + f"LARP check failed: {exc}"

        chosen = self._choose(members, limit)
        await asyncio.gather(*(one(m) for m in chosen))

        # analysis may have found the real create time and new bundle roles: refresh both
        index = build_clusters(self.store, self.cfg.clusters)
        black = {c.cluster_id for c in index.black(self.cfg.selection)}
        for m in members:
            tok = self.store.token(m.mint) or {}
            m.create_time = tok.get("create_time") or m.create_time
            m.name, m.symbol = m.name or tok.get("name") or "", m.symbol or tok.get("symbol") or ""
            cid = index.wallet_to_cluster.get(tok.get("creator") or "")
            if not cid:  # creator unknown or not clustered: any bundle wallet of a known group counts
                for _, wallet, role in self.store.roles([m.mint]):
                    if role == "bundle" and wallet in index.wallet_to_cluster:
                        cid = index.wallet_to_cluster[wallet]
                        break
            if cid:
                m.cluster = index.clusters.get(cid)
                m.cluster_black = cid in black
                if m.larp and m.cluster:
                    m.larp.findings += LarpChecker.dev_findings(m.cluster)
        dated = [m for m in members if m.create_time]
        if dated:
            min(dated, key=lambda m: m.create_time or 0).first = True
        return index

    # -- verdict -----------------------------------------------------------------------

    def pick(self, members: list[Member]) -> tuple[Member | None, str]:
        max_larp = self.cfg.strategy.max_larp_score
        ok = [m for m in members if m.larp and m.larp.score <= max_larp and m.narrative_label not in ("LARP", "COPY")]
        if not ok:
            return None, "no token of this narrative passes the LARP check: it looks borrowed / copied everywhere"
        ok.sort(key=lambda m: (m.narrative_label != "ORIGINAL", not m.cluster_black, -(m.market_cap or 0)))
        best = ok[0]
        name = f"{best.symbol or best.name or '?'} ({_short(best.mint)})"
        parts = [f"{name}: {best.narrative_label.lower()} token of this narrative, LARP score {best.larp.score if best.larp else '?'}"]
        m = best.metrics or {}
        if best.cluster_black and best.cluster:
            c = best.cluster
            parts.append(
                f"bundled by {c.cluster_id}, a group in the black ({c.realized_pnl:+.1f} SOL over {len(c.launches)} launches, "
                f"{c.win_rate:.0%} wins): the copy-trade target, exit when they sell"
            )
        elif best.bundle_label == "HEAVY BUNDLE":
            parts.append(f"but the bundle is heavy (bundlers {m.get('bundlers_pct', 0):.0%}, insiders {m.get('insiders_pct') or 0:.0%}): dump risk")
        elif best.bundle_label == "BUNDLE SOLD":
            parts.append("the bundle has already sold most of its bag")
        elif best.bundle_label == "CLEAN":
            parts.append("bundle is small / clean")
        return best, "; ".join(parts)

    # -- live watch --------------------------------------------------------------------

    def matches_create(self, q: Query, ev: MarketEvent) -> bool:
        return _matches_terms(q.terms, ev.name, ev.symbol)

    async def watch(self, q: Query, feed: Any, on_member: Any) -> None:
        """Report every new launch that joins the narrative, analyzed once its bundle has landed."""
        tasks: set[asyncio.Task] = set()
        sem = asyncio.Semaphore(4)

        async def link_match(ev: MarketEvent) -> bool:
            if not ev.uri:
                return False
            async with sem:
                meta = await self.larp.store_offchain(ev.mint, ev.uri)
            return bool(
                (q.twitter and norm_link(meta.twitter) == q.twitter)
                or (q.website and norm_link(meta.website).split("/")[0] == q.website)
            )

        async def handle(ev: MarketEvent) -> None:
            if not self.matches_create(q, ev) and not ((q.twitter or q.website) and await link_match(ev)):
                return
            await asyncio.sleep(self.cfg.narrative.watch_delay_s)
            members = [Member(ev.mint, ev.name, ev.symbol, ev.ts, {"live"})]
            family = await self.find(Query(q.raw, list(q.terms), q.twitter, q.website), use_dex=False)
            await self.analyze(members, limit=1)
            first = min((m for m in family if m.create_time), key=lambda m: m.create_time or 0, default=None)
            members[0].first = bool(first and first.mint == ev.mint)
            on_member(members[0], len(family))

        await feed.subscribe_new_tokens()
        async for ev in feed.events():
            if ev.kind != "create":
                continue
            self.store.upsert_token(ev.mint, creator=ev.wallet, create_time=ev.ts, name=ev.name, symbol=ev.symbol,
                                    uri=ev.uri or None, source="pumpportal")
            task = asyncio.create_task(handle(ev))
            tasks.add(task)
            task.add_done_callback(tasks.discard)


# -- output ---------------------------------------------------------------------------


def _usd(x: float | None) -> str:
    if not x:
        return "-"
    return f"${x / 1e6:.1f}M" if x >= 1e6 else f"${x / 1e3:.0f}k" if x >= 1e3 else f"${x:.0f}"


def _pct(x: float | None) -> str:
    return f"{x:.0%}" if x is not None else "-"


def member_line(i: int, m: Member) -> str:
    met = m.metrics or {}
    dev = "-"
    if m.cluster:
        dev = f"{m.cluster.cluster_id}{' (black)' if m.cluster_black else ''} {m.cluster.realized_pnl:+.1f}"
    name = (m.symbol or m.name or "?")[:10]
    return (
        f"{i:>2}  {name:10} {_short(m.mint):12} {_ago(m.create_time):>12} {_usd(m.market_cap):>7} "
        f"{(str(m.larp.score) if m.larp else '-'):>4}  {m.narrative_label:17} "
        f"{_pct(met.get('bundlers_pct')):>4}/{_pct(m.linked_initial_pct):<4} {_pct(met.get('insiders_pct')):>8}  "
        f"{dev:26} {m.bundle_label}"
    )


HEADER = (
    f"{'#':>2}  {'token':10} {'mint':12} {'created':>12} {'mcap':>7} {'LARP':>4}  {'narrative':17} "
    f"{'bundle now/launch':>9} {'insiders':>8}  {'dev group':26} bundle"
)


def format_family(q: Query, members: list[Member], pick: tuple[Member | None, str]) -> str:
    analyzed = [m for m in members if m.larp or m.metrics]
    rest = [m for m in members if m not in analyzed]
    analyzed.sort(key=lambda m: m.create_time or float("inf"))
    srcs = {s for m in members for s in m.sources}
    lines = [
        f'NARRATIVE "{q.label}": {len(members)} token(s) found ({", ".join(sorted(srcs)) or "-"}), {len(analyzed)} analyzed',
        "",
        HEADER,
        "-" * len(HEADER),
    ]
    lines += [member_line(i, m) for i, m in enumerate(analyzed, 1)]
    for m in analyzed:
        if m.error:
            lines.append(f"    {_short(m.mint)}: {m.error}")
    if rest:
        lines.append(f"\n{len(rest)} more not analyzed (raise --limit): "
                     + ", ".join(f"{m.symbol or '?'} {_short(m.mint)}" for m in rest[:8]) + (" ..." if len(rest) > 8 else ""))
    best, why = pick
    lines += ["", f"PICK: {why}" if best else f"PICK: none - {why}"]
    bad = [m for m in analyzed if m.narrative_label in ("LARP", "COPY")]
    if bad:
        lines.append(f"AVOID: {len(bad)} copies / LARPs: " + ", ".join(f"{m.symbol or '?'} {_short(m.mint)}" for m in bad))
    return "\n".join(lines)


def family_to_dict(q: Query, members: list[Member], pick: tuple[Member | None, str]) -> dict[str, Any]:
    return {
        "query": q.raw,
        "terms": q.terms,
        "twitter": q.twitter,
        "website": q.website,
        "pick": pick[0].mint if pick[0] else None,
        "pick_reason": pick[1],
        "tokens": [
            {
                "mint": m.mint, "name": m.name, "symbol": m.symbol, "create_time": m.create_time,
                "sources": sorted(m.sources), "market_cap_usd": m.market_cap, "volume_h24_usd": m.volume_h24,
                "narrative": m.narrative_label, "bundle": m.bundle_label, "first": m.first,
                "larp_score": m.larp.score if m.larp else None,
                "larp_findings": [f"[{f.level}] {f.text}" for f in m.larp.findings] if m.larp else [],
                "holder_panel": m.metrics, "dev_cluster": m.cluster.cluster_id if m.cluster else None,
                "dev_cluster_in_the_black": m.cluster_black, "error": m.error or None,
            }
            for m in sorted(members, key=lambda m: m.create_time or float("inf"))
        ],
        "generated_at": time.time(),
    }
