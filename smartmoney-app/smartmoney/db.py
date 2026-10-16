import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address TEXT PRIMARY KEY,
    label TEXT,
    source TEXT,                -- 'discovered' | 'manual'
    hits INTEGER DEFAULT 0,     -- winners this wallet was early on
    avg_multiple REAL,
    tx_per_day REAL,
    funder TEXT,
    is_bot INTEGER DEFAULT 0,
    cluster TEXT,
    tracked INTEGER DEFAULT 0,
    last_sig TEXT,
    last_slot INTEGER,
    added_at INTEGER
);
CREATE TABLE IF NOT EXISTS discovery_hits (
    wallet TEXT, mint TEXT, symbol TEXT,
    first_buy_ts INTEGER, buys INTEGER, sol_in REAL, avg_multiple REAL,
    PRIMARY KEY (wallet, mint)
);
CREATE TABLE IF NOT EXISTS trades (
    sig TEXT, wallet TEXT, mint TEXT, side TEXT,
    token_amount REAL, quote_sol REAL, quote_usd REAL, ts INTEGER, slot INTEGER,
    PRIMARY KEY (sig, wallet, mint)
);
CREATE INDEX IF NOT EXISTS trades_ts ON trades(ts);
CREATE TABLE IF NOT EXISTS tokens (
    mint TEXT PRIMARY KEY, symbol TEXT, name TEXT, price_usd REAL, mcap REAL,
    liquidity REAL, change_h24 REAL, pair_created INTEGER, url TEXT, updated_at INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (mint TEXT PRIMARY KEY, sent_at INTEGER);
"""


NEW_WALLET_COLUMNS = {"new_tokens_per_day": "REAL", "median_hold_min": "REAL", "flag": "TEXT"}


def connect(path=None):
    con = sqlite3.connect(path or config.DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(wallets)")}
    for col, typ in NEW_WALLET_COLUMNS.items():
        if col not in have:
            con.execute(f"ALTER TABLE wallets ADD COLUMN {col} {typ}")
    con.commit()
    return con


def upsert_wallet(con, address, **fields):
    row = con.execute("SELECT address FROM wallets WHERE address=?", (address,)).fetchone()
    if row is None:
        fields.setdefault("added_at", int(time.time()))
        cols = ["address"] + list(fields)
        con.execute(f"INSERT INTO wallets ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [address] + list(fields.values()))
    elif fields:
        sets = ",".join(f"{k}=?" for k in fields)
        con.execute(f"UPDATE wallets SET {sets} WHERE address=?", list(fields.values()) + [address])
    con.commit()


def insert_trades(con, trades):
    con.executemany(
        "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
        [(t.sig, t.wallet, t.mint, t.side, t.token_amount, t.quote_sol,
          t.quote_usd, t.ts, t.slot) for t in trades])
    con.commit()


def upsert_token(con, mint, info):
    if not info:
        return
    con.execute(
        """INSERT INTO tokens VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(mint) DO UPDATE SET symbol=excluded.symbol, name=excluded.name,
           price_usd=excluded.price_usd, mcap=excluded.mcap, liquidity=excluded.liquidity,
           change_h24=excluded.change_h24, pair_created=excluded.pair_created,
           url=excluded.url, updated_at=excluded.updated_at""",
        (mint, info.get("symbol"), info.get("name"), info.get("price_usd"), info.get("mcap"),
         info.get("liquidity"), info.get("change_h24"), info.get("pair_created"),
         info.get("url"), int(time.time())))
    con.commit()
