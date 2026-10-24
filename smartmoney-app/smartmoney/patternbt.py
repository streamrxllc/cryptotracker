"""Backtest the "BORDR pattern": was a coin being accumulated by several
patient, independent wallets BEFORE it ran, more than coins that went nowhere?

For each coin (past winners + a random sample of other coins our wallets
traded), we replay its trades from launch until it first reaches CHECK_MCAP
(the "checkpoint", e.g. $300k), score the accumulation in the WINDOW hours
before that, then look up how high it went afterwards (GeckoTerminal daily
closes). Runners (10x+ from the checkpoint) vs duds (<2x) are compared.
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


def run(controls=40, check_mcap=300_000, window_h=72, runner_x=10, max_txs=6000, seed=7,
        log=print, csv_path="patternbt.csv"):
    con = db.connect()
    hel = Helius()
    winners = list(read_winners())
    others = [r[0] for r in con.execute("SELECT DISTINCT mint FROM trades")]
    skip = set(winners) | config.QUOTE_MINTS | config.NON_MEME_MINTS
    others = [m for m in others if m not in skip]
    random.Random(seed).shuffle(others)
    sol_usd = dexscreener.sol_usd()
    log(f"Backtesting the accumulation pattern: {len(winners)} past winners + up to "
        f"{controls} other coins, checkpoint at ${check_mcap:,.0f} market cap, "
        f"{window_h:g}h window. Up to ~{max_txs // 10:,} credits per coin; "
        f"GeckoTerminal adds ~15s per coin.")

    rows, tried_controls = [], 0
    skipped = defaultdict(int)
    queue = [(m, "winner") for m in winners] + [(m, "control") for m in others]
    for mint, source in queue:
        if source == "control" and tried_controls >= controls:
            break
        if source == "control":
            tried_controls += 1
        pool = gecko.token_best_pool(mint)
        if not pool:
            skipped["no price history"] += 1
            continue
        try:
            supply = float((hel.rpc("getTokenSupply", [mint]) or {}).get("value", {})
                           .get("uiAmountString") or 0)
        except HeliusError:
            continue
        if not supply:
            continue
        try:
            txs = hel.transactions(mint, order="asc", full=True, limit=100, max_txs=max_txs,
                                   filters={"status": "succeeded"})
            cp = scan_to_checkpoint(txs, mint, supply, sol_usd, check_mcap)
        except HeliusError as e:
            log(f"  {mint[:6]}… skipped: {e}")
            continue
        if not cp:
            skipped[f"didn't reach ${check_mcap:,.0f} within {max_txs:,} txs"] += 1
            continue
        x = outcome(gecko.daily_ohlcv(pool["pool"]), cp["checkpoint_ts"], supply,
                    cp["checkpoint_mcap"])
        if x is None:
            continue
        f = features(cp["trades"], cp["checkpoint_ts"], cp["launch_ts"], cp["creator"],
                     window_h)
        label = "runner" if x >= runner_x else ("dud" if x < 2 else "middle")
        rows.append({"mint": mint, "name": pool["name"], "source": source, "label": label,
                     "outcome_x": x, **f})
        log(f"  {(pool['name'] or mint[:6])[:22]:<22} {source:<7} → {x:6.1f}x  "
            f"{label:<6}  accumulators {f['n_accum']:>2}  share {f['accum_share']*100:3.0f}%  "
            f"[~{hel.credits:,} credits]")

    runners = [r for r in rows if r["label"] == "runner"]
    duds = [r for r in rows if r["label"] == "dud"]
    if skipped:
        log("\nSkipped: " + ", ".join(f"{n} {why}" for why, n in skipped.items()) +
            ". (Raise --max-txs if many winners hit the cap.)")
    log(f"\n{len(rows)} coins scored: {len(runners)} runners ({runner_x:g}x+ after the "
        f"checkpoint), {len(duds)} duds (<2x), {len(rows) - len(runners) - len(duds)} in between.")
    if not runners or not duds:
        log("Not enough of both groups to compare. Try --controls 80 or a lower --mcap.")
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
        cols = ["mint", "name", "source", "label", "outcome_x", "n_accum", "accum_share",
                "accum_sol", "net_flow", "unique_buyers", "top10_share", "age_h", "buy_sol"]
        wr = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    log(f"\nSaved to {csv_path}. Est. Helius credits: {hel.credits:,}")
