"""Backtest "copy a wallet when it buys the same token for the Nth time".

Uses trades already stored (run `review` first). For each wallet+token with at
least N buys: we enter at the price the wallet paid on its Nth buy (plus
slippage), then exit alongside the wallet: its later sells set our exit price
for the share it sold; whatever it still holds is valued at today's price.
No Helius credits used.
"""
import statistics
from collections import defaultdict

from . import config, db, dexscreener


def copy_returns(trades, n, prices_sol, slip=None):
    """[(wallet, mint, return_pct, entry_ts)] for positions with >= n buys. Pure."""
    slip = config.PAPER_SLIPPAGE_PCT / 100 if slip is None else slip
    groups = defaultdict(list)
    for t in trades:
        if t["side"] in ("buy", "sell") and t["token_amount"] > 0:
            groups[(t["wallet"], t["mint"])].append(t)
    out = []
    for (w, m), ts in groups.items():
        ts.sort(key=lambda t: (t["ts"], t["side"] != "buy"))
        buys = [t for t in ts if t["side"] == "buy" and (t["quote_sol"] or 0) > 0]
        if len(buys) < n:
            continue
        nth = buys[n - 1]
        entry = nth["quote_sol"] / nth["token_amount"] * (1 + slip)
        idx = ts.index(nth)
        before, after = ts[:idx + 1], ts[idx + 1:]
        pos = sum(t["token_amount"] for t in before if t["side"] == "buy") - \
            sum(t["token_amount"] for t in before if t["side"] == "sell")
        if pos <= 0:
            continue
        sells = [t for t in after if t["side"] == "sell"]
        bought_after = sum(t["token_amount"] for t in after if t["side"] == "buy")
        sold_tok = sum(t["token_amount"] for t in sells)
        sold_sol = sum(abs(t["quote_sol"] or 0) for t in sells)
        # We mirror the wallet: we sell the same SHARE of the position it sells.
        frac = min(sold_tok / (pos + bought_after), 1.0)
        sell_px = (sold_sol / sold_tok) if sold_tok else 0.0
        cur = prices_sol.get(m)
        if frac < 1 and cur is None:
            cur = 0.0  # no price anywhere: treat what's left as worthless
        value_per_tok = frac * sell_px + (1 - frac) * (cur or 0)
        exit_px = value_per_tok * (1 - slip)
        out.append((w, m, (exit_px / entry - 1) * 100, nth["ts"]))
    return out


def report(rs, label, log=print):
    if not rs:
        log(f"  {label:<28} no positions")
        return
    r = [x[2] for x in rs]
    wins = sum(1 for x in r if x > 0)
    log(f"  {label:<28} n={len(r):3}  win {wins / len(r) * 100:3.0f}%  "
        f"avg {statistics.mean(r):+7.0f}%  median {statistics.median(r):+5.0f}%  "
        f"$100 each -> ${sum(100 * (1 + x / 100) for x in r):,.0f} from ${100 * len(r):,}")


def run(nths=(1, 2, 3, 5), log=print):
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT t.* FROM trades t JOIN wallets w ON w.address=t.wallet WHERE w.tracked=1")]
    if not rows:
        log("No stored trades. Run: python main.py review")
        return
    mints = list({r["mint"] for r in rows})
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(mints)
    prices = {m: dexscreener.price_in_sol(p, sol_usd) for m, p in pairs.items()}

    pnl = defaultdict(float)  # rank wallets by their own P&L to find leaders
    for r in rows:
        if r["side"] == "buy":
            pnl[r["wallet"]] -= abs(r["quote_sol"] or 0)
        elif r["side"] == "sell":
            pnl[r["wallet"]] += abs(r["quote_sol"] or 0)
    log(f"Copy-at-Nth-buy backtest over {len(rows):,} stored trades "
        f"({config.PAPER_SLIPPAGE_PCT:g}% slippage each way; exits follow the wallet)\n")
    log("All tracked wallets:")
    for n in nths:
        report(copy_returns(rows, n, prices), f"enter at buy #{n}", log)
    leaders = [w for w, _ in sorted(pnl.items(), key=lambda x: -x[1])[:3]]
    for w in leaders:
        log(f"\nOnly {w[:4]}…{w[-4:]}:")
        sub = [r for r in rows if r["wallet"] == w]
        for n in nths:
            report(copy_returns(sub, n, prices), f"enter at buy #{n}", log)
    log("\nCaveats: one week of data; real copies enter minutes later than the wallet;")
    log("a strategy needs many more trades before trusting it.")
