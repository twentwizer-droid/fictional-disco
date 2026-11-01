"""Small async Solana JSON-RPC client with rate limiting and retries."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

# node behind / block not available yet / long-term storage hiccups
_RETRYABLE_CODES = {-32004, -32005, -32014, -32016, 429}


class RpcError(RuntimeError):
    pass


class SolanaRpc:
    def __init__(
        self,
        url: str,
        max_concurrency: int = 8,
        requests_per_second: float = 8.0,
        timeout_s: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._sem = asyncio.Semaphore(max_concurrency)
        self._interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._next_at = 0.0
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self._interval
        if wait > 0:
            await asyncio.sleep(wait)

    async def call(self, method: str, params: list[Any] | None = None, retries: int = 5) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or []}
        delay = 0.5
        last: Exception | None = None
        async with self._sem:
            for _ in range(retries):
                await self._throttle()
                try:
                    resp = await self._client.post(self.url, json=payload)
                    if resp.status_code == 429 or resp.status_code >= 500:
                        last = RpcError(f"{method}: HTTP {resp.status_code}")
                    elif resp.status_code >= 400:
                        raise RpcError(f"{method}: HTTP {resp.status_code} {resp.text[:200]} (check BH_RPC_URL / API key)")
                    else:
                        data = resp.json()
                        err = data.get("error")
                        if not err:
                            return data.get("result")
                        last = RpcError(f"{method}: {err}")
                        if err.get("code") not in _RETRYABLE_CODES:
                            raise last
                except httpx.TransportError as exc:
                    last = exc
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        raise RpcError(f"{method} failed after {retries} attempts: {last}")

    # -- reads -----------------------------------------------------------------

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return await self.call(
            "getTransaction",
            [signature, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}],
        )

    async def signatures(
        self, address: str, max_pages: int = 1, until: str | None = None, page_size: int = 1000
    ) -> tuple[list[dict[str, Any]], bool]:
        """Signatures newest -> oldest. Second value is True if history was exhausted."""
        out: list[dict[str, Any]] = []
        before = None
        for _ in range(max_pages):
            opts: dict[str, Any] = {"limit": page_size, "commitment": "confirmed"}
            if before:
                opts["before"] = before
            if until:
                opts["until"] = until
            page = await self.call("getSignaturesForAddress", [address, opts]) or []
            out.extend(page)
            if len(page) < page_size:
                return out, True
            before = page[-1]["signature"]
        return out, False

    async def get_parsed_account(self, address: str) -> dict[str, Any] | None:
        res = await self.call("getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return (res or {}).get("value")

    async def get_multiple_accounts(self, addresses: list[str]) -> list[dict[str, Any] | None]:
        out: list[dict[str, Any] | None] = []
        for i in range(0, len(addresses), 100):
            res = await self.call(
                "getMultipleAccounts", [addresses[i : i + 100], {"encoding": "jsonParsed", "commitment": "confirmed"}]
            )
            out.extend((res or {}).get("value") or [None] * len(addresses[i : i + 100]))
        return out

    async def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        res = await self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return (res or {}).get("value") or []

    async def get_token_supply(self, mint: str) -> float | None:
        res = await self.call("getTokenSupply", [mint])
        value = (res or {}).get("value") or {}
        return float(value["uiAmountString"]) if value.get("uiAmountString") else None

    async def get_latest_blockhash(self) -> str:
        res = await self.call("getLatestBlockhash", [{"commitment": "confirmed"}])
        return res["value"]["blockhash"]

    async def get_signature_statuses(self, signatures: list[str]) -> list[dict[str, Any] | None]:
        res = await self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": False}])
        return (res or {}).get("value") or []

    async def get_balance(self, address: str) -> int:
        res = await self.call("getBalance", [address, {"commitment": "confirmed"}])
        return int((res or {}).get("value", 0))

    # -- writes ----------------------------------------------------------------

    async def send_transaction(self, tx_base64: str) -> str:
        return await self.call(
            "sendTransaction",
            [tx_base64, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}],
            retries=2,
        )
