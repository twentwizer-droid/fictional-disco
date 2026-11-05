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
    full_profile INTEGER NOT NULL DEFAULT 1,  -- 0 = only the funder was looked up
    profiled_at REAL
);
CREATE INDEX IF NOT EXISTS wallets_funder ON wallets(funder);

-- your own watchlist of trader wallets (signals)
CREATE TABLE IF NOT EXISTS traders (
    address TEXT PRIMARY KEY,
    label TEXT,
    added_at REAL
);

-- everything the scalper saw, for replaying it offline (bh scalp eval)
CREATE TABLE IF NOT EXISTS scalp_events (
    mint TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, wallet TEXT, sol REAL, tokens REAL, price REAL,
    person TEXT, score REAL
);
CREATE INDEX IF NOT EXISTS scalp_events_mint ON scalp_events(mint, ts);
CREATE TABLE IF NOT EXISTS scalp_checks (mint TEXT PRIMARY KEY, ok INTEGER, reason TEXT, metrics TEXT, ts REAL);

-- paper money: one balance, every movement logged
CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    balance REAL NOT NULL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS paper_ledger (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,      -- deposit | withdraw | reset | buy | sell
    amount REAL NOT NULL,    -- + in, - out
    balance REAL NOT NULL,
    note TEXT
);

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
        self._migrate()

    _ADDED_COLUMNS = {
        "wallets": {"full_profile": "INTEGER NOT NULL DEFAULT 1"},
        # token metadata for the LARP check; links are stored normalized (see larp.norm_link)
        "tokens": {c: "TEXT" for c in ("uri", "description", "image", "twitter", "telegram", "website")}
        | {"meta_at": "REAL"},
        "positions": {"strategy": "TEXT NOT NULL DEFAULT 'bundle'", "partial_sol": "REAL NOT NULL DEFAULT 0"},
    }
    _LINK_COLUMNS = ("uri", "image", "twitter", "telegram", "website")

    def _migrate(self) -> None:
        for table, columns in self._ADDED_COLUMNS.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, decl in columns.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.db.commit()

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

    def similar_tokens(self, mint: str, name: str, symbol: str) -> list[dict[str, Any]]:
        """Other tokens with the same ticker or name (case, spaces and '$' ignored)."""
        norm = "LOWER(REPLACE(REPLACE(TRIM({c}), '$', ''), ' ', ''))"
        conds, args = [], []
        if symbol:
            conds.append(f"{norm.format(c='symbol')} = ?")
            args.append(symbol.replace("$", "").replace(" ", "").lower())
        if name:
            conds.append(f"{norm.format(c='name')} = ?")
            args.append(name.replace("$", "").replace(" ", "").lower())
        if not conds:
            return []
        q = f"SELECT * FROM tokens WHERE mint != ? AND ({' OR '.join(conds)}) ORDER BY create_time"
        return [dict(r) for r in self.db.execute(q, (mint, *args))]

    def tokens_with(self, column: str, value: str, exclude_mint: str) -> list[dict[str, Any]]:
        if column not in self._LINK_COLUMNS:
            raise ValueError(column)
        if not value:
            return []
        q = f"SELECT * FROM tokens WHERE {column} = ? AND mint != ? ORDER BY create_time"
        return [dict(r) for r in self.db.execute(q, (value, exclude_mint))]

    def search_tokens(self, terms: Sequence[str], limit: int = 200) -> list[dict[str, Any]]:
        """Tokens whose name / ticker / description contain every term (case-insensitive)."""
        if not terms:
            return []
        text = "LOWER(COALESCE(name, '') || ' ' || COALESCE(symbol, '') || ' ' || COALESCE(description, ''))"
        where = " AND ".join(f"{text} LIKE ?" for _ in terms)
        q = f"SELECT * FROM tokens WHERE {where} ORDER BY create_time LIMIT ?"
        return [dict(r) for r in self.db.execute(q, (*[f"%{t.lower()}%" for t in terms], limit))]

    def token_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) AS n FROM tokens GROUP BY status")}

    def wallet_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM wallets").fetchone()[0])

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

    def transfers_for_mint(self, mint: str) -> list[tuple[str, str, float, int]]:
        rows = self.db.execute("SELECT src, dst, amount, slot FROM transfers WHERE mint = ? ORDER BY slot", (mint,))
        return [(r["src"], r["dst"], r["amount"] or 0.0, r["slot"] or 0) for r in rows]

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

    def funders_of(self, wallets: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for chunk in _chunks(list(wallets)):
            q = f"SELECT address, funder FROM wallets WHERE funder IS NOT NULL AND address IN ({','.join('?' * len(chunk))})"
            out.update({r["address"]: r["funder"] for r in self.db.execute(q, list(chunk))})
        return out

    def funded_counts(self, funders: Sequence[str]) -> dict[str, int]:
        out: dict[str, int] = {}
        for chunk in _chunks(list(funders)):
            q = f"SELECT funder, COUNT(*) AS n FROM wallets WHERE funder IN ({','.join('?' * len(chunk))}) GROUP BY funder"
            out.update({r["funder"]: r["n"] for r in self.db.execute(q, list(chunk))})
        return out

    def funders(self) -> dict[str, str]:
        rows = self.db.execute("SELECT address, funder FROM wallets WHERE funder IS NOT NULL").fetchall()
        return {r["address"]: r["funder"] for r in rows}

    # -- traders watchlist -----------------------------------------------------

    def add_trader(self, address: str, label: str = "") -> None:
        self.db.execute(
            "INSERT INTO traders(address, label, added_at) VALUES (?,?,?) "
            "ON CONFLICT(address) DO UPDATE SET label = COALESCE(NULLIF(excluded.label, ''), traders.label)",
            (address, label, time.time()),
        )
        self.db.commit()

    def remove_trader(self, address: str) -> bool:
        cur = self.db.execute("DELETE FROM traders WHERE address = ?", (address,))
        self.db.commit()
        return cur.rowcount > 0

    def traders(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM traders ORDER BY added_at")]

    # -- scalper recordings ------------------------------------------------------

    def record_scalp_events(self, rows: list[tuple]) -> None:
        if rows:
            self.db.executemany("INSERT INTO scalp_events VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.db.commit()

    def record_scalp_check(self, mint: str, ok: bool, reason: str, metrics: dict | None) -> None:
        import json

        self.db.execute("INSERT OR REPLACE INTO scalp_checks VALUES (?,?,?,?,?)",
                        (mint, int(ok), reason, json.dumps(metrics or {}), time.time()))
        self.db.commit()

    def scalp_recordings(self) -> tuple[dict[str, list[dict]], dict[str, dict]]:
        import json

        events: dict[str, list[dict]] = {}
        for r in self.db.execute("SELECT * FROM scalp_events ORDER BY ts, rowid"):
            events.setdefault(r["mint"], []).append(dict(r))
        checks = {r["mint"]: {"ok": bool(r["ok"]), "reason": r["reason"], "metrics": json.loads(r["metrics"] or "{}")}
                  for r in self.db.execute("SELECT * FROM scalp_checks")}
        return events, checks

    # -- paper account ---------------------------------------------------------

    def paper_balance(self, default: float | None = None) -> float:
        row = self.db.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()
        if row is None:
            if default is None:
                return 0.0
            self.paper_move("reset", default, "first paper balance", absolute=True)
            return default
        return float(row["balance"])

    def paper_move(self, kind: str, amount: float, note: str = "", absolute: bool = False) -> float:
        """Change the paper balance (absolute=True sets it). Returns the new balance."""
        row = self.db.execute("SELECT balance FROM paper_account WHERE id = 1").fetchone()
        old = float(row["balance"]) if row else 0.0
        new = amount if absolute else old + amount
        self.db.execute(
            "INSERT INTO paper_account(id, balance, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET balance = excluded.balance, updated_at = excluded.updated_at",
            (new, time.time()),
        )
        self.db.execute("INSERT INTO paper_ledger VALUES (?,?,?,?,?)", (time.time(), kind, new - old, new, note))
        self.db.commit()
        return new

    def paper_ledger(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM paper_ledger ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def live_realized_pnl(self) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(pnl_sol), 0) FROM positions WHERE live = 1 AND closed_at IS NOT NULL")
        return float(row.fetchone()[0])

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
