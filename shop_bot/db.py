import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS texts (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- старые подписи кнопок меню, чтобы у пользователей со старой клавиатурой всё работало
CREATE TABLE IF NOT EXISTS aliases (
    label TEXT PRIMARY KEY,
    key   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cities (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shops (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL REFERENCES cities(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    hours   TEXT NOT NULL DEFAULT '',
    phone   TEXT NOT NULL DEFAULT '',
    lat     REAL,
    lon     REAL
);
CREATE TABLE IF NOT EXISTS categories (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    photo       TEXT
);
CREATE TABLE IF NOT EXISTS variants (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    price      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stock (
    shop_id    INTEGER NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
    variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
    PRIMARY KEY (shop_id, variant_id)
);
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    username   TEXT,
    full_name  TEXT,
    age_ok     INTEGER NOT NULL DEFAULT 0,
    shop_id    INTEGER REFERENCES shops(id) ON DELETE SET NULL,
    blocked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    product_id    INTEGER,
    text          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    created_at    REAL NOT NULL,
    last_reminded REAL,
    answered_at   REAL,
    answered_by   INTEGER,
    answer        TEXT
);
"""

_conn: aiosqlite.Connection | None = None


async def connect(path: str) -> None:
    global _conn
    _conn = await aiosqlite.connect(path)
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA foreign_keys = ON")
    await _conn.executescript(SCHEMA)
    await _conn.commit()


async def close() -> None:
    if _conn:
        await _conn.close()


async def fetch_all(sql: str, *args) -> list[aiosqlite.Row]:
    cur = await _conn.execute(sql, args)
    return list(await cur.fetchall())


async def fetch_one(sql: str, *args) -> aiosqlite.Row | None:
    cur = await _conn.execute(sql, args)
    return await cur.fetchone()


async def execute(sql: str, *args) -> int:
    cur = await _conn.execute(sql, args)
    await _conn.commit()
    return cur.lastrowid


async def upsert_user(user) -> None:
    await execute(
        "INSERT INTO users (id, username, full_name) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET username = excluded.username, "
        "full_name = excluded.full_name",
        user.id,
        user.username,
        user.full_name,
    )
