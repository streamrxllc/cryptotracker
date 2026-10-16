"""Step 1: find wallets that bought several past winners cheaply."""
import os
import statistics
from collections import defaultdict

from . import config, db, dexscreener
from .helius import Helius
from .parse import trader_trade


def scan_token(txs, mint, ref_price_sol, sol_usd=None, cfg=config):
    """Walk a token's transactions oldest-first and collect early buyers.

    Returns (per-wallet stats dict, meta dict). Pure function: no network.
    A buy is "early" if ref_price / price_paid >= MIN_ENTRY_MULTIPLE.
    Scanning stops once recent trades are no longer cheap, or limits hit.
    """
    stats = defaultdict(lambda: {"buys": 0, "sol_in": 0.0, "tokens": 0.0,
                                 "first_ts": None, "sold_sol": 0.0})
    launch_ts = creator = None
    recent = []
    expensive_run = 0
    read = 0
    stop_reason = "end of history"
    cheap_cutoff = ref_price_sol / cfg.MIN_ENTRY_MULTIPLE

    for tx in txs:
        read += 1
        ts = int(tx.get("blockTime") or 0)
        if launch_ts is None:
            launch_ts = ts
            keys = tx["transaction"]["message"]["accountKeys"]
            k0 = keys[0]
            creator = k0["pubkey"] if isinstance(k0, dict) else k0
        if ts - launch_ts > cfg.MAX_SCAN_HOURS * 3600:
            stop_reason = "max hours"
            break
        if read > cfg.MAX_SCAN_TXS:
            stop_reason = "max txs"
            break

        t = trader_trade(tx, mint)
        if t is None:
            continue
        price = t.price_sol(sol_usd)
        if price:
            recent.append(price)
            recent = recent[-40:]
        if t.wallet == creator:
            continue
        if t.side == "sell":
            if t.wallet in stats:
                stats[t.wallet]["sold_sol"] += t.sol_value(sol_usd)
            continue
        if ts - launch_ts < cfg.SNIPE_SECONDS or not price:
            continue
        if price <= cheap_cutoff:
            s = stats[t.wallet]
            s["buys"] += 1
            s["sol_in"] += t.sol_value(sol_usd)
            s["tokens"] += t.token_amount
            s["first_ts"] = s["first_ts"] or ts

        # Cheap zone over: rolling median stays above the cutoff for a long
        # stretch (a brief early spike doesn't end the scan).
        if price and len(recent) >= 40:
            if statistics.median(recent) > cheap_cutoff:
                expensive_run += 1
            else:
                expensive_run = 0
            if expensive_run >= cfg.EXPENSIVE_RUN_STOP:
                stop_reason = "price left cheap zone"
                break

    out = {}
    for w, s in stats.items():
        if s["sol_in"] < cfg.MIN_POSITION_SOL or s["tokens"] <= 0:
            continue
        avg_price = s["sol_in"] / s["tokens"]
        out[w] = {**s, "avg_multiple": ref_price_sol / avg_price}
    return out, {"read": read, "stop": stop_reason, "launch_ts": launch_ts,
                 "creator": creator}


def read_winners(paths=None):
    """Lines of `MINT` or `MINT PEAK_PRICE_USD`. Returns {mint: peak_usd|None}.

    Reads the auto-generated list, then your own winners.txt (which wins on
    duplicates).
    """
    out = {}
    for path in paths or (config.WINNERS_AUTO_FILE, config.WINNERS_FILE):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                parts = line.split("#")[0].split()
                if not parts:
                    continue
                out[parts[0]] = float(parts[1]) if len(parts) > 1 else None
    return out


def run(log=print):
    con = db.connect()
    hel = Helius()
    winners = read_winners()
    mints = list(winners)
    if not mints:
        log("No winners yet. Run: python main.py winners  (or add mints to winners.txt)")
        return
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(mints)
    log(f"SOL = ${sol_usd:,.2f}. Scanning {len(mints)} winners...\n")

    for mint in mints:
        pair = pairs.get(mint)
        if not pair:
            log(f"  {mint[:8]}…  not found on Dexscreener, skipped")
            continue
        info = dexscreener.summarize(pair)
        db.upsert_token(con, mint, info)
        # Reference price: the peak you typed in winners.txt, else today's price.
        ref = (winners[mint] / sol_usd) if winners[mint] else \
            dexscreener.price_in_sol(pair, sol_usd)
        if not ref:
            log(f"  {info['symbol']}: no price, skipped")
            continue
        txs = hel.transactions(mint, order="asc", full=True, limit=100,
                               filters={"status": "succeeded"})
        found, meta = scan_token(txs, mint, ref, sol_usd)
        con.execute("DELETE FROM discovery_hits WHERE mint=?", (mint,))
        con.executemany(
            "INSERT INTO discovery_hits VALUES (?,?,?,?,?,?,?)",
            [(w, mint, info["symbol"], s["first_ts"], s["buys"], s["sol_in"],
              s["avg_multiple"]) for w, s in found.items()])
        con.commit()
        log(f"  {info['symbol']:<12} read {meta['read']:>6} txs ({meta['stop']}), "
            f"{len(found)} early buyers   [~{hel.credits:,} credits used so far]")

    # Roll up: wallets early on multiple winners.
    rows = con.execute("""
        SELECT wallet, COUNT(*) AS hits, AVG(avg_multiple) AS mult, SUM(sol_in) AS sol
        FROM discovery_hits GROUP BY wallet HAVING hits >= ?
        ORDER BY hits DESC, mult DESC""", (config.MIN_HITS,)).fetchall()
    for r in rows:
        db.upsert_wallet(con, r["wallet"], source="discovered", hits=r["hits"],
                         avg_multiple=r["mult"])
    log(f"\n{len(rows)} wallets were early on {config.MIN_HITS}+ winners.")
    for r in rows[:15]:
        log(f"  {r['wallet']}  hits={r['hits']}  avg entry {r['mult']:.0f}x below "
            f"reference  ({r['sol']:.1f} SOL in)")
    log(f"\nEstimated Helius credits used: {hel.credits:,}")
    log("Next: python main.py vet")
