"""Holder-side search: who built a position slowly in a coin that ran, and is
still holding it?

1. Coins: the winners list (plus any you pass), kept if still liquid and not
   collapsed from their peak.
2. Each coin's 20 largest holder accounts (getTokenLargestAccounts) and their
   owners. Owners that aren't ordinary wallets (pools, program accounts) are
   dropped: a normal wallet is an existing System-program account.
3. For each remaining holder, its history for that one token: how many buys,
   how spread out, average price paid, tokens received by transfer instead of
   bought, and how much it has sold.
4. Conviction = bought (not given) the position, in 2+ buys spread over 2h+,
   not in the launch minute, now worth 2x+ its average cost, and has sold at
   most half.
5. Wallets that show conviction in 2+ different coins rank first; busy wallets
   (exchanges, bots) are dropped with a cheap activity count.
"""
import csv
import time
from collections import defaultdict

from . import config, db, dexscreener
from .discover import read_winners
from .helius import HeliusError, Helius
from .parse import extract_trades

SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"


def owners_of(hel, token_accounts):
    """{token_account: owner} via getMultipleAccounts (jsonParsed)."""
    out = {}
    for i in range(0, len(token_accounts), 100):
        chunk = token_accounts[i:i + 100]
        res = hel.rpc("getMultipleAccounts", [chunk, {"encoding": "jsonParsed"}])
        for addr, acc in zip(chunk, (res or {}).get("value") or []):
            try:
                out[addr] = acc["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                pass
    return out


def wallet_like(hel, owners):
    """Subset of owners that are ordinary wallets (existing System accounts)."""
    keep = set()
    owners = [o for o in owners if o != INCINERATOR]
    for i in range(0, len(owners), 100):
        chunk = owners[i:i + 100]
        res = hel.rpc("getMultipleAccounts", [chunk, {"encoding": "base64"}])
        for addr, acc in zip(chunk, (res or {}).get("value") or []):
            if acc and acc.get("owner") == SYSTEM_PROGRAM:
                keep.add(addr)
    return keep


def token_history(hel, wallet, mint, max_txs=600):
    """A wallet's transactions touching one token. Uses Helius's tokenTransfer
    filter; if the plan/API rejects it, falls back to all token-balance-changing
    transactions (more credits; the caller filters by mint)."""
    base = {"status": "succeeded", "tokenAccounts": "balanceChanged"}
    try:
        return list(hel.transactions(wallet, order="asc", full=True, limit=100,
                                     max_txs=max_txs,
                                     filters={**base, "tokenTransfer": {"mint": mint}}))
    except HeliusError:
        return list(hel.transactions(wallet, order="asc", full=True, limit=100,
                                     max_txs=max_txs * 3, filters=base))


def position_story(trades, mint, launch_ts, price_now_sol, holding):
    """How a wallet built its position in one token. Pure.

    trades: Trade dicts for this wallet (any tokens); holding: tokens held now.
    """
    ts = sorted((t for t in trades if t["mint"] == mint), key=lambda t: t["ts"])
    buys = [t for t in ts if t["side"] == "buy" and (t["quote_sol"] or 0) > 0]
    got = sum(t["token_amount"] for t in ts if t["side"] == "transfer_in")
    bought = sum(t["token_amount"] for t in buys)
    sold = sum(t["token_amount"] for t in ts if t["side"] == "sell")
    sol_in = sum(t["quote_sol"] for t in buys)
    avg = sol_in / bought if bought else None
    first = buys[0]["ts"] if buys else None
    return {
        "buys": len(buys), "sells": sum(1 for t in ts if t["side"] == "sell"),
        "span_h": (buys[-1]["ts"] - first) / 3600 if buys else 0.0,
        "first_buy": first,
        "launch_buyer": bool(first and launch_ts and first - launch_ts < 120),
        "sol_in": sol_in, "avg_price_sol": avg,
        "bought_share": bought / (bought + got) if (bought + got) else 0.0,
        "sold_share": sold / bought if bought else 0.0,
        "multiple": (price_now_sol / avg) if (avg and price_now_sol) else None,
        "value_sol": holding * price_now_sol if price_now_sol else None,
    }


def is_conviction(s, min_buys=2, min_span_h=2, min_multiple=2.0):
    return (s["buys"] >= min_buys and s["span_h"] >= min_span_h and not s["launch_buyer"]
            and s["bought_share"] >= 0.8 and s["sold_share"] <= 0.5
            and (s["multiple"] or 0) >= min_multiple)


def run(extra_mints=(), top=20, min_multiple=2.0, max_drawdown=0.8, max_tx_30d=3000,
        log=print, csv_path="holders.csv"):
    hel = Helius()
    con = db.connect()
    winners = read_winners()
    for m in extra_mints:
        winners.setdefault(m, None)
    if not winners:
        log("No coins to check. Run: python main.py winners, or pass --mints")
        return
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(list(winners))
    coins = []
    for m, peak in winners.items():
        info = dexscreener.summarize(pairs.get(m))
        px = info.get("price_usd")
        if not px or (info.get("liquidity") or 0) < config.PAPER_MIN_LIQUIDITY_USD:
            continue
        dd = 1 - px / peak if peak else 0.0
        if dd > max_drawdown and m not in extra_mints:
            continue
        coins.append({"mint": m, **info, "drawdown": dd,
                      "price_sol": dexscreener.price_in_sol(pairs[m], sol_usd)})
    log(f"Checking the top {top} holders of {len(coins)} coins that ran and are still "
        f"liquid and within {max_drawdown * 100:.0f}% of their peak...")

    stories = []           # one row per (wallet, coin)
    for c in coins:
        try:
            res = hel.rpc("getTokenLargestAccounts", [c["mint"]])
        except HeliusError as e:
            log(f"  {c['symbol']}: holders unavailable ({e})")
            continue
        accts = [(a["address"], float(a.get("uiAmountString") or a.get("uiAmount") or 0))
                 for a in (res or {}).get("value") or []][:top]
        owner = owners_of(hel, [a for a, _ in accts])
        held = defaultdict(float)
        for a, amt in accts:
            if a in owner:
                held[owner[a]] += amt
        wallets = wallet_like(hel, list(held))
        for w in wallets:
            trades = []
            try:
                for tx in token_history(hel, w, c["mint"]):
                    trades += [t.as_dict() for t in extract_trades(tx, w)]
            except HeliusError as e:
                log(f"  {w[:8]}… {c['symbol']}: skipped ({e})")
                continue
            s = position_story(trades, c["mint"], c.get("pair_created"), c["price_sol"],
                               held[w])
            stories.append({"wallet": w, "mint": c["mint"], "symbol": c["symbol"],
                            "url": c.get("url"), **s, "conviction": is_conviction(
                                s, min_multiple=min_multiple)})
        n = sum(1 for s in stories if s["mint"] == c["mint"] and s["conviction"])
        log(f"  {c['symbol'] or c['mint'][:6]:<12} {len(wallets):>2} wallet holders, "
            f"{n} built it with conviction   [~{hel.credits:,} credits]")

    conv = [s for s in stories if s["conviction"]]
    by_w = defaultdict(list)
    for s in conv:
        by_w[s["wallet"]].append(s)
    # Drop exchanges/bots: they show up as big holders everywhere.
    since = int(time.time() - 30 * 86400)
    busy = set()
    for w in by_w:
        try:
            n = sum(1 for _ in hel.transactions(w, order="desc", full=False, limit=1000,
                                                 max_txs=max_tx_30d + 1,
                                                 filters={"blockTime": {"gte": since}}))
        except HeliusError:
            continue
        if n > max_tx_30d:
            busy.add(w)
    ranked = sorted(((w, ss) for w, ss in by_w.items() if w not in busy),
                    key=lambda x: (-len(x[1]), -sum(s["value_sol"] or 0 for s in x[1])))

    log(f"\n{len(conv)} conviction positions across {len(by_w)} wallets "
        f"({len(busy)} busy wallets/exchanges dropped).")
    log("Conviction = bought (not given) in 2+ buys over 2h+, not at launch, now "
        f"{min_multiple:g}x+ its average cost, sold at most half.\n")
    for w, ss in ranked[:25]:
        log(f"{w}   coins: {len(ss)}")
        for s in sorted(ss, key=lambda s: -(s["value_sol"] or 0)):
            log(f"    {s['symbol'] or s['mint'][:6]:<10} {s['buys']:>2} buys over "
                f"{s['span_h']:5.1f}h  {s['sol_in']:6.1f} SOL in  now {s['multiple']:5.1f}x  "
                f"worth {s['value_sol'] or 0:7.1f} SOL  sold {s['sold_share'] * 100:3.0f}%")
    multi = [w for w, ss in ranked if len(ss) >= 2]
    log(f"\nWallets with conviction in 2+ coins: {len(multi)}")
    if multi:
        log("To watch them, add to the WATCH_WALLETS secret:\n  " + ",".join(multi[:10]))

    with open(csv_path, "w", newline="") as f:
        cols = ["wallet", "symbol", "mint", "conviction", "buys", "sells", "span_h", "sol_in",
                "avg_price_sol", "multiple", "value_sol", "bought_share", "sold_share",
                "launch_buyer", "first_buy", "url"]
        wr = csv.DictWriter(f, fieldnames=cols + ["busy"], extrasaction="ignore")
        wr.writeheader()
        for s in sorted(stories, key=lambda s: (s["wallet"], s["symbol"] or "")):
            wr.writerow({**s, "busy": s["wallet"] in busy})
    log(f"\nAll {len(stories)} holder positions saved to {csv_path}. "
        f"Est. Helius credits: {hel.credits:,}")
