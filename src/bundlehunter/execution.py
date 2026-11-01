"""Order execution: paper broker (default) and a live broker (Jito bundle + RPC).

Live flow for one order:
  1. ask the swap API (PumpPortal `trade-local`) for an *unsigned* transaction
     - it routes pump.fun bonding curve, PumpSwap and Raydium pools;
  2. sign it locally - the private key never leaves this process;
  3. send it as a Jito bundle [swap, tip] for fast inclusion, and (optionally)
     the same signed tx through RPC as a fallback (one signature can only land once);
  4. wait for confirmation and read the real fill back from the chain.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .config import ExecutionConfig
from .constants import JITO_TIP_ACCOUNTS, LAMPORTS_PER_SOL
from .parser import parse_transaction
from .rpc import SolanaRpc

log = logging.getLogger(__name__)


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Fill:
    side: str
    mint: str
    price: float
    tokens: float
    sol: float  # buys: SOL spent incl. fees; sells: SOL received net of fees
    signature: str
    ts: float


class Broker(Protocol):
    live: bool

    async def buy(self, mint: str, sol_amount: float, ref_price: float) -> Fill | None: ...

    async def sell(self, mint: str, token_amount: float, ref_price: float) -> Fill | None: ...


class PaperBroker:
    """Simulated fills at the reference price minus an assumed slippage and fee."""

    live = False

    def __init__(self, slippage_pct: float = 3.0, fee_sol: float = 0.0015) -> None:
        self.slip = slippage_pct / 100
        self.fee = fee_sol

    def fill_buy(self, mint: str, sol_amount: float, ref_price: float, ts: float | None = None) -> Fill:
        price = ref_price * (1 + self.slip)
        return Fill("buy", mint, price, sol_amount / price, sol_amount + self.fee, "paper", ts or time.time())

    def fill_sell(self, mint: str, token_amount: float, ref_price: float, ts: float | None = None) -> Fill:
        price = ref_price * (1 - self.slip)
        return Fill("sell", mint, price, token_amount, token_amount * price - self.fee, "paper", ts or time.time())

    async def buy(self, mint: str, sol_amount: float, ref_price: float) -> Fill | None:
        return self.fill_buy(mint, sol_amount, ref_price)

    async def sell(self, mint: str, token_amount: float, ref_price: float) -> Fill | None:
        return self.fill_sell(mint, token_amount, ref_price)


def load_keypair() -> Any:
    """Keypair from BH_PRIVATE_KEY (base58) or BH_KEYPAIR_PATH (solana-keygen JSON)."""
    from solders.keypair import Keypair

    if os.environ.get("BH_PRIVATE_KEY"):
        return Keypair.from_base58_string(os.environ["BH_PRIVATE_KEY"].strip())
    path = os.environ.get("BH_KEYPAIR_PATH")
    if path:
        with open(path) as fh:
            return Keypair.from_bytes(bytes(json.load(fh)))
    raise ExecutionError("live trading needs BH_PRIVATE_KEY or BH_KEYPAIR_PATH (use a dedicated hot wallet)")


class LiveBroker:
    live = True

    def __init__(self, cfg: ExecutionConfig, rpc: SolanaRpc, keypair: Any, http: httpx.AsyncClient | None = None):
        self.cfg, self.rpc, self.kp = cfg, rpc, keypair
        self.pubkey = str(keypair.pubkey())
        self.http = http or httpx.AsyncClient(timeout=10)

    async def _build_tx(self, action: str, mint: str, amount: float | str, in_sol: bool) -> bytes:
        resp = await self.http.post(
            self.cfg.swap_api,
            data={
                "publicKey": self.pubkey,
                "action": action,
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true" if in_sol else "false",
                "slippage": self.cfg.slippage_pct,
                "priorityFee": self.cfg.priority_fee_sol,
                "pool": self.cfg.pool,
            },
        )
        if resp.status_code != 200:
            raise ExecutionError(f"swap api {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def _tip_tx(self) -> str:
        from solders.hash import Hash
        from solders.message import MessageV0
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import VersionedTransaction

        blockhash = await self.rpc.get_latest_blockhash()
        ix = transfer(
            TransferParams(
                from_pubkey=self.kp.pubkey(),
                to_pubkey=Pubkey.from_string(random.choice(sorted(JITO_TIP_ACCOUNTS))),
                lamports=int(self.cfg.jito_tip_sol * LAMPORTS_PER_SOL),
            )
        )
        msg = MessageV0.try_compile(self.kp.pubkey(), [ix], [], Hash.from_string(blockhash))
        return base64.b64encode(bytes(VersionedTransaction(msg, [self.kp]))).decode()

    async def _send_bundle(self, txs_b64: list[str]) -> str:
        resp = await self.http.post(
            f"{self.cfg.jito_block_engine.rstrip('/')}/api/v1/bundles",
            json={"jsonrpc": "2.0", "id": 1, "method": "sendBundle", "params": [txs_b64, {"encoding": "base64"}]},
        )
        data = resp.json()
        if "error" in data:
            raise ExecutionError(f"jito: {data['error']}")
        return data.get("result", "")

    async def _submit(self, raw: bytes) -> str:
        from solders.transaction import VersionedTransaction

        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self.kp])
        sig = str(signed.signatures[0])
        b64 = base64.b64encode(bytes(signed)).decode()
        sent = False
        if self.cfg.use_jito:
            try:
                bundle_id = await self._send_bundle([b64, await self._tip_tx()])
                log.info("jito bundle %s for %s", bundle_id, sig)
                sent = True
            except Exception as exc:
                log.warning("jito send failed (%s), falling back to RPC", exc)
        if self.cfg.also_send_rpc or not sent:
            await self.rpc.send_transaction(b64)
        return sig

    async def _confirm(self, sig: str) -> dict[str, Any] | None:
        deadline = time.monotonic() + self.cfg.confirm_timeout_s
        while time.monotonic() < deadline:
            status = (await self.rpc.get_signature_statuses([sig]) or [None])[0]
            if status:
                if status.get("err"):
                    log.warning("tx %s failed: %s", sig, status["err"])
                    return None
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    for _ in range(10):
                        tx = await self.rpc.get_transaction(sig)
                        if tx:
                            return tx
                        await asyncio.sleep(0.5)
            await asyncio.sleep(0.4)
        log.warning("tx %s not confirmed in %.0fs", sig, self.cfg.confirm_timeout_s)
        return None

    async def _execute(self, side: str, mint: str, amount: float | str, in_sol: bool) -> Fill | None:
        sig = await self._submit(await self._build_tx(side, mint, amount, in_sol))
        tx = await self._confirm(sig)
        if not tx:
            return None
        ptx = parse_transaction(tx, sig)
        trade = next((t for t in ptx.trades if t.wallet == self.pubkey and t.mint == mint and t.side == side), None)
        if not trade:
            return None
        sol = -trade.cash_flow if side == "buy" else trade.cash_flow
        return Fill(side, mint, trade.price, trade.token_amount, sol, sig, time.time())

    async def buy(self, mint: str, sol_amount: float, ref_price: float) -> Fill | None:
        return await self._execute("buy", mint, sol_amount, True)

    async def sell(self, mint: str, token_amount: float, ref_price: float) -> Fill | None:
        return await self._execute("sell", mint, "100%", False)  # whole bag, no dust left behind
