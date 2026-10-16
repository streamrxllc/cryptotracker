"""Step 4: consensus — which tokens are several tracked wallets accumulating?"""
from collections import defaultdict

from . import config


def compute(trades, weights, sol_usd=None, cfg=config):
    """Aggregate trades (dicts or rows) into per-token signals. Pure function.

    weights: {wallet: weight}; a wallet's weight is its discovery hits (min 1).
    Returns a list of signal dicts, strongest first.
    """
    per = defaultdict(lambda: defaultdict(lambda: {"in": 0.0, "out": 0.0, "buys": 0,
                                                   "first": None, "last": None}))
    for t in trades:
        if t["side"] not in ("buy", "sell"):
            continue
        sol = abs(t["quote_sol"] or 0.0)
        if t["quote_usd"] and sol_usd:
            sol += abs(t["quote_usd"]) / sol_usd
        w = per[t["mint"]][t["wallet"]]
        if t["side"] == "buy":
            if sol < cfg.MIN_BUY_SOL:
                continue
            w["in"] += sol
            w["buys"] += 1
            w["first"] = min(w["first"] or t["ts"], t["ts"])
            w["last"] = max(w["last"] or 0, t["ts"])
        else:
            w["out"] += sol

    out = []
    for mint, wallets in per.items():
        buyers = {a: s for a, s in wallets.items() if s["in"] - s["out"] > 0 and s["buys"]}
        sellers = [a for a, s in wallets.items() if s["out"] > s["in"]]
        if len(buyers) < cfg.MIN_WALLETS_FOR_SIGNAL:
            continue
        out.append({
            "mint": mint,
            "wallets": len(buyers),
            "sellers": len(sellers),
            "accumulators": sum(1 for s in buyers.values() if s["buys"] >= 2),
            "net_sol": round(sum(s["in"] - s["out"] for s in buyers.values()), 3),
            "buys": sum(s["buys"] for s in buyers.values()),
            "score": round(sum(max(1, weights.get(a, 1)) for a in buyers)
                           - 0.5 * sum(max(1, weights.get(a, 1)) for a in sellers), 2),
            "first_buy": min(s["first"] for s in buyers.values()),
            "last_buy": max(s["last"] for s in buyers.values()),
            "buyer_list": sorted(buyers, key=lambda a: -buyers[a]["in"]),
        })
    out.sort(key=lambda s: (s["score"], s["net_sol"]), reverse=True)
    return out
