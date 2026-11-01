"""SQLite storage for launches, trades, roles, funders and the bot's positions."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .models import ParsedTx, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY,
    creator TEXT,
    create_sig TEXT,
    create_slot INTEGER,
    create_time REAL,
    name TEXT,
    symbol TEXT,
    source TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | skipped | ok | partial | error
    note TEXT,
    bundle_wallets INTEGER,
    bundle_sol REAL,
    bundle_tokens REAL,
    supply REAL,
    analyzed_at REAL
);
CREATE INDEX IF NOT EXISTS tokens_status ON tokens(status, create_time);

CREATE TABLE IF NOT EXISTS trades (
    signature TEXT NOT NULL,
    wallet TEXT NOT NULL,
    mint TEXT NOT NULL,
    side TEXT NOT NULL,
    slot INTEGER NOT NULL,
    block_time INTEGER,
    token_amount REAL NOT NULL,
    sol_amount REAL NOT NULL,
    fee_sol REAL NOT NULL DEFAULT 0,
    tip_sol REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (signature, wallet, mint)
);
CREATE INDEX IF NOT EXISTS trades_mint ON trades(mint, slot);
CREATE INDEX IF NOT EXISTS trades_wallet ON trades(wallet);

-- who took part in a launch, and how: creator | bundle | sniper
CREATE TABLE IF NOT EXISTS roles (
    mint TEXT NOT NULL,
    wallet TEXT NOT NULL,
    role TEXT NOT NULL,
    slot INTEGER,
    tipped INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (mint, wallet)
);
CREATE INDEX IF NOT EXISTS roles_wallet ON roles(wallet);

CREATE TABLE IF NOT EXISTS transfers (
    signature TEXT NOT NULL,
    mint TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    amount REAL,
    slot INTEGER,
    PRIMARY KEY (signature, mint, src, dst)
);

CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    funder TEXT,
    funded_slot INTEGER,
    funded_time INTEGER,
    funded_sol REAL,
    history_complete INTEGER,
    tx_count INTEGER,
    profiled_at REAL
);
CREATE INDEX IF NOT EXISTS wallets_funder ON wallets(funder);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT NOT NULL,
    cluster_id TEXT,
    live INTEGER NOT NULL,
    opened_at REAL,
    entry_price REAL,
    size_sol REAL,
    tokens REAL,
    entry_sig TEXT,
    reason_open TEXT,
    closed_at REAL,
    exit_price REAL,
    exit_sol REAL,
    exit_sig TEXT,
    reason_close TEXT,
    pnl_sol REAL
);
"""

_CHUNK = 900


def _chunks(seq: Sequence[str]) -> Iterator[Sequence[str]]:
    for i in range(0, len(seq), _CHUNK):
        yield seq[i : i + _CHUNK]


def _row_to_trade(r: sqlite3.Row) -> Trade:
    return Trade(
        signature=r["signature"],
        slot=r["slot"],
        block_time=r["block_time"],
        mint=r["mint"],
        wallet=r["wallet"],
        side=r["side"],
        token_amount=r["token_amount"],
        sol_amount=r["sol_amount"],
        fee_sol=r["fee_sol"],
        tip_sol=r["tip_sol"],
    )


class Store:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- tokens ----------------------------------------------------------------

    def upsert_token(self, mint: str, **values: Any) -> None:
        self.db.execute("INSERT OR IGNORE INTO tokens(mint) VALUES (?)", (mint,))
        values = {k: v for k, v in values.items() if v is not None}
        if values:
            cols = ", ".join(f"{k} = ?" for k in values)
            self.db.execute(f"UPDATE tokens SET {cols} WHERE mint = ?", (*values.values(), mint))
        self.db.commit()

    def token(self, mint: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM tokens WHERE mint = ?", (mint,)).fetchone()
        return dict(row) if row else None

    def tokens(self, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if statuses is None:
            rows = self.db.execute("SELECT * FROM tokens ORDER BY create_time").fetchall()
        else:
            st = list(statuses)
            q = f"SELECT * FROM tokens WHERE status IN ({','.join('?' * len(st))}) ORDER BY create_time"
            rows = self.db.execute(q, st).fetchall()
        return [dict(r) for r in rows]

    def pending_tokens(self, min_age_s: float, limit: int) -> list[str]:
        cutoff = time.time() - min_age_s
        rows = self.db.execute(
            "SELECT mint FROM tokens WHERE status = 'pending' AND (create_time IS NULL OR create_time <= ?) "
            "ORDER BY create_time DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [r["mint"] for r in rows]

    # -- trades / transfers ----------------------------------------------------

    def add_parsed(self, txs: Iterable[ParsedTx]) -> None:
        trades, transfers = [], []
        for ptx in txs:
            trades.extend(
                (t.signature, t.wallet, t.mint, t.side, t.slot, t.block_time, t.token_amount, t.sol_amount, t.fee_sol, t.tip_sol)
                for t in ptx.trades
            )
            transfers.extend((x.signature, x.mint, x.src, x.dst, x.amount, x.slot) for x in ptx.token_transfers)
        self.db.executemany("INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?,?)", trades)
        self.db.executemany("INSERT OR IGNORE INTO transfers VALUES (?,?,?,?,?,?)", transfers)
        self.db.commit()

    def trades(self, wallets: Sequence[str] | None = None, mints: Sequence[str] | None = None) -> list[Trade]:
        if wallets is None and mints is None:
            rows = self.db.execute("SELECT * FROM trades ORDER BY slot").fetchall()
            return [_row_to_trade(r) for r in rows]
        col, values = ("wallet", list(wallets)) if wallets is not None else ("mint", list(mints or []))
        out: list[Trade] = []
        for chunk in _chunks(values):
            q = f"SELECT * FROM trades WHERE {col} IN ({','.join('?' * len(chunk))})"
            out.extend(_row_to_trade(r) for r in self.db.execute(q, list(chunk)))
        out.sort(key=lambda t: (t.slot, t.signature))
        return out

    def last_prices(self) -> dict[str, float]:
        rows = self.db.execute(
            "SELECT t.mint, t.sol_amount, t.token_amount FROM trades t "
            "JOIN (SELECT mint, MAX(slot) AS s FROM trades GROUP BY mint) m ON t.mint = m.mint AND t.slot = m.s"
        ).fetchall()
        return {r["mint"]: r["sol_amount"] / r["token_amount"] for r in rows if r["token_amount"]}

    def transfers(self) -> list[tuple[str, str, str]]:
        return [(r["mint"], r["src"], r["dst"]) for r in self.db.execute("SELECT mint, src, dst FROM transfers")]

    # -- roles -----------------------------------------------------------------

    def replace_roles(self, mint: str, rows: Iterable[tuple[str, str, int, bool]]) -> None:
        """rows: (wallet, role, slot, tipped)"""
        self.db.execute("DELETE FROM roles WHERE mint = ?", (mint,))
        self.db.executemany(
            "INSERT INTO roles(mint, wallet, role, slot, tipped) VALUES (?,?,?,?,?)",
            [(mint, w, role, slot, int(tipped)) for w, role, slot, tipped in rows],
        )
        self.db.commit()

    def roles(self, mints: Sequence[str] | None = None) -> list[tuple[str, str, str]]:
        if mints is None:
            rows = self.db.execute("SELECT mint, wallet, role FROM roles").fetchall()
        else:
            rows = []
            for chunk in _chunks(list(mints)):
                q = f"SELECT mint, wallet, role FROM roles WHERE mint IN ({','.join('?' * len(chunk))})"
                rows.extend(self.db.execute(q, list(chunk)).fetchall())
        return [(r["mint"], r["wallet"], r["role"]) for r in rows]

    # -- wallets ---------------------------------------------------------------

    def set_wallet(self, address: str, **values: Any) -> None:
        values["profiled_at"] = time.time()
        cols = ", ".join(values)
        qs = ", ".join("?" * len(values))
        upd = ", ".join(f"{k} = excluded.{k}" for k in values)
        self.db.execute(
            f"INSERT INTO wallets(address, {cols}) VALUES (?, {qs}) ON CONFLICT(address) DO UPDATE SET {upd}",
            (address, *values.values()),
        )
        self.db.commit()

    def wallet(self, address: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM wallets WHERE address = ?", (address,)).fetchone()
        return dict(row) if row else None

    def funders(self) -> dict[str, str]:
        rows = self.db.execute("SELECT address, funder FROM wallets WHERE funder IS NOT NULL").fetchall()
        return {r["address"]: r["funder"] for r in rows}

    # -- bot positions ---------------------------------------------------------

    def open_position(self, **values: Any) -> int:
        cols = ", ".join(values)
        cur = self.db.execute(
            f"INSERT INTO positions({cols}) VALUES ({','.join('?' * len(values))})", tuple(values.values())
        )
        self.db.commit()
        return int(cur.lastrowid)

    def close_position(self, position_id: int, **values: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in values)
        self.db.execute(f"UPDATE positions SET {cols} WHERE id = ?", (*values.values(), position_id))
        self.db.commit()

    def positions(self, live: bool | None = None) -> list[dict[str, Any]]:
        if live is None:
            rows = self.db.execute("SELECT * FROM positions ORDER BY id").fetchall()
        else:
            rows = self.db.execute("SELECT * FROM positions WHERE live = ? ORDER BY id", (int(live),)).fetchall()
        return [dict(r) for r in rows]
