"""Step 2: drop bots and sybil clusters, then start tracking the best wallets."""
from collections import defaultdict

from . import config, db
from .helius import Helius
from .parse import owner_deltas, account_keys


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


def run(log=print):
    con = db.connect()
    hel = Helius()
    cands = con.execute("""SELECT * FROM wallets WHERE source='discovered'
                           ORDER BY hits DESC, avg_multiple DESC""").fetchall()
    if not cands:
        log("No candidates yet. Run: python main.py discover")
        return
    log(f"Vetting {len(cands)} candidates...")
    funders = {}
    for c in cands:
        w = c["address"]
        sigs = list(hel.transactions(w, order="desc", full=False, limit=1000, max_txs=1000))
        rate = tx_rate(sigs)
        first = list(hel.transactions(w, order="asc", full=True, limit=20, max_txs=20))
        funders[w] = find_funder(first, w)
        db.upsert_wallet(con, w, tx_per_day=rate, funder=funders[w],
                         is_bot=int(rate > config.MAX_TX_PER_DAY))

    clusters = assign_clusters(funders, config.MAX_CLUSTER_SIZE)
    for w in funders:
        db.upsert_wallet(con, w, cluster=clusters.get(w))

    # Pick: not a bot, one wallet per cluster (the best one), top N.
    chosen, used_clusters = [], set()
    for c in con.execute("""SELECT * FROM wallets WHERE source='discovered' AND is_bot=0
                            ORDER BY hits DESC, avg_multiple DESC""").fetchall():
        if c["cluster"]:
            if c["cluster"] in used_clusters:
                continue
            used_clusters.add(c["cluster"])
        chosen.append(c["address"])
        if len(chosen) >= config.AUTO_TRACK_TOP:
            break
    for w in chosen:
        db.upsert_wallet(con, w, tracked=1)

    bots = sum(1 for c in cands if con.execute(
        "SELECT is_bot FROM wallets WHERE address=?", (c["address"],)).fetchone()[0])
    log(f"  bots dropped: {bots}")
    log(f"  wallets in shared-funder clusters: {len(clusters)} "
        f"(kept one per cluster)")
    log(f"  now tracking: {len(chosen)} wallets")
    log(f"Estimated Helius credits used: {hel.credits:,}")
    log("Next: python main.py serve   (or: python main.py track --once)")
