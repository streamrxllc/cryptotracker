"""Scout: find low-activity wallets with very high win rates, and the groups
they trade in.

Hypothesis: a wallet that makes few trades but wins ~90%+ of them is likely
part of a coordinated group (insiders / callers). We scan the discovered
candidates, INCLUDING ones vetting dropped as flippers or clusters (that's
where such groups would hide), over the last N days.

Honest win rate: a position still open but down more than DEAD_PCT counts
as a loss, so wallets can't look perfect by never selling their losers.
Groups: wallets that repeatedly first-buy the same token within
COBUY_SECONDS of each other.
"""
import csv
import statistics
import time
from collections import defaultdict

from . import config, db, dexscreener
from .helius import Helius, HeliusError
from .parse import extract_trades
from .review import positions

DEAD_PCT = -50          # open and down this much = loss
COBUY_SECONDS = 120     # "bought together"
MIN_SHARED = 2          # co-buys on this many tokens = same group


def score(rows, min_trades, max_trades, min_win):
    """Per-wallet stats from position rows (review.positions output). Pure."""
    by = defaultdict(list)
    for r in rows:
        by[r["wallet"]].append(r)
    out = []
    for w, rs in by.items():
        decided = [r for r in rs if r["status"] == "closed" or r["pnl_pct"] <= DEAD_PCT]
        if not (min_trades <= len(decided) <= max_trades):
            continue
        wins = [r for r in decided if r["pnl_pct"] > 0]
        rate = len(wins) / len(decided)
        holds = [r["hold_min"] for r in decided if r.get("hold_min") is not None]
        out.append({
            "wallet": w, "trades": len(decided), "open": len(rs) - len(decided),
            "win_rate": rate, "pass": rate >= min_win,
            "median_gain": statistics.median(r["pnl_pct"] for r in decided),
            "net_sol": sum(r["pnl_sol"] for r in rs),
            "median_hold_min": statistics.median(holds) if holds else None,
            "tokens": [r["mint"] for r in rs],
        })
    return sorted(out, key=lambda s: (-s["win_rate"], -s["net_sol"]))


def groups(rows, wallets, window=COBUY_SECONDS, min_shared=MIN_SHARED):
    """Connected groups of wallets that co-bought >= min_shared tokens. Pure."""
    first = defaultdict(dict)                       # mint -> wallet -> first buy ts
    for r in rows:
        if r["wallet"] in wallets and r.get("first_buy"):
            first[r["mint"]][r["wallet"]] = r["first_buy"]
    shared = defaultdict(set)
    for m, buyers in first.items():
        ws = sorted(buyers.items(), key=lambda x: x[1])
        for i, (a, ta) in enumerate(ws):
            for b, tb in ws[i + 1:]:
                if tb - ta > window:
                    break
                shared[tuple(sorted((a, b)))].add(m)
    parent = {w: w for w in wallets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for (a, b), ms in shared.items():
        if len(ms) >= min_shared:
            parent[find(a)] = find(b)
    g = defaultdict(set)
    for w in wallets:
        g[find(w)].add(w)
    out = [sorted(ws) for ws in g.values() if len(ws) >= 2]
    return sorted(out, key=len, reverse=True), shared


def run(days=30, top=300, min_trades=5, max_trades=40, min_win=0.9, log=print,
        csv_path="scout.csv"):
    con = db.connect()
    hel = Helius()
    cands = [r["address"] for r in con.execute(
        """SELECT address FROM wallets WHERE source='discovered' AND hits >= 2
           ORDER BY hits DESC, avg_multiple DESC LIMIT ?""", (top,))]
    if not cands:
        log("No discovered wallets. Run: python main.py discover")
        return
    since = int(time.time() - days * 86400)
    log(f"Scouting {len(cands)} discovered wallets over {days} days "
        f"(up to {max_trades * 3} txs each; est. {len(cands) * 15:,}–{len(cands) * 40:,} credits)...")
    trades = []
    for i, w in enumerate(cands, 1):
        try:
            # Busy wallets can't qualify (too many trades), so cap the read.
            for tx in hel.transactions(w, order="desc", full=True, limit=100,
                                       max_txs=max_trades * 6,
                                       filters={"blockTime": {"gte": since},
                                                "status": "succeeded"}):
                trades += [t.as_dict() for t in extract_trades(tx, w)
                           if t.side in ("buy", "sell")]
        except HeliusError as e:
            log(f"  {w[:8]}… skipped: {e}")
        if i % 50 == 0:
            log(f"  {i}/{len(cands)} scanned  [~{hel.credits:,} credits]")
    trades = [t for t in trades if t["mint"] not in config.NON_MEME_MINTS]

    mints = list({t["mint"] for t in trades})
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(mints) if mints else {}
    prices = {m: dexscreener.price_in_sol(p, sol_usd) for m, p in pairs.items()}
    sym = {m: (p.get("baseToken") or {}).get("symbol") for m, p in pairs.items()}
    rows = positions(trades, prices)

    stats = score(rows, min_trades, max_trades, min_win)
    hits = [s for s in stats if s["pass"]]
    grp, shared = groups(rows, {s["wallet"] for s in hits})
    flags = {r["address"]: r["flag"] for r in con.execute("SELECT address, flag FROM wallets")}

    def short(a):
        return f"{a[:4]}…{a[-4:]}"

    def hold(m):
        if m is None:
            return "–"
        return f"{m:.0f}m" if m < 120 else f"{m / 60:.1f}h"

    log(f"\n{len(stats)} wallets made {min_trades}–{max_trades} decided trades in {days} days; "
        f"{len(hits)} won {min_win * 100:.0f}%+ of them.\n")
    for s in hits[:30]:
        log(f"  {s['wallet']}  {s['win_rate'] * 100:3.0f}% of {s['trades']:>2}  "
            f"median {s['median_gain']:+5.0f}%  net {s['net_sol']:+7.1f} SOL  "
            f"hold {hold(s['median_hold_min']):>6}  vetting: {flags.get(s['wallet']) or 'passed'}")

    log(f"\nGroups (wallets co-buying {MIN_SHARED}+ of the same tokens within "
        f"{COBUY_SECONDS}s): {len(grp)}")
    for g in grp[:10]:
        ms = set()
        for i, a in enumerate(g):
            for b in g[i + 1:]:
                ms |= shared.get(tuple(sorted((a, b))), set())
        names = ", ".join((sym.get(m) or m[:6]) for m in list(ms)[:6])
        log(f"  {len(g)} wallets: {', '.join(short(w) for w in g)}  · together on: {names}")

    others = [s for s in stats if not s["pass"]]
    if others and hits:
        log(f"\nFor comparison, the other {len(others)} low-activity wallets: median win rate "
            f"{statistics.median(s['win_rate'] for s in others) * 100:.0f}%, "
            f"net {sum(s['net_sol'] for s in others):+.0f} SOL total "
            f"vs {sum(s['net_sol'] for s in hits):+.0f} SOL for the 90%+ group.")

    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["wallet", "win_rate", "trades", "open", "median_gain_pct", "net_sol",
                     "median_hold_min", "vetting_flag", "passes"])
        for s in stats:
            wr.writerow([s["wallet"], round(s["win_rate"], 3), s["trades"], s["open"],
                         round(s["median_gain"], 1), round(s["net_sol"], 3),
                         s["median_hold_min"], flags.get(s["wallet"]) or "", s["pass"]])
    log(f"\nAll {len(stats)} wallets saved to {csv_path}. Est. Helius credits: {hel.credits:,}")
    log("To watch some: python main.py add WALLET scout   "
        "(or add them to WATCH_WALLETS for alerts)")
