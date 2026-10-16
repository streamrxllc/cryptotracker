"""Step 2: drop bots and sybil clusters, then start tracking the best wallets."""
import statistics
from collections import Counter, defaultdict

from . import config, db
from .helius import Helius, HeliusError
from .parse import account_keys, extract_trades, owner_deltas


def tx_rate(sig_rows):
    """Transactions per day from a list of signature rows (newest first)."""
    times = [r["blockTime"] for r in sig_rows if r.get("blockTime")]
    if len(times) < 2:
        return 0.0
    span_days = max((max(times) - min(times)) / 86400, 1 / 24)
    return len(times) / span_days


def find_funder(txs, wallet):
    """The account that sent this wallet its first SOL (oldest-first txs)."""
    for tx in txs:
        if (tx.get("meta") or {}).get("err"):
            continue
        sol, _ = owner_deltas(tx, wallet)
        if sol <= 0:
            continue
        meta, keys = tx["meta"], account_keys(tx)
        drops = [(post - pre, keys[i]) for i, (pre, post)
                 in enumerate(zip(meta["preBalances"], meta["postBalances"]))
                 if i < len(keys) and keys[i] != wallet]
        if drops:
            delta, who = min(drops)
            if delta < 0:
                return who
    return None


def assign_clusters(funders, max_size):
    """{wallet: cluster_id} for wallets sharing a funder (2..max_size wallets)."""
    groups = defaultdict(list)
    for w, f in funders.items():
        if f:
            groups[f].append(w)
    out = {}
    for f, ws in groups.items():
        if 2 <= len(ws) <= max_size:
            for w in ws:
                out[w] = f
    return out


def trading_profile(txs, wallet):
    """Behaviour from a wallet's recent transactions (any order). Pure function.

    Returns tx_per_day, new_tokens_per_day (distinct tokens bought per day),
    and median_hold_min (first buy -> first sell, over tokens it sold).
    """
    times, first_buy, first_sell = [], {}, {}
    for tx in txs:
        ts = int(tx.get("blockTime") or 0)
        if ts:
            times.append(ts)
        for t in extract_trades(tx, wallet):
            if t.side == "buy":
                first_buy[t.mint] = min(first_buy.get(t.mint, ts), ts)
            elif t.side == "sell":
                first_sell[t.mint] = min(first_sell.get(t.mint, ts), ts)
    if len(times) < 2:
        return {"tx_per_day": 0.0, "new_tokens_per_day": 0.0, "median_hold_min": None}
    span_days = max((max(times) - min(times)) / 86400, 1 / 24)
    holds = [(first_sell[m] - b) / 60 for m, b in first_buy.items()
             if m in first_sell and first_sell[m] >= b]
    return {
        "tx_per_day": len(times) / span_days,
        "new_tokens_per_day": len(first_buy) / span_days,
        "median_hold_min": statistics.median(holds) if holds else None,
    }


def flag_for(profile, cfg=config):
    """Why a wallet can't be copied, or None if it looks copyable."""
    if profile["tx_per_day"] > cfg.MAX_TX_PER_DAY:
        return f"bot: {profile['tx_per_day']:.0f} tx/day"
    if profile["new_tokens_per_day"] > cfg.MAX_NEW_TOKENS_PER_DAY:
        return f"spray: buys {profile['new_tokens_per_day']:.0f} new tokens/day"
    hold = profile["median_hold_min"]
    if hold is not None and hold < cfg.MIN_MEDIAN_HOLD_MIN:
        return f"flipper: median hold {hold:.1f} min"
    return None


def run(log=print):
    con = db.connect()
    hel = Helius()
    cands = con.execute("""SELECT * FROM wallets WHERE source='discovered' AND hits >= ?
                           ORDER BY hits DESC, avg_multiple DESC LIMIT ?""",
                        (config.MIN_HITS, config.VET_TOP)).fetchall()
    if not cands:
        log("No candidates yet. Run: python main.py discover")
        return
    total = con.execute("SELECT COUNT(*) FROM wallets WHERE source='discovered'").fetchone()[0]
    log(f"Vetting the top {len(cands)} of {total} candidates "
        f"({config.MIN_HITS}+ hits; ~{len(cands) * 60:,} credits)...")
    con.execute("UPDATE wallets SET tracked=0 WHERE source='discovered'")
    con.commit()

    funders, flags = {}, {}
    for i, c in enumerate(cands, 1):
        w = c["address"]
        try:
            recent = list(hel.transactions(w, order="desc", full=True, limit=100,
                                           filters={"status": "succeeded"},
                                           max_txs=config.VET_RECENT_TXS))
            prof = trading_profile(recent, w)
            first = list(hel.transactions(w, order="asc", full=True, limit=20, max_txs=20))
            funders[w] = find_funder(first, w)
        except HeliusError as e:
            log(f"  {w[:8]}… skipped: {e}")
            continue
        flags[w] = flag_for(prof)
        db.upsert_wallet(con, w, tx_per_day=prof["tx_per_day"],
                         new_tokens_per_day=prof["new_tokens_per_day"],
                         median_hold_min=prof["median_hold_min"], funder=funders[w],
                         flag=flags[w], is_bot=int(flags[w] is not None))
        if i % 25 == 0:
            ok = sum(1 for f in flags.values() if f is None)
            log(f"  {i}/{len(cands)} vetted, {ok} look copyable  [~{hel.credits:,} credits]")

    clusters = assign_clusters(funders, config.MAX_CLUSTER_SIZE)
    for w in funders:
        db.upsert_wallet(con, w, cluster=clusters.get(w))
    log(f"{len(clusters)} wallets are in shared-funder clusters (one per cluster is kept).")
    log(f"Estimated Helius credits used: {hel.credits:,}\n")
    pick(con, log)


def pick(con=None, log=print):
    """Choose which vetted wallets to track, from stored stats. Costs no credits,
    so thresholds in config can be changed and re-applied with `main.py pick`."""
    con = con or db.connect()
    # Only wallets measured by the current vetting (new_tokens_per_day is set).
    rows = con.execute("""SELECT * FROM wallets WHERE source='discovered'
                          AND new_tokens_per_day IS NOT NULL AND hits >= ?
                          ORDER BY hits DESC, avg_multiple DESC""",
                       (config.MIN_HITS,)).fetchall()
    if not rows:
        log("No vetted wallets yet. Run: python main.py vet")
        return []
    con.execute("UPDATE wallets SET tracked=0 WHERE source='discovered'")
    reasons, chosen, used_clusters = Counter(), [], set()
    for c in rows:
        prof = {"tx_per_day": c["tx_per_day"] or 0.0,
                "new_tokens_per_day": c["new_tokens_per_day"] or 0.0,
                "median_hold_min": c["median_hold_min"]}
        flag = flag_for(prof)
        con.execute("UPDATE wallets SET flag=?, is_bot=? WHERE address=?",
                    (flag, int(flag is not None), c["address"]))
        if flag:
            reasons[flag.split(":")[0]] += 1
            continue
        if c["cluster"]:
            if c["cluster"] in used_clusters:
                reasons["cluster duplicate"] += 1
                continue
            used_clusters.add(c["cluster"])
        if len(chosen) < config.AUTO_TRACK_TOP:
            chosen.append(c)
    for c in chosen:
        con.execute("UPDATE wallets SET tracked=1 WHERE address=?", (c["address"],))
    con.commit()

    log(f"Of {len(rows)} vetted wallets, dropped: " +
        ", ".join(f"{n} {r}" for r, n in reasons.most_common()) + ".")
    log(f"Rules: <= {config.MAX_TX_PER_DAY:g} tx/day, <= {config.MAX_NEW_TOKENS_PER_DAY:g} "
        f"new tokens/day, median hold >= {config.MIN_MEDIAN_HOLD_MIN:g} min.")
    log(f"Now tracking {len(chosen)} wallets:")
    for c in chosen:
        hold = "no sells seen" if c["median_hold_min"] is None \
            else f"median hold {c['median_hold_min']:.0f}m"
        log(f"  {c['address']}  hits={c['hits']}  "
            f"{c['new_tokens_per_day'] or 0:.1f} tokens/day  {hold}")
    log("\nNext: start the 'Solana Wallet Tracker' workflow (or: python main.py track --once)")
    return chosen
