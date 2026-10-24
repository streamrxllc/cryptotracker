"""Backtest the "BORDR pattern": was a coin being accumulated by several
patient, independent wallets BEFORE it ran, more than coins that went nowhere?

This mirrors what an hourly scanner would see. For each coin (past winners +
other memecoins our wallets traded), the checkpoint is the first day the coin
is at least MIN_AGE_DAYS old with a market cap between LO and HI (e.g. $100k–
$1.5M): old enough to be past the launch frenzy, small enough to still run.
We score accumulation in the WINDOW hours before that day, then look up the
highest daily close afterwards. Runners (10x+) are compared with duds (<2x).
Coins that were already above HI by MIN_AGE_DAYS are counted separately:
no scanner of this kind could have caught them.
"""
import csv
import random
import statistics
from collections import defaultdict

from . import config, db, dexscreener, gecko
from .discover import read_winners
from .helius import Helius, HeliusError
from .parse import trader_trade

SNIPE_SECONDS = 60
MIN_BUY_SOL = 0.05


def scan_to_checkpoint(txs, mint, supply, sol_usd, check_mcap, min_age_s=3600):
    """Replay a coin's transactions (oldest first) until its market cap first
    holds above check_mcap. Pure. Returns dict or None if never reached."""
    trades, recent = [], []
    launch = creator = None
    read = 0
    for tx in txs:
        read += 1
        ts = int(tx.get("blockTime") or 0)
        if launch is None:
            launch = ts
            k0 = tx["transaction"]["message"]["accountKeys"][0]
            creator = k0["pubkey"] if isinstance(k0, dict) else k0
        t = trader_trade(tx, mint)
        if t is None:
            continue
        p = t.price_sol(sol_usd)
        trades.append({"wallet": t.wallet, "side": t.side, "sol": t.sol_value(sol_usd),
                       "amt": t.token_amount, "ts": ts})
        if p:
            recent = (recent + [p])[-5:]
        # Median of the last 5 prices, so one odd trade can't fake the crossing.
        if len(recent) == 5 and ts - launch >= min_age_s:
            mcap = statistics.median(recent) * supply * sol_usd
            if mcap >= check_mcap:
                return {"trades": trades, "checkpoint_ts": ts, "checkpoint_mcap": mcap,
                        "launch_ts": launch, "creator": creator, "read": read}
    return None


def features(trades, checkpoint_ts, launch_ts, creator, window_h=72):
    """Accumulation features in the window before the checkpoint. Pure."""
    since = max(checkpoint_ts - window_h * 3600, launch_ts + SNIPE_SECONDS)
    w = defaultdict(lambda: {"buys": 0, "sells": 0, "sol_in": 0.0, "sol_out": 0.0,
                             "bought": 0.0, "sold": 0.0, "first": None, "last": None, "n": 0})
    buy_sol = sell_sol = 0.0
    for t in trades:
        if t["ts"] < since or t["wallet"] == creator:
            continue
        s = w[t["wallet"]]
        s["n"] += 1
        if t["side"] == "buy" and t["sol"] >= MIN_BUY_SOL:
            s["buys"] += 1
            s["sol_in"] += t["sol"]
            s["bought"] += t["amt"]
            s["first"] = s["first"] or t["ts"]
            s["last"] = t["ts"]
            buy_sol += t["sol"]
        elif t["side"] == "sell":
            s["sells"] += 1
            s["sol_out"] += t["sol"]
            s["sold"] += t["amt"]
            sell_sol += t["sol"]
    acc = [s for s in w.values()
           if s["buys"] >= 3 and s["n"] <= 30                       # many trades = bot/MM
           and (s["last"] - s["first"]) >= 2 * 3600
           and s["sold"] <= 0.2 * s["bought"] and s["sol_in"] >= 0.5]
    buyers = sorted((s["sol_in"] for s in w.values() if s["sol_in"] > 0), reverse=True)
    return {
        "age_h": (checkpoint_ts - launch_ts) / 3600,
        "unique_buyers": len(buyers),
        "n_accum": len(acc),
        "accum_sol": sum(s["sol_in"] for s in acc),
        "accum_share": sum(s["sol_in"] for s in acc) / buy_sol if buy_sol else 0.0,
        "net_flow": (buy_sol - sell_sol) / (buy_sol + sell_sol) if (buy_sol + sell_sol) else 0.0,
        "top10_share": sum(buyers[:10]) / sum(buyers) if buyers else 0.0,
        "buy_sol": buy_sol,
    }


def outcome(candles, checkpoint_ts, supply, check_mcap):
    """Highest daily close after the checkpoint, as a multiple of it. Pure."""
    after = [c for c in candles if c[0] >= checkpoint_ts - 86400]
    if not after:
        return None
    return max(c[4] for c in after) * supply / check_mcap


def find_checkpoint(candles, launch_ts, supply, min_age_days, lo, hi):
    """First day >= min_age_days after launch whose opening market cap (prior
    day's close) is within [lo, hi]. Pure. Returns (day_ts, mcap), or
    ("ran", mcap) if it passed hi before ever being in range, or None."""
    candles = sorted(candles, key=lambda c: c[0])
    for prev, cur in zip(candles, candles[1:]):
        if cur[0] < launch_ts + min_age_days * 86400:
            continue
        mcap = prev[4] * supply
        if lo <= mcap <= hi:
            return cur[0], mcap
        if mcap > hi:
            return "ran", mcap
    return None


def run(controls=60, lo=100_000, hi=1_500_000, min_age_days=2, window_h=48, runner_x=10,
        max_txs=10_000, max_age_days=150, seed=7, log=print, csv_path="patternbt.csv"):
    import time as _time
    con = db.connect()
    hel = Helius()
    winners = list(read_winners())
    others = [r[0] for r in con.execute("SELECT DISTINCT mint FROM trades")]
    skip = set(winners) | config.QUOTE_MINTS | config.NON_MEME_MINTS
    others = [m for m in others if m not in skip]
    random.Random(seed).shuffle(others)
    sol_usd = dexscreener.sol_usd()
    oldest = _time.time() - max_age_days * 86400
    log(f"Backtesting the accumulation pattern on {len(winners)} past winners + up to "
        f"{controls} other memecoins. Checkpoint: first day a coin is {min_age_days:g}+ days "
        f"old at ${lo:,.0f}–${hi:,.0f}; scoring the {window_h:g}h before it. "
        f"Up to ~{max_txs // 10:,} credits and ~15s per coin.")

    rows, tried, skipped = [], 0, defaultdict(int)
    queue = [(m, "winner") for m in winners] + [(m, "control") for m in others]
    for mint, source in queue:
        if source == "control" and tried >= controls:
            break
        pool = gecko.token_best_pool(mint)
        if not pool:
            skipped["no price history"] += 1
            continue
        if source == "control":
            # Only memecoins like the winners: SOL-paired, launched in the window.
            if pool["quote"] not in config.SOL_QUOTES or not pool["created"] \
                    or pool["created"] < oldest:
                skipped["not a recent SOL-paired coin"] += 1
                continue
            tried += 1
        try:
            supply = float((hel.rpc("getTokenSupply", [mint]) or {}).get("value", {})
                           .get("uiAmountString") or 0)
        except HeliusError:
            supply = 0
        if not supply:
            skipped["no supply"] += 1
            continue
        candles = gecko.daily_ohlcv(pool["pool"])
        cp = find_checkpoint(candles, pool["created"] or 0, supply, min_age_days, lo, hi)
        if cp is None:
            skipped[f"never in ${lo:,.0f}–${hi:,.0f} after day {min_age_days:g}"] += 1
            continue
        if cp[0] == "ran":
            skipped[f"already above ${hi:,.0f} by day {min_age_days:g} ({source})"] += 1
            continue
        cp_ts, cp_mcap = cp
        trades, read = [], 0
        try:
            for tx in hel.transactions(mint, order="asc", full=True, limit=100,
                                       max_txs=max_txs,
                                       filters={"status": "succeeded",
                                                "blockTime": {"gte": int(cp_ts - window_h * 3600),
                                                              "lt": int(cp_ts)}}):
                read += 1
                t = trader_trade(tx, mint)
                if t:
                    trades.append({"wallet": t.wallet, "side": t.side,
                                   "sol": t.sol_value(sol_usd), "amt": t.token_amount,
                                   "ts": int(tx.get("blockTime") or 0)})
        except HeliusError as e:
            log(f"  {mint[:6]}… skipped: {e}")
            continue
        f = features(trades, cp_ts, pool["created"] or 0, None, window_h)
        after = [c for c in candles if c[0] >= cp_ts]
        x = max(c[4] for c in after) * supply / cp_mcap if after else None
        if x is None:
            continue
        label = "runner" if x >= runner_x else ("dud" if x < 2 else "middle")
        rows.append({"mint": mint, "name": pool["name"], "source": source, "label": label,
                     "outcome_x": x, "checkpoint_mcap": cp_mcap, "truncated": read >= max_txs,
                     "window_txs": read, **f})
        log(f"  {(pool['name'] or mint[:6])[:22]:<22} {source:<7} ${cp_mcap / 1e3:6.0f}k → "
            f"{x:6.1f}x {label:<6} accumulators {f['n_accum']:>2}  share "
            f"{f['accum_share'] * 100:3.0f}%  [~{hel.credits:,} credits]")
    runners = [r for r in rows if r["label"] == "runner"]
    duds = [r for r in rows if r["label"] == "dud"]
    if skipped:
        log("\nNot scored: " + "; ".join(f"{n} {why}" for why, n in skipped.items()) + ".")
    log(f"\n{len(rows)} coins scored: {len(runners)} runners ({runner_x:g}x+ after the "
        f"checkpoint), {len(duds)} duds (<2x), {len(rows) - len(runners) - len(duds)} in between.")
    if not runners or not duds:
        log("Not enough of both groups to compare. Try --controls 120, or widen --lo/--hi.")
    else:
        log("\nMedian at the checkpoint      runners    duds")
        for k, lab, fmt in [("n_accum", "patient accumulators", "{:.0f}"),
                            ("accum_share", "their share of buying", "{:.0%}"),
                            ("accum_sol", "their SOL", "{:.1f}"),
                            ("net_flow", "net buy flow", "{:+.2f}"),
                            ("unique_buyers", "unique buyers", "{:.0f}"),
                            ("top10_share", "top-10 buyer share", "{:.0%}"),
                            ("age_h", "coin age (hours)", "{:.0f}")]:
            a = statistics.median(r[k] for r in runners)
            b = statistics.median(r[k] for r in duds)
            log(f"  {lab:<26} {fmt.format(a):>8} {fmt.format(b):>8}")
        base = len(runners) / (len(runners) + len(duds))
        log(f"\nRule check (base rate: {base:.0%} of runners+duds are runners):")
        for k in (1, 2, 3, 5):
            pr = sum(r["n_accum"] >= k for r in runners)
            pd = sum(r["n_accum"] >= k for r in duds)
            hit = pr / (pr + pd) if (pr + pd) else 0
            log(f"  {k}+ patient accumulators: catches {pr}/{len(runners)} runners, "
                f"{pd}/{len(duds)} duds → {hit:.0%} of flagged coins ran")
    with open(csv_path, "w", newline="") as fh:
        cols = ["mint", "name", "source", "label", "outcome_x", "checkpoint_mcap", "n_accum",
                "accum_share", "accum_sol", "net_flow", "unique_buyers", "top10_share", "age_h",
                "buy_sol", "window_txs", "truncated"]
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    log(f"\nSaved to {csv_path}. Est. Helius credits: {hel.credits:,}")
