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

    async def sell(self, mint: str, token_amount: float, ref_price: float, fraction: float = 1.0) -> Fill | None: ...


class PaperBroker:
    """Simulated fills with realistic costs, paid from a paper balance.

    Costs per side: pool fee (fee_pct of the trade), fixed priority fee + tip (fee_sol) and
    slippage on the price. With a store, every buy is paid from the paper balance and refused if
    the balance is short; sells pay into it. Without a store (backtests) money is unlimited.
    """

    live = False

    def __init__(self, slippage_pct: float = 3.0, fee_sol: float = 0.0015, fee_pct: float = 0.0,
                 store: Any = None, start_balance: float = 1.0) -> None:
        self.slip = slippage_pct / 100
        self.fee = fee_sol
        self.fee_pct = fee_pct / 100
        self.store = store
        if store is not None:
            store.paper_balance(default=start_balance)

    def balance(self) -> float | None:
        return self.store.paper_balance() if self.store is not None else None

    def round_trip_cost_pct(self, size_sol: float) -> float:
        """What a buy + sell costs before the price moves at all, as a fraction of the stake."""
        fixed = 2 * self.fee / size_sol if size_sol > 0 else 0.0
        return 2 * self.fee_pct + 2 * self.slip + fixed

    def fill_buy(self, mint: str, sol_amount: float, ref_price: float, ts: float | None = None) -> Fill:
        price = ref_price * (1 + self.slip)
        spent = sol_amount * (1 + self.fee_pct) + self.fee
        return Fill("buy", mint, price, sol_amount / price, spent, "paper", ts or time.time())

    def fill_sell(self, mint: str, token_amount: float, ref_price: float, ts: float | None = None) -> Fill:
        price = ref_price * (1 - self.slip)
        got = token_amount * price * (1 - self.fee_pct) - self.fee
        return Fill("sell", mint, price, token_amount, got, "paper", ts or time.time())

    async def buy(self, mint: str, sol_amount: float, ref_price: float) -> Fill | None:
        fill = self.fill_buy(mint, sol_amount, ref_price)
        if self.store is not None:
            if self.store.paper_balance() < fill.sol:
                log.info("paper: balance too low for %.4f SOL", fill.sol)
                return None
            self.store.paper_move("buy", -fill.sol, mint)
        return fill

    async def sell(self, mint: str, token_amount: float, ref_price: float, fraction: float = 1.0) -> Fill | None:
        fill = self.fill_sell(mint, token_amount, ref_price)
        if self.store is not None:
            self.store.paper_move("sell", fill.sol, mint)
        return fill


WALLET_FILE = "data/wallet.key"  # written by `bh wallet set`, owner-only permissions, never committed


def load_keypair() -> Any:
    """Keypair from BH_PRIVATE_KEY (base58), BH_KEYPAIR_PATH (solana-keygen JSON) or data/wallet.key."""
    from solders.keypair import Keypair

    if os.environ.get("BH_PRIVATE_KEY"):
        return Keypair.from_base58_string(os.environ["BH_PRIVATE_KEY"].strip())
    path = os.environ.get("BH_KEYPAIR_PATH") or (WALLET_FILE if os.path.exists(WALLET_FILE) else "")
    if path:
        with open(path) as fh:
            return Keypair.from_bytes(bytes(json.load(fh)))
    raise ExecutionError("live trading needs a wallet: run `bh wallet set` (use a dedicated hot wallet)")


def save_keypair(secret: str, path: str = WALLET_FILE) -> str:
    """Validate a base58 secret key or a JSON byte array and store it owner-only. Returns the address."""
    from solders.keypair import Keypair

    secret = secret.strip()
    kp = Keypair.from_bytes(bytes(json.loads(secret))) if secret.startswith("[") else Keypair.from_base58_string(secret)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(list(bytes(kp)), fh)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(kp.pubkey())


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

    async def sell(self, mint: str, token_amount: float, ref_price: float, fraction: float = 1.0) -> Fill | None:
        # a percentage of the wallet's tokens: "100%" leaves no dust; partial takes sell their share
        pct = "100%" if fraction >= 0.999 else f"{max(1, round(fraction * 100))}%"
        return await self._execute("sell", mint, pct, False)

    async def balance_sol(self) -> float:
        return await self.rpc.get_balance(self.pubkey) / LAMPORTS_PER_SOL
