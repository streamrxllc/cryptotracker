"""Follow the money: who profited on the winners, who funded them, and do a
few funding sources sit behind many of the profitable wallets?

1. Candidates: for each winner, the biggest early buyers found by `discover`
   (discovery_hits), plus any extra wallets you pass.
2. Profit: each candidate's own trades in that coin (one-token history):
   SOL received + value still held - SOL spent. Keep the clearly profitable.
3. Funding chain: for each profitable wallet, the account that sent it its
   first SOL, then that account's funder, up to DEPTH hops. Climbing stops at
   hubs (exchanges, bridges, bots): accounts with very heavy recent activity.
4. Sources: any non-hub ancestor behind 2+ profitable wallets. Ranked by how
   many different winners its wallets hit and total profit.
5. For the top sources: the other wallets they funded recently. Those are the
   candidates to watch next.
"""
import csv
import time
from collections import defaultdict

from . import config, db, dexscreener
from .helius import Helius, HeliusError
from .holders import token_history
from .parse import account_keys, extract_trades, signers
from .review import positions
from .vet import find_funder

HUB_TXS = 1000          # >= this many transactions in HUB_DAYS = exchange/bot/service
HUB_DAYS = 3


def sol_recipients(tx, sender, min_sol=0.5):
    """Accounts that received >= min_sol SOL from `sender` in this tx. Pure."""
    if sender not in signers(tx) or (tx.get("meta") or {}).get("err"):
        return []
    meta, keys = tx["meta"], account_keys(tx)
    out = []
    for i, (pre, post) in enumerate(zip(meta["preBalances"], meta["postBalances"])):
        if i < len(keys) and keys[i] != sender and (post - pre) / 1e9 >= min_sol:
            out.append((keys[i], (post - pre) / 1e9))
    return out


def group_sources(chains, profits, coins):
    """chains: {wallet: [funder, grand-funder, ...]} (hubs already removed).
    profits: {wallet: SOL}; coins: {wallet: set(mints)}. Pure.
    Returns sources sorted by distinct coins, then profit."""
    under, depth_of = defaultdict(set), {}
    for w, chain in chains.items():
        for depth, a in enumerate(chain, 1):
            under[a].add(w)                       # same ancestor at any depth = one source
            depth_of[a] = min(depth, depth_of.get(a, depth))
    best, seen = {}, set()
    # Several ancestors on one chain cover the same wallets; keep only the nearest.
    for a in sorted(under, key=lambda a: depth_of[a]):
        ws = frozenset(under[a])
        if len(ws) < 2 or ws in seen:
            continue
        seen.add(ws)
        best[a] = {"source": a, "depth": depth_of[a], "wallets": sorted(ws)}
    for s in best.values():
        s["profit_sol"] = sum(profits.get(w, 0) for w in s["wallets"])
        s["coins"] = set().union(*(coins.get(w, set()) for w in s["wallets"]))
    return sorted(best.values(), key=lambda s: (-len(s["coins"]), -s["profit_sol"]))


def run(per_coin=40, depth=3, min_profit_sol=5.0, min_multiple=2.0, top_sources=8,
        extra_wallets=(), log=print, csv_path="trace.csv"):
    con = db.connect()
    hel = Helius()
    rows = con.execute("""SELECT wallet, mint, symbol, sol_in FROM discovery_hits
                          ORDER BY mint, sol_in DESC""").fetchall()
    if not rows and not extra_wallets:
        log("No discovery data. Run: python main.py discover")
        return
    cand = defaultdict(list)
    for r in rows:
        if len(cand[r["mint"]]) < per_coin:
            cand[r["mint"]].append(r["wallet"])
    symbols = {r["mint"]: r["symbol"] for r in rows}
    sol_usd = dexscreener.sol_usd()
    pairs = dexscreener.best_pairs(list(cand))
    prices = {m: dexscreener.price_in_sol(p, sol_usd) for m, p in pairs.items()}
    n = sum(len(v) for v in cand.values())
    log(f"Step 1/3: profit check on the top {per_coin} early buyers of {len(cand)} winners "
        f"({n} wallet-coin pairs, ~{n * 15:,} credits)...")

    profits, coins, detail = defaultdict(float), defaultdict(set), []
    for i, (mint, ws) in enumerate(cand.items(), 1):
        for w in ws:
            try:
                trades = [t.as_dict() for tx in token_history(hel, w, mint, max_txs=300)
                          for t in extract_trades(tx, w) if t.mint == mint]
            except HeliusError:
                continue
            pos = positions(trades, prices)
            if not pos:
                continue
            p = pos[0]
            mult = (p["sol_out"] + p["held_value"]) / p["sol_in"] if p["sol_in"] else 0
            if p["pnl_sol"] >= min_profit_sol and mult >= min_multiple:
                profits[w] += p["pnl_sol"]
                coins[w].add(mint)
                detail.append({"wallet": w, "mint": mint, "symbol": symbols.get(mint),
                               "sol_in": p["sol_in"], "pnl_sol": p["pnl_sol"], "multiple": mult,
                               "status": p["status"]})
        log(f"  {i}/{len(cand)} {symbols.get(mint) or mint[:6]:<10} "
            f"{sum(1 for d in detail if d['mint'] == mint)} profitable  [~{hel.credits:,} credits]")
    profitable = list(profits) + [w for w in extra_wallets if w not in profits]
    log(f"  {len(profits)} wallets made {min_profit_sol:g}+ SOL and {min_multiple:g}x+ "
        f"on at least one winner ({sum(1 for w in profits if len(coins[w]) > 1)} on 2+).")

    log(f"\nStep 2/3: tracing who funded them, up to {depth} hops "
        f"(stopping at exchanges/hubs)...")
    funder_of, hub = {}, {}
    since = int(time.time() - HUB_DAYS * 86400)

    def is_hub(a):
        if a not in hub:
            try:
                k = sum(1 for _ in hel.transactions(a, order="desc", full=False, limit=1000,
                                                     max_txs=HUB_TXS,
                                                     filters={"blockTime": {"gte": since}}))
            except HeliusError:
                k = 0
            hub[a] = k >= HUB_TXS
        return hub[a]

    def funder(a):
        if a not in funder_of:
            try:
                first = list(hel.transactions(a, order="asc", full=True, limit=20, max_txs=20))
                funder_of[a] = find_funder(first, a)
            except HeliusError:
                funder_of[a] = None
        return funder_of[a]

    chains, hubs_hit = {}, defaultdict(int)
    for w in profitable:
        chain, cur = [], w
        for _ in range(depth):
            f = funder(cur)
            if not f or f in chain or f == w:
                break
            if is_hub(f):
                hubs_hit[f] += 1
                break
            chain.append(f)
            cur = f
        chains[w] = chain
    log(f"  traced {len(chains)} wallets; {sum(1 for c in chains.values() if not c)} were "
        f"funded straight from an exchange/hub  [~{hel.credits:,} credits]")

    sources = group_sources(chains, profits, coins)
    log(f"\nStep 3/3: {len(sources)} funding sources sit behind 2+ profitable wallets.")
    out_rows = []
    for s in sources[:top_sources]:
        names = ", ".join(sorted(symbols.get(m) or m[:6] for m in s["coins"]))
        log(f"\nSOURCE {s['source']}  ({s['depth']} hop{'s' if s['depth'] > 1 else ''} up)")
        log(f"  funded {len(s['wallets'])} profitable wallets · {len(s['coins'])} winners "
            f"({names}) · +{s['profit_sol']:.0f} SOL")
        for w in s["wallets"][:8]:
            log(f"    {w}  +{profits.get(w, 0):.1f} SOL on "
                f"{', '.join(symbols.get(m) or m[:6] for m in coins.get(w, []))}")
        # Who else did this source fund lately? Fresh wallets = possible next bets.
        kids = {}
        try:
            for tx in hel.transactions(s["source"], order="desc", full=True, limit=100,
                                       max_txs=300,
                                       filters={"blockTime": {"gte": int(time.time() - 30 * 86400)}}):
                for to, amt in sol_recipients(tx, s["source"]):
                    if to not in s["wallets"]:
                        kids.setdefault(to, (int(tx.get("blockTime") or 0), amt))
        except HeliusError:
            pass
        if kids:
            log(f"  also funded in the last 30 days: {len(kids)} other wallet(s), newest:")
            for to, (ts, amt) in sorted(kids.items(), key=lambda kv: -kv[1][0])[:5]:
                log(f"    {to}  {amt:.1f} SOL on {time.strftime('%b %d', time.gmtime(ts))}")
        out_rows.append({**s, "kids": kids})

    if hubs_hit:
        log(f"\n{sum(hubs_hit.values())} funding trails ended at {len(hubs_hit)} "
            f"exchanges/hubs (the trail goes cold there).")
    with open(csv_path, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["wallet", "coin", "sol_in", "pnl_sol", "multiple", "status",
                     "funder_1", "funder_2", "funder_3"])
        for d in sorted(detail, key=lambda d: -d["pnl_sol"]):
            ch = chains.get(d["wallet"], []) + ["", "", ""]
            wr.writerow([d["wallet"], d["symbol"], round(d["sol_in"], 3), round(d["pnl_sol"], 3),
                         round(d["multiple"], 2), d["status"], ch[0], ch[1], ch[2]])
    log(f"\nAll profitable positions and their funding chains saved to {csv_path}. "
        f"Est. Helius credits: {hel.credits:,}")
    return out_rows
