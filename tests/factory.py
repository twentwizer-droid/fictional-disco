"""Builders for synthetic jsonParsed transactions and pre-filled stores."""

from __future__ import annotations

from typing import Any

from bundlehunter.constants import JITO_TIP_ACCOUNTS
from bundlehunter.models import ParsedTx, Trade
from bundlehunter.storage import Store

TIP = sorted(JITO_TIP_ACCOUNTS)[0]
SOL = 1_000_000_000
RENT = 2_039_280


def tok(idx: int, mint: str, owner: str, raw: int, decimals: int = 6) -> dict[str, Any]:
    return {"accountIndex": idx, "mint": mint, "owner": owner, "uiTokenAmount": {"amount": str(raw), "decimals": decimals}}


def sys_transfer(src: str, dst: str, lamports: int) -> dict[str, Any]:
    return {
        "program": "system",
        "programId": "11111111111111111111111111111111",
        "parsed": {"type": "transfer", "info": {"source": src, "destination": dst, "lamports": lamports}},
    }


def init_mint(mint: str) -> dict[str, Any]:
    return {
        "program": "spl-token",
        "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "parsed": {"type": "initializeMint2", "info": {"mint": mint, "decimals": 6}},
    }


def make_tx(
    sig: str,
    slot: int,
    keys: list[tuple[str, bool]],
    pre: list[int],
    post: list[int],
    pre_tok: list[dict] | None = None,
    post_tok: list[dict] | None = None,
    instructions: list[dict] | None = None,
    inner: list[dict] | None = None,
    fee: int = 5000,
    err: Any = None,
    block_time: int = 1_700_000_000,
) -> dict[str, Any]:
    return {
        "slot": slot,
        "blockTime": block_time,
        "meta": {
            "err": err,
            "fee": fee,
            "preBalances": pre,
            "postBalances": post,
            "preTokenBalances": pre_tok or [],
            "postTokenBalances": post_tok or [],
            "innerInstructions": [{"index": 0, "instructions": inner}] if inner else [],
        },
        "transaction": {
            "signatures": [sig],
            "message": {
                "accountKeys": [{"pubkey": k, "signer": s, "writable": True, "source": "transaction"} for k, s in keys],
                "instructions": instructions or [],
            },
        },
    }


def pump_buy_tx(
    sig: str, slot: int, user: str, mint: str, sol: float, tokens: float, tip_sol: float = 0.0,
    create: bool = False, block_time: int = 1_700_000_000, user_balance: int = 5 * SOL,
) -> dict[str, Any]:
    """pump.fun-style buy: user pays SOL to the bonding curve, gets tokens in a fresh ATA."""
    lamports, raw, tip = int(sol * SOL), int(tokens * 1_000_000), int(tip_sol * SOL)
    fee = 5000
    keys = [(user, True), (f"{user}-ata", False), (f"{mint}-curve", False), (f"{mint}-curve-ata", False), (TIP, False)]
    pre = [user_balance, 0, 30 * SOL, RENT, 10 * SOL]
    post = [user_balance - lamports - fee - RENT - tip, RENT, 30 * SOL + lamports, RENT, 10 * SOL + tip]
    curve_pre = 800_000_000 * 1_000_000
    return make_tx(
        sig, slot, keys, pre, post,
        pre_tok=[tok(3, mint, f"{mint}-curve", curve_pre)],
        post_tok=[tok(1, mint, user, raw), tok(3, mint, f"{mint}-curve", curve_pre - raw)],
        instructions=[sys_transfer(user, TIP, tip)] if tip else [],
        inner=[init_mint(mint)] if create else None,
        fee=fee, block_time=block_time,
    )


def trade(sig: str, mint: str, wallet: str, side: str, sol: float, tokens: float, slot: int, t: int) -> Trade:
    return Trade(sig, slot, t, mint, wallet, side, tokens, sol)


def add_launch(
    store: Store, mint: str, creator: str, bundle: list[str], t0: int, slot0: int,
    buy_sol: float, sell_sol: float, sell_after: int = 120, snipers: list[str] | None = None,
) -> None:
    """A launch where creator + bundle each buy `buy_sol` and later sell for `sell_sol`."""
    store.upsert_token(mint, creator=creator, create_slot=slot0, create_time=t0, status="ok", supply=1e9)
    trades = []
    for i, w in enumerate([creator, *bundle]):
        trades.append(trade(f"{mint}-b{i}", mint, w, "buy", buy_sol, 10_000_000, slot0, t0))
        trades.append(trade(f"{mint}-s{i}", mint, w, "sell", sell_sol, 10_000_000, slot0 + 300, t0 + sell_after))
    store.add_parsed([ParsedTx("x", 0, None, False, "", [], trades=trades)])
    roles = [(creator, "creator", slot0, True)] + [(w, "bundle", slot0, True) for w in bundle]
    roles += [(w, "sniper", slot0 + 1, False) for w in snipers or []]
    store.replace_roles(mint, roles)
