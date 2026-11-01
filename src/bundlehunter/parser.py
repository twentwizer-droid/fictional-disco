"""Turn a `getTransaction` (jsonParsed) result into trades, transfers and tips.

The parser is program-agnostic: instead of decoding pump.fun / PumpSwap /
Raydium instructions (whose layouts change), it diffs token and lamport
balances per owner. A signer whose token balance went up while its SOL went
down bought; the reverse is a sell. A signer who lost tokens with no SOL leg
sent them to someone - which is recorded as a wallet-to-wallet transfer, one
of the strongest signals that two wallets belong to the same bundler.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .constants import JITO_TIP_ACCOUNTS, LAMPORTS_PER_SOL, WSOL_MINT
from .models import ParsedTx, SolTransfer, TokenTransfer, Trade

_MINT_INIT = {"initializeMint", "initializeMint2"}
_TOKEN_PROGRAMS = {"spl-token", "spl-token-2022"}
_SOL_TRANSFER = {"transfer", "transferWithSeed"}


def _keys_and_signers(tx: dict[str, Any]) -> tuple[list[str], set[str]]:
    msg = tx["transaction"]["message"]
    raw = msg["accountKeys"]
    if raw and isinstance(raw[0], dict):
        # jsonParsed encoding already lists address-lookup-table accounts.
        return [k["pubkey"] for k in raw], {k["pubkey"] for k in raw if k.get("signer")}
    keys = list(raw)
    signers = set(keys[: msg.get("header", {}).get("numRequiredSignatures", 1)])
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys += loaded.get("writable", []) + loaded.get("readonly", [])
    return keys, signers


def _instructions(tx: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield from tx["transaction"]["message"].get("instructions", [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        yield from group.get("instructions", [])


def _token_rows(rows: list[dict[str, Any]] | None) -> dict[int, tuple[str, str | None, int, int]]:
    out = {}
    for r in rows or []:
        amt = r.get("uiTokenAmount") or {}
        out[r["accountIndex"]] = (r["mint"], r.get("owner"), int(amt.get("amount", "0")), int(amt.get("decimals", 0)))
    return out


def parse_transaction(
    tx: dict[str, Any],
    signature: str | None = None,
    slot: int | None = None,
    block_time: int | None = None,
) -> ParsedTx:
    meta = tx.get("meta") or {}
    keys, signers = _keys_and_signers(tx)
    sig = signature or tx["transaction"]["signatures"][0]
    slot = tx["slot"] if tx.get("slot") is not None else (slot or 0)
    block_time = tx["blockTime"] if tx.get("blockTime") is not None else block_time
    fee_payer = keys[0]
    result = ParsedTx(
        signature=sig,
        slot=slot,
        block_time=block_time,
        failed=meta.get("err") is not None,
        fee_payer=fee_payer,
        signers=sorted(signers),
    )
    if result.failed:
        return result  # nothing but the fee happened

    for ix in _instructions(tx):
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        info = parsed.get("info") or {}
        kind, program = parsed.get("type"), ix.get("program")
        if program == "system" and kind in _SOL_TRANSFER and info.get("destination"):
            result.sol_transfers.append(
                SolTransfer(info.get("source", ""), info["destination"], int(info.get("lamports", 0)))
            )
        elif program in _TOKEN_PROGRAMS and kind in _MINT_INIT and info.get("mint"):
            result.created_mints.append(info["mint"])

    tips_by_src: dict[str, int] = defaultdict(int)
    for t in result.sol_transfers:
        if t.dst in JITO_TIP_ACCOUNTS:
            tips_by_src[t.src] += t.lamports
    result.tips_lamports = sum(tips_by_src.values())

    pre, post = _token_rows(meta.get("preTokenBalances")), _token_rows(meta.get("postTokenBalances"))
    pre_l, post_l = meta.get("preBalances") or [], meta.get("postBalances") or []

    def lamport_delta(i: int) -> int:
        return (post_l[i] if i < len(post_l) else 0) - (pre_l[i] if i < len(pre_l) else 0)

    token_delta: dict[tuple[str, str], int] = defaultdict(int)
    decimals: dict[str, int] = {}
    owned_accounts: dict[str, set[int]] = defaultdict(set)
    for idx in set(pre) | set(post):
        p, q = pre.get(idx), post.get(idx)
        mint = (q or p)[0]
        owner = (q and q[1]) or (p and p[1])
        if not owner:
            continue
        owned_accounts[owner].add(idx)
        decimals[mint] = (q or p)[3]
        token_delta[(owner, mint)] += (q[2] if q else 0) - (p[2] if p else 0)

    key_index: dict[str, int] = {}
    for i, k in enumerate(keys):
        key_index.setdefault(k, i)
    fee = int(meta.get("fee") or 0)

    # (owner, raw_delta, is_signer) left over after trade classification, per mint
    leftovers: dict[str, list[tuple[str, int, bool]]] = defaultdict(list)
    for (owner, mint), raw in token_delta.items():
        if raw == 0 or mint == WSOL_MINT:
            continue
        is_signer = owner in signers
        if is_signer and owner in key_index:
            # Wallet lamports + lamports of every token account it owns in this tx:
            # makes ATA rent, WSOL wrap/unwrap and account closes cancel out.
            lam = sum(lamport_delta(i) for i in owned_accounts[owner] | {key_index[owner]})
            fee_l = fee if owner == fee_payer else 0
            tip_l = tips_by_src.get(owner, 0)
            swap = lam + fee_l + tip_l
            ui = abs(raw) / 10 ** decimals[mint]
            side = "buy" if raw > 0 and swap < 0 else "sell" if raw < 0 and swap > 0 else None
            if side:
                result.trades.append(
                    Trade(
                        signature=sig,
                        slot=slot,
                        block_time=block_time,
                        mint=mint,
                        wallet=owner,
                        side=side,
                        token_amount=ui,
                        sol_amount=abs(swap) / LAMPORTS_PER_SOL,
                        fee_sol=fee_l / LAMPORTS_PER_SOL,
                        tip_sol=tip_l / LAMPORTS_PER_SOL,
                    )
                )
                continue
        leftovers[mint].append((owner, raw, is_signer))

    for mint, rows in leftovers.items():
        srcs = [(o, -r) for o, r, s in rows if r < 0 and s]
        dsts = [(o, r) for o, r, _ in rows if r > 0]
        for src, out_amt in srcs:
            for dst, in_amt in dsts:
                if src != dst:
                    result.token_transfers.append(
                        TokenTransfer(sig, slot, mint, src, dst, min(out_amt, in_amt) / 10 ** decimals[mint])
                    )
    return result
