"""Review what tracked wallets actually did: per-trade % gain over the last N
days, and which tokens more than one of them traded.

P&L per wallet per token = SOL received from sells + current value of what's
still held - SOL spent on buys. Tokens first bought before the window are
skipped (their cost isn't in view). Tokens moved in or out by plain transfers
make the "still held" figure approximate.
"""
import csv
import statistics
import time
from collections import defaultdict

from . import config, db, dexscreener
from .helius import Helius, HeliusError
from .parse import extract_trades


def positions(trades, prices_sol, closed_share=0.9):
    """Per (wallet, mint) position stats from trade dicts. Pure function.

    prices_sol: {mint: current price in SOL or None}.
    """
    pos = defaultdict(lambda: {"sol_in": 0.0, "sol_out": 0.0, "bought": 0.0, "sold": 0.0,
                               "buys": 0, "sells": 0, "first_buy": None, "last_sell": None})
    for t in sorted(trades, key=lambda t: t["ts"]):
        key = (t["wallet"], t["mint"])
        p = pos[key]
        sol = abs(t["quote_sol"] or 0.0)
        if t["side"] == "buy":
            p["sol_in"] += sol
            p["bought"] += t["token_amount"]
            p["buys"] += 1
            p["first_buy"] = p["first_buy"] or t["ts"]
        elif t["side"] == "sell" and p["buys"]:   # ignore sells of pre-window positions
            p["sol_out"] += sol
            p["sold"] += t["token_amount"]
            p["sells"] += 1
            p["last_sell"] = t["ts"]
    out = []
    for (wallet, mint), p in pos.items():
        if not p["buys"] or p["sol_in"] <= 0:
            continue
        held = max(p["bought"] - p["sold"], 0.0)
        price = prices_sol.get(mint)
        held_value = held * price if price else 0.0
        pnl = p["sol_out"] + held_value - p["sol_in"]
        closed = p["sold"] >= closed_share * p["bought"]
        out.append({
            "wallet": wallet, "mint": mint, **p,
            "held_value": held_value, "pnl_sol": pnl,
            "pnl_pct": pnl / p["sol_in"] * 100,
            "status": "closed" if closed else ("open" if price else "open (no price)"),
            "hold_min": ((p["last_sell"] - p["first_buy"]) / 60
                         if closed and p["last_sell"] else None),
        })
    return out


def overlaps(rows, min_wallets=2):
    """Tokens traded by at least `min_wallets` different wallets."""
    by_mint = defaultdict(list)
    for r in rows:
        by_mint[r["mint"]].append(r)
    out = [(m, sorted(rs, key=lambda r: r["first_buy"])) for m, rs in by_mint.items()
           if len({r["wallet"] for r in rs}) >= min_wallets]
    return sorted(out, key=lambda x: (-len(x[1]), x[1][0]["first_buy"]))


def run(days=7, log=print, csv_path="review.csv"):
    con = db.connect()
    hel = Helius()
    wallets = [r["address"] for r in con.execute(
        "SELECT address FROM wallets WHERE tracked=1 ORDER BY hits DESC")]
    if not wallets:
        log("No tracked wallets. Run: python main.py vet")
        return
    since = int(time.time() - days * 86400)
    log(f"Pulling {days} days of trades for {len(wallets)} tracked wallets...")
    trades = []
    for w in wallets:
        try:
            for tx in hel.transactions(w, order="asc", full=True, limit=100, max_txs=3000,
                                       filters={"blockTime": {"gte": since},
                                                "status": "succeeded"}):
                trades += [t for t in extract_trades(tx, w) if t.side in ("buy", "sell")]
        except HeliusError as e:
            log(f"  {w[:8]}… skipped: {e}")
    db.insert_trades(con, trades)
    rows_in = [t.as_dict() for t in trades]

    mints = list({t["mint"] for t in rows_in})
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(mints) if mints else {}
    prices = {m: dexscreener.price_in_sol(p, sol_usd) for m, p in pairs.items()}
    symbols = {m: (p.get("baseToken") or {}).get("symbol") for m, p in pairs.items()}
    rows = positions(rows_in, prices)
    for r in rows:
        r["symbol"] = symbols.get(r["mint"]) or r["mint"][:6]

    def short(a):
        return a[:4] + "…" + a[-4:]

    # --- per wallet --------------------------------------------------------
    log(f"\n=== Per wallet, last {days} days (SOL = ${sol_usd:,.0f}) ===")
    by_w = defaultdict(list)
    for r in rows:
        by_w[r["wallet"]].append(r)
    for w in wallets:
        rs = sorted(by_w.get(w, []), key=lambda r: -r["pnl_pct"])
        if not rs:
            log(f"\n{w}  no buys in the window")
            continue
        closed = [r for r in rs if r["status"] == "closed"]
        wins = [r for r in closed if r["pnl_sol"] > 0]
        total = sum(r["pnl_sol"] for r in rs)
        med = statistics.median(r["pnl_pct"] for r in closed) if closed else None
        log(f"\n{w}")
        log(f"  {len(rs)} tokens bought · {len(closed)} closed · "
            f"win rate {len(wins) / len(closed) * 100:.0f}%" if closed else
            f"  {len(rs)} tokens bought · none closed yet")
        log(f"  total P&L {total:+.2f} SOL (${total * sol_usd:+,.0f})"
            + (f" · median closed trade {med:+.0f}%" if med is not None else ""))
        for r in rs[:8]:
            hold = f"{r['hold_min'] / 60:.1f}h" if r["hold_min"] else ""
            log(f"    {r['symbol']:<12} {r['pnl_pct']:+7.0f}%  {r['sol_in']:6.2f} SOL in  "
                f"{r['status']:<8} {hold}")
        if len(rs) > 8:
            log(f"    … {len(rs) - 8} more in {csv_path}")

    # --- overlap -----------------------------------------------------------
    ov = overlaps(rows)
    log(f"\n=== Tokens traded by 2+ tracked wallets: {len(ov)} ===")
    for mint, rs in ov[:15]:
        sym = rs[0]["symbol"]
        log(f"\n{sym}  ({len({r['wallet'] for r in rs})} wallets)  {mint}")
        for r in rs:
            log(f"    {short(r['wallet'])}  bought {time.strftime('%b %d %H:%M UTC', time.gmtime(r['first_buy']))}"
                f"  {r['sol_in']:6.2f} SOL  {r['pnl_pct']:+6.0f}%  {r['status']}")

    with open(csv_path, "w", newline="") as f:
        cols = ["wallet", "symbol", "mint", "status", "pnl_pct", "pnl_sol", "sol_in",
                "sol_out", "held_value", "buys", "sells", "first_buy", "last_sell", "hold_min"]
        wr = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for r in sorted(rows, key=lambda r: (r["wallet"], -r["pnl_pct"])):
            wr.writerow({**r, "pnl_pct": round(r["pnl_pct"], 1), "pnl_sol": round(r["pnl_sol"], 4)})
    log(f"\nAll positions saved to {csv_path}. Estimated Helius credits used: {hel.credits:,}")
