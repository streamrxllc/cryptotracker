"""Paper trading: act on every signal with fake money and score the results.

Entry: when a signal fires (not flagged late, enough liquidity), "buy"
PAPER_SIZE_USD at the current price plus slippage.
Exit, whichever comes first (all prices include slippage):
  - stop loss:       price falls PAPER_STOP_LOSS_PCT below entry
  - trailing stop:   once up PAPER_TRAIL_ARM_X, sell if it falls
                     PAPER_TRAIL_PCT from its highest price
  - smart money out: half or more of the wallets that triggered the
                     entry have sold since
  - time stop:       held PAPER_MAX_HOLD_HOURS
"""
import json
import time

from . import config, db, dexscreener

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT, symbol TEXT, url TEXT,
    opened_at INTEGER, entry_price REAL, size_usd REAL, liquidity REAL,
    wallets TEXT, score REAL,
    peak_price REAL, last_price REAL,
    closed_at INTEGER, exit_price REAL, exit_reason TEXT
);
"""


def ensure(con):
    con.executescript(SCHEMA)


def should_open(sig, open_mints, recent_mints, cfg=config):
    """Return None if we'd enter this signal, else the reason we wouldn't."""
    if sig["mint"] in open_mints:
        return "already open"
    if sig["mint"] in recent_mints:
        return "traded recently"
    if sig.get("late"):
        return "already ran"
    if (sig.get("liquidity") or 0) < cfg.PAPER_MIN_LIQUIDITY_USD:
        return "liquidity too low"
    if not sig.get("price_usd"):
        return "no price"
    return None


def decide_exit(trade, price, sellers, now, cfg=config):
    """Exit reason for an open trade at `price`, or None to keep holding.

    sellers: how many of the trade's entry wallets have sold since entry.
    """
    entry, peak = trade["entry_price"], max(trade["peak_price"] or 0, price)
    if price <= entry * (1 - cfg.PAPER_STOP_LOSS_PCT / 100):
        return "stop loss"
    if peak >= entry * cfg.PAPER_TRAIL_ARM_X and price <= peak * (1 - cfg.PAPER_TRAIL_PCT / 100):
        return "trailing stop"
    n = len(json.loads(trade["wallets"] or "[]"))
    if n and sellers * 2 >= n:
        return "smart money sold"
    if now - trade["opened_at"] >= cfg.PAPER_MAX_HOLD_HOURS * 3600:
        return "time stop"
    return None


def _sellers_since(con, mint, wallets, since):
    if not wallets:
        return 0
    q = ",".join("?" * len(wallets))
    return con.execute(
        f"SELECT COUNT(DISTINCT wallet) FROM trades WHERE mint=? AND side='sell' "
        f"AND ts>=? AND wallet IN ({q})", [mint, since] + list(wallets)).fetchone()[0]


def update(con, sigs, now=None, prices=None, log=print):
    """Open new paper trades from signals and manage open ones. Returns events."""
    ensure(con)
    now = now or int(time.time())
    slip = config.PAPER_SLIPPAGE_PCT / 100
    events = []

    open_rows = con.execute("SELECT * FROM paper_trades WHERE closed_at IS NULL").fetchall()
    recent = {r[0] for r in con.execute(
        "SELECT mint FROM paper_trades WHERE closed_at >= ?",
        (now - config.PAPER_REENTRY_HOURS * 3600,))}
    if prices is None:
        mints = [r["mint"] for r in open_rows] + [s["mint"] for s in sigs]
        pairs = dexscreener.best_pairs(mints) if mints else {}
        prices = {m: dexscreener.summarize(p) for m, p in pairs.items()}

    # Manage open positions first.
    for t in open_rows:
        info = prices.get(t["mint"]) or {}
        px = info.get("price_usd")
        if not px:
            continue
        sell_px = px * (1 - slip)
        sellers = _sellers_since(con, t["mint"], json.loads(t["wallets"] or "[]"), t["opened_at"])
        reason = decide_exit(t, sell_px, sellers, now)
        peak = max(t["peak_price"] or 0, sell_px)
        if reason:
            con.execute("""UPDATE paper_trades SET closed_at=?, exit_price=?, exit_reason=?,
                           peak_price=?, last_price=? WHERE id=?""",
                        (now, sell_px, reason, peak, sell_px, t["id"]))
            pnl = (sell_px / t["entry_price"] - 1) * 100
            events.append(f"PAPER SELL {t['symbol']}: {pnl:+.0f}% ({reason})")
        else:
            con.execute("UPDATE paper_trades SET peak_price=?, last_price=? WHERE id=?",
                        (peak, sell_px, t["id"]))

    # Then open new ones.
    open_mints = {r["mint"] for r in con.execute(
        "SELECT mint FROM paper_trades WHERE closed_at IS NULL")}
    for s in sigs:
        info = prices.get(s["mint"]) or {}
        s = {**s, "price_usd": info.get("price_usd"),
             "liquidity": info.get("liquidity", s.get("liquidity"))}
        if should_open(s, open_mints, recent):
            continue
        entry = s["price_usd"] * (1 + slip)
        con.execute("""INSERT INTO paper_trades (mint, symbol, url, opened_at, entry_price,
                       size_usd, liquidity, wallets, score, peak_price, last_price)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (s["mint"], s.get("symbol") or s["mint"][:8], s.get("url"), now, entry,
                     config.PAPER_SIZE_USD, s["liquidity"],
                     json.dumps(s.get("buyer_list") or []), s.get("score"), entry, entry))
        open_mints.add(s["mint"])
        events.append(f"PAPER BUY {s.get('symbol') or s['mint'][:8]} at ${entry:.8g} "
                      f"({s['wallets']} wallets, score {s.get('score')})")
    con.commit()
    for e in events:
        log("   " + e)
    return events


def summary(con):
    ensure(con)
    rows = [dict(r) for r in con.execute("SELECT * FROM paper_trades ORDER BY opened_at DESC")]
    closed = [r for r in rows if r["closed_at"]]
    open_ = [r for r in rows if not r["closed_at"]]
    for r in rows:
        px = r["exit_price"] if r["closed_at"] else r["last_price"]
        r["pnl_pct"] = (px / r["entry_price"] - 1) * 100 if px and r["entry_price"] else 0.0
        r["pnl_usd"] = r["size_usd"] * r["pnl_pct"] / 100
    wins = [r for r in closed if r["pnl_pct"] > 0]
    return {
        "open": open_, "closed": closed,
        "realized_usd": sum(r["pnl_usd"] for r in closed),
        "unrealized_usd": sum(r["pnl_usd"] for r in open_),
        "invested_usd": sum(r["size_usd"] for r in rows),
        "win_rate": (len(wins) / len(closed) * 100) if closed else None,
        "best": max(closed, key=lambda r: r["pnl_pct"], default=None),
        "worst": min(closed, key=lambda r: r["pnl_pct"], default=None),
    }


def print_summary(con=None, log=print):
    con = con or db.connect()
    s = summary(con)
    n = len(s["open"]) + len(s["closed"])
    if not n:
        log("No paper trades yet. They open automatically when a signal fires.")
        return
    wr = f"{s['win_rate']:.0f}%" if s["win_rate"] is not None else "–"
    log(f"Paper trades: {n} (${config.PAPER_SIZE_USD:g} each, "
        f"{config.PAPER_SLIPPAGE_PCT:g}% slippage per side)")
    log(f"  Realized P&L:   ${s['realized_usd']:+,.2f} on {len(s['closed'])} closed "
        f"(win rate {wr})")
    log(f"  Unrealized P&L: ${s['unrealized_usd']:+,.2f} on {len(s['open'])} open")
    for r in s["open"] + s["closed"][:20]:
        state = r["exit_reason"] or "open"
        log(f"  {r['symbol']:<10} {r['pnl_pct']:+7.1f}%  {state:<16} "
            f"opened {time.strftime('%b %d %H:%M', time.localtime(r['opened_at']))}")
