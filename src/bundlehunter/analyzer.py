"""Launch and wallet analysis: who bundled a launch, who funded them, what they traded.

For a token (mint):
  1. page `getSignaturesForAddress(mint)` back to the oldest tx -> the create tx,
     its slot S and the creator (fee payer);
  2. fetch every tx in slots S..S+early_slots and parse the buys:
     slot S buyers are the bundle (the mint does not exist before S, so a buy in S
     almost always rode in the creator's own Jito bundle), later ones are snipers;
  3. profile the creator and every bundle wallet: their recent history (which
     other tokens they touched, when) and their funder (first SOL deposit).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .config import AnalyzerConfig
from .constants import LAMPORTS_PER_SOL
from .models import ParsedTx
from .parser import parse_transaction
from .rpc import SolanaRpc
from .storage import Store

log = logging.getLogger(__name__)


@dataclass
class LaunchReport:
    mint: str
    status: str
    creator: str | None = None
    create_slot: int | None = None
    create_time: int | None = None
    bundle_wallets: list[str] = field(default_factory=list)
    sniper_wallets: list[str] = field(default_factory=list)
    bundle_sol: float = 0.0
    bundle_tokens: float = 0.0
    supply: float | None = None
    note: str = ""

    @property
    def bundle_supply_pct(self) -> float | None:
        return self.bundle_tokens / self.supply if self.supply else None


@dataclass
class ScanResult:
    mint: str
    create_slot: int | None
    bundle_txs: int
    total_txs: int
    complete: bool


class Analyzer:
    def __init__(self, rpc: SolanaRpc, store: Store, cfg: AnalyzerConfig) -> None:
        self.rpc, self.store, self.cfg = rpc, store, cfg

    async def fetch_parsed(self, signatures: list[str]) -> list[ParsedTx]:
        async def one(sig: str) -> ParsedTx | None:
            try:
                tx = await self.rpc.get_transaction(sig)
            except Exception as exc:  # keep going on single failures
                log.warning("getTransaction %s failed: %s", sig, exc)
                return None
            return parse_transaction(tx, sig) if tx else None

        results = await asyncio.gather(*(one(s) for s in dict.fromkeys(signatures)))
        return [r for r in results if r is not None]

    async def quick_scan(self, mint: str) -> ScanResult:
        """1 RPC call: how many txs landed in the create slot (bundle size) and overall activity."""
        sigs, complete = await self.rpc.signatures(mint, max_pages=1)
        if not sigs:
            return ScanResult(mint, None, 0, 0, complete)
        create_slot = sigs[-1]["slot"] if complete else None
        bundle = sum(1 for s in sigs if create_slot is not None and s["slot"] == create_slot and not s.get("err"))
        return ScanResult(mint, create_slot, bundle, len(sigs), complete)

    async def analyze_launch(
        self, mint: str, profile: bool = True, full: bool = False, final: bool = True
    ) -> LaunchReport:
        """final=False (live holder panel) stores roles but leaves the launch 'pending' for the
        auto pipeline to analyze again later, with wallet profiles and the bundle's sells."""
        sigs, complete = await self.rpc.signatures(mint, max_pages=self.cfg.max_mint_sig_pages)
        if not sigs:
            self.store.upsert_token(mint, status="error", note="no signatures", analyzed_at=time.time())
            return LaunchReport(mint, "error", note="no signatures")

        oldest = [s for s in reversed(sigs) if not s.get("err")][:5]
        create_tx = None
        for ptx in await self.fetch_parsed([s["signature"] for s in oldest]):
            if mint in ptx.created_mints:
                create_tx = ptx
                break
        if create_tx is None:
            note = "create tx not reached (raise max_mint_sig_pages)" if not complete else "create tx not found"
            self.store.upsert_token(mint, status="error", note=note, analyzed_at=time.time())
            return LaunchReport(mint, "error", note=note)

        s0 = create_tx.slot
        early_sigs = [s["signature"] for s in sigs if not s.get("err") and s["slot"] <= s0 + self.cfg.early_slots]
        if full:  # whole early price path (oldest first), needed for meaningful backtests
            early_sigs += [s["signature"] for s in reversed(sigs) if not s.get("err")][: self.cfg.max_full_txs]
        txs = await self.fetch_parsed(early_sigs)
        self.store.add_parsed(txs)

        creator = create_tx.fee_payer
        window = [p for p in txs if p.slot <= s0 + self.cfg.early_slots]  # only the launch window decides roles
        buyers_per_slot: dict[int, set[str]] = defaultdict(set)
        for ptx in window:
            for t in ptx.trades:
                if t.mint == mint and t.side == "buy" and t.wallet != creator:
                    buyers_per_slot[ptx.slot].add(t.wallet)
        # bundle = the create slot, plus any early slot where many wallets bought at once
        n = self.cfg.bundle_min_wallets_per_slot
        bundle_slots = {s0} | {slot for slot, ws in buyers_per_slot.items() if n and len(ws) >= n}

        roles: dict[str, tuple[str, int, bool]] = {creator: ("creator", s0, create_tx.tips_lamports > 0)}
        bundle_sol = bundle_tokens = 0.0
        for ptx in sorted(window, key=lambda p: p.slot):
            for t in ptx.trades:
                if t.mint != mint or t.side != "buy":
                    continue
                in_bundle = ptx.slot in bundle_slots
                if in_bundle:
                    bundle_sol += t.sol_amount
                    bundle_tokens += t.token_amount
                if t.wallet in roles:
                    continue
                roles[t.wallet] = ("bundle" if in_bundle else "sniper", ptx.slot, t.tip_sol > 0 or ptx.tips_lamports > 0)
        self.store.replace_roles(mint, [(w, r, s, tip) for w, (r, s, tip) in roles.items()])

        try:
            supply = await self.rpc.get_token_supply(mint)
        except Exception:
            supply = None

        bundle = [w for w, (r, _, _) in roles.items() if r == "bundle"]
        snipers = [w for w, (r, _, _) in roles.items() if r == "sniper"]
        report = LaunchReport(
            mint=mint,
            status="ok",
            creator=creator,
            create_slot=s0,
            create_time=create_tx.block_time,
            bundle_wallets=bundle,
            sniper_wallets=snipers,
            bundle_sol=bundle_sol,
            bundle_tokens=bundle_tokens,
            supply=supply,
        )
        self.store.upsert_token(
            mint,
            creator=creator,
            create_sig=create_tx.signature,
            create_slot=s0,
            create_time=create_tx.block_time,
            bundle_wallets=len(bundle),
            bundle_sol=bundle_sol,
            bundle_tokens=bundle_tokens,
            supply=supply,
        )

        if profile:
            to_profile = [creator, *bundle, *(snipers if self.cfg.profile_snipers else [])]
            for wallet in to_profile:
                rec = self.store.wallet(wallet)
                if not (rec and rec["full_profile"]):
                    await self.profile_wallet(wallet)

        if final:
            self.store.upsert_token(mint, status=report.status, analyzed_at=time.time())
        return report

    def _find_funder(self, wallet: str, txs: list[ParsedTx]) -> tuple[str, int, int | None, float] | None:
        min_lamports = int(self.cfg.min_funding_sol * LAMPORTS_PER_SOL)
        for ptx in sorted(txs, key=lambda p: p.slot):
            for t in ptx.sol_transfers:
                if t.dst == wallet and t.src != wallet and t.lamports >= min_lamports:
                    return t.src, ptx.slot, ptx.block_time, t.lamports / LAMPORTS_PER_SOL
        return None

    async def quick_funder(self, wallet: str) -> str | None:
        """Funder only (1-2 RPC calls): enough to tell if an early buyer is linked to the dev."""
        rec = self.store.wallet(wallet)
        if rec:
            return rec["funder"]
        sigs, complete = await self.rpc.signatures(wallet, max_pages=1, page_size=self.cfg.quick_funder_sigs)
        hit = None
        if complete:
            oldest = [s["signature"] for s in reversed(sigs) if not s.get("err")][:3]
            hit = self._find_funder(wallet, await self.fetch_parsed(oldest))
        funder, slot, when, sol = hit or (None, None, None, None)
        self.store.set_wallet(
            wallet, funder=funder, funded_slot=slot, funded_time=when, funded_sol=sol,
            history_complete=int(complete), tx_count=len(sigs), full_profile=0,
        )
        return funder

    async def quick_funders(self, wallets: list[str]) -> dict[str, str]:
        wallets = list(dict.fromkeys(wallets))[: self.cfg.max_quick_funders]
        found = await asyncio.gather(*(self.quick_funder(w) for w in wallets), return_exceptions=True)
        return {w: f for w, f in zip(wallets, found, strict=False) if isinstance(f, str)}

    async def profile_wallet(self, wallet: str) -> dict[str, object]:
        """Store a wallet's recent trades and find its funder (first meaningful SOL deposit)."""
        sigs, complete = await self.rpc.signatures(wallet, max_pages=self.cfg.max_wallet_sig_pages)
        ok = [s for s in sigs if not s.get("err")]
        wanted = [s["signature"] for s in ok[: self.cfg.max_wallet_txs]]
        if complete:
            wanted += [s["signature"] for s in ok[-5:]]  # the oldest txs reveal the funder
        txs = await self.fetch_parsed(wanted)
        self.store.add_parsed(txs)

        hit = self._find_funder(wallet, txs) if complete else None
        funder, funded_slot, funded_time, funded_sol = hit or (None, None, None, None)
        self.store.set_wallet(
            wallet,
            funder=funder,
            funded_slot=funded_slot,
            funded_time=funded_time,
            funded_sol=funded_sol,
            history_complete=int(complete),
            tx_count=len(sigs),
            full_profile=1,
        )
        mints = sorted({t.mint for p in txs for t in p.trades if t.wallet == wallet})
        return {"wallet": wallet, "funder": funder, "txs": len(sigs), "complete": complete, "mints": mints}

    async def seed_wallet(self, wallet: str, max_launches: int = 20) -> list[LaunchReport]:
        """Start from a known bundler wallet: analyze every launch it took part in early."""
        info = await self.profile_wallet(wallet)
        reports = []
        for mint in list(info["mints"])[:max_launches]:  # type: ignore[index]
            tok = self.store.token(mint)
            if tok and tok["status"] in ("ok", "partial"):
                continue
            try:
                reports.append(await self.analyze_launch(mint))
            except Exception as exc:
                log.warning("analyze %s failed: %s", mint, exc)
        return reports

    async def process_pending(self, limit: int) -> list[LaunchReport | ScanResult]:
        """Auto pipeline: cheap scan of recorded launches, deep analysis of the bundled ones."""
        done: list[LaunchReport | ScanResult] = []
        for mint in self.store.pending_tokens(self.cfg.min_age_s, limit):
            try:
                scan = await self.quick_scan(mint)
                if scan.complete and (
                    scan.bundle_txs < self.cfg.min_create_slot_txs or scan.total_txs < self.cfg.min_total_txs
                ):
                    self.store.upsert_token(
                        mint, status="skipped", note=f"bundle_txs={scan.bundle_txs} txs={scan.total_txs}",
                        analyzed_at=time.time(),
                    )
                    done.append(scan)
                    continue
                done.append(await self.analyze_launch(mint))
            except Exception as exc:
                log.warning("pending %s failed: %s", mint, exc)
                self.store.upsert_token(mint, status="error", note=str(exc)[:200], analyzed_at=time.time())
        return done
