"""Live market feed (PumpPortal websocket) normalized to `MarketEvent`s.

PumpPortal streams pump.fun creates and trades with ~sub-second delay and
lets us subscribe to: every new token, trades by specific wallets (our
tracked bundler wallets) and trades on specific tokens (our watches).
For the lowest latency replace this with a Yellowstone gRPC (Geyser) stream
from your RPC provider - the strategy only needs `MarketEvent`s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Iterable

import websockets

from .models import MarketEvent

log = logging.getLogger(__name__)

_CHUNK = 200


def normalize(msg: dict[str, Any], now: float) -> MarketEvent | None:
    kind = msg.get("txType")
    mint, wallet = msg.get("mint"), msg.get("traderPublicKey")
    if kind not in ("create", "buy", "sell") or not mint or not wallet:
        return None
    sol = float(msg.get("solAmount") or 0)
    tokens = float(msg.get("tokenAmount") or msg.get("initialBuy") or 0)
    v_sol, v_tok = msg.get("vSolInBondingCurve"), msg.get("vTokensInBondingCurve")
    if v_sol and v_tok:
        price = float(v_sol) / float(v_tok)  # marginal bonding-curve price after this trade
    else:
        price = sol / tokens if sol and tokens else 0.0
    return MarketEvent(
        kind=kind,
        mint=mint,
        wallet=wallet,
        ts=now,
        sol_amount=sol,
        token_amount=tokens,
        price=price,
        signature=msg.get("signature", ""),
        name=msg.get("name", "") or "",
        symbol=msg.get("symbol", "") or "",
        uri=msg.get("uri", "") or "",
    )


class PumpPortalFeed:
    def __init__(self, url: str) -> None:
        self.url = url
        self._ws: Any = None
        self.new_tokens = False
        self.accounts: set[str] = set()
        self.tokens: set[str] = set()

    async def _send(self, method: str, keys: Iterable[str] | None = None) -> None:
        if self._ws is None:
            return  # will be (re)sent on connect
        if keys is None:
            await self._ws.send(json.dumps({"method": method}))
            return
        keys = sorted(keys)
        for i in range(0, len(keys), _CHUNK):
            await self._ws.send(json.dumps({"method": method, "keys": keys[i : i + _CHUNK]}))

    async def subscribe_new_tokens(self) -> None:
        self.new_tokens = True
        await self._send("subscribeNewToken")

    async def set_accounts(self, wallets: Iterable[str]) -> None:
        wanted = set(wallets)
        add, remove = wanted - self.accounts, self.accounts - wanted
        self.accounts = wanted
        if remove:
            await self._send("unsubscribeAccountTrade", remove)
        if add:
            await self._send("subscribeAccountTrade", add)

    async def subscribe_tokens(self, mints: Iterable[str]) -> None:
        add = set(mints) - self.tokens
        self.tokens |= add
        if add:
            await self._send("subscribeTokenTrade", add)

    async def unsubscribe_tokens(self, mints: Iterable[str]) -> None:
        remove = set(mints) & self.tokens
        self.tokens -= remove
        if remove:
            await self._send("unsubscribeTokenTrade", remove)

    async def _resubscribe(self) -> None:
        if self.new_tokens:
            await self._send("subscribeNewToken")
        if self.accounts:
            await self._send("subscribeAccountTrade", self.accounts)
        if self.tokens:
            await self._send("subscribeTokenTrade", self.tokens)

    async def events(self) -> AsyncIterator[MarketEvent]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=20, max_size=2**22) as ws:
                    self._ws = ws
                    backoff = 1.0
                    await self._resubscribe()
                    log.info("feed connected: %d wallets, %d tokens", len(self.accounts), len(self.tokens))
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except ValueError:
                            continue
                        ev = normalize(msg, time.time()) if isinstance(msg, dict) else None
                        if ev:
                            yield ev
            except (OSError, websockets.WebSocketException) as exc:
                log.warning("feed disconnected: %s", exc)
            finally:
                self._ws = None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
