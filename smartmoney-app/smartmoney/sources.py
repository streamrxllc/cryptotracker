"""Funding-source watch.

The trace found a few accounts that funded several wallets which then made
big money on winners (e.g. FWznbc… behind STONK, Agency, SI, CLAUDIA). Here we
watch those sources for SOL going out to other wallets:

  * every wallet a source funds (>= SOURCE_MIN_SOL, not a swap, not an
    exchange) is alerted and tracked for SOURCE_FOLLOW_DAYS;
  * wallets funded directly by a source are watched for funding too, up to
    SOURCE_HOPS hops (the STONK whale sat 2 hops below FWznbc…);
  * the first real buy a funded wallet makes is alerted ("🎯").

The first scan of each source looks back SOURCE_LOOKBACK_DAYS; only funding from
the last 2 hours gets its own alert, older finds are summed up in one message.
"""
import time

from . import config
from .helius import HeliusError
from .parse import extract_trades, signature
from .trace import HUB_DAYS, HUB_TXS, sol_recipients

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_wallets (
    address TEXT PRIMARY KEY, root TEXT, parent TEXT, depth INTEGER,
    funded_sol REAL, funded_ts INTEGER, sig TEXT,
    skip INTEGER DEFAULT 0,        -- exchange/hub: never tracked
    last_sig TEXT, last_slot INTEGER, added_at INTEGER
);
CREATE TABLE IF NOT EXISTS source_buys (
    wallet TEXT, mint TEXT, ts INTEGER, sol REAL, alerted INTEGER,
    PRIMARY KEY (wallet, mint)
);
"""


def short(a):
    return f"{a[:4]}…{a[-4:]}" if a else "?"


def transfers_out(tx, source, min_sol):
    """SOL `source` sent to other wallets in this tx. Swaps are ignored (the
    pool's SOL vault would look like a recipient), as are token accounts. Pure."""
    if extract_trades(tx, source):
        return []
    meta = tx.get("meta") or {}
    tok_idx = {b["accountIndex"] for b in
               (meta.get("preTokenBalances") or []) + (meta.get("postTokenBalances") or [])}
    from .parse import account_keys
    keys = account_keys(tx)
    tok_accts = {keys[i] for i in tok_idx if i < len(keys)}
    return [(to, amt) for to, amt in sol_recipients(tx, source, min_sol) if to not in tok_accts]


def active(con, now=None):
    """Funded wallets currently being followed (tracked for trades)."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    return [r["address"] for r in con.execute(
        "SELECT address FROM source_wallets WHERE depth>=1 AND skip=0 AND funded_ts>=?",
        (now - config.SOURCE_FOLLOW_DAYS * 86400,))]


def _is_hub(hel, a, now, cache):
    if a not in cache:
        try:
            n = sum(1 for _ in hel.transactions(a, order="desc", full=False, limit=1000,
                                                 max_txs=HUB_TXS,
                                                 filters={"blockTime": {"gte": now - HUB_DAYS * 86400}}))
        except HeliusError:
            n = 0
        cache[a] = n >= HUB_TXS
    return cache[a]


def check_funding(hel, con, notify, log=print, now=None, fresh_hours=2):
    """Scan watched sources for new outgoing SOL. Returns newly found wallets."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    for s in config.WATCH_SOURCES:
        con.execute("INSERT OR IGNORE INTO source_wallets (address, root, depth, added_at) "
                    "VALUES (?,?,0,?)", (s, s, now))
    con.commit()
    scan = con.execute(
        """SELECT * FROM source_wallets WHERE skip=0 AND depth<?
           AND (depth=0 OR funded_ts>=?)""",
        (config.SOURCE_HOPS, now - config.SOURCE_FOLLOW_DAYS * 86400)).fetchall()
    hubs, found = {}, []
    for src in scan:
        a = src["address"]
        try:
            latest = hel.latest_signature(a)
            if latest is None or latest == src["last_sig"]:
                continue
            first_scan = src["last_slot"] is None
            start = (now - config.SOURCE_LOOKBACK_DAYS * 86400 if src["depth"] == 0
                     else (src["funded_ts"] or now) - 3600)
            filters = ({"slot": {"gt": src["last_slot"]}} if not first_scan else
                       {"blockTime": {"gte": int(start)}})
            filters["status"] = "succeeded"
            max_slot, kids = src["last_slot"] or 0, []
            for tx in hel.transactions(a, order="asc", full=True, limit=100,
                                       filters=filters, max_txs=1000):
                max_slot = max(max_slot, int(tx.get("slot") or 0))
                for to, amt in transfers_out(tx, a, config.SOURCE_MIN_SOL):
                    if to in config.WATCH_SOURCES or to == a:
                        continue
                    if con.execute("SELECT 1 FROM source_wallets WHERE address=?", (to,)).fetchone():
                        continue
                    skip = 1 if _is_hub(hel, to, now, hubs) else 0
                    con.execute(
                        """INSERT OR IGNORE INTO source_wallets
                           (address, root, parent, depth, funded_sol, funded_ts, sig, skip, added_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (to, src["root"], a, src["depth"] + 1, amt,
                         int(tx.get("blockTime") or now), signature(tx), skip, now))
                    if not skip:
                        kids.append((to, amt, int(tx.get("blockTime") or now), src["depth"] + 1))
            con.execute("UPDATE source_wallets SET last_sig=?, last_slot=? WHERE address=?",
                        (latest, max_slot or None, a))
            con.commit()
        except HeliusError as e:
            log(f"   source {short(a)} error: {e}")
            continue
        root = src["root"]
        recent = [k for k in kids if k[2] >= now - fresh_hours * 3600]
        older = [k for k in kids if k[2] < now - fresh_hours * 3600]
        if first_scan and src["depth"] == 0:
            notify(f"👀 Now watching funding source {short(a)}: {len(older)} wallet(s) funded "
                   f"in the last {config.SOURCE_LOOKBACK_DAYS:g} days are now tracked.\n"
                   f"https://solscan.io/account/{a}", log)
            log(f"   source {short(a)}: first scan, {len(kids)} funded wallet(s) recorded")
        for to, amt, ts, depth in recent:
            via = f" (via {short(a)})" if depth > 1 else ""
            notify(f"💸 {short(root)}{via} just funded {short(to)} with {amt:.1f} SOL. "
                   f"Now tracking it; you'll get a 🎯 when it buys.\n"
                   f"https://solscan.io/account/{to}", log)
            log(f"   FUNDED {short(root)}{via} -> {to} {amt:.1f} SOL")
        found += kids
    return found


def check_buys(con, notify, log=print, now=None, fresh_hours=2):
    """Alert the first real buy of each token by a funded wallet. Buys older
    than fresh_hours (history loaded on first poll) are recorded silently."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    kids = {r["address"]: r for r in con.execute(
        "SELECT * FROM source_wallets WHERE depth>=1 AND skip=0 AND funded_ts>=?",
        (now - config.SOURCE_FOLLOW_DAYS * 86400,))}
    if not kids:
        return []
    q = ",".join("?" * len(kids))
    rows = con.execute(
        f"""SELECT wallet, mint, MIN(ts) ts, SUM(ABS(quote_sol)) sol FROM trades
            WHERE side='buy' AND wallet IN ({q}) AND ABS(quote_sol)>=?
            GROUP BY wallet, mint""", list(kids) + [config.SOURCE_MIN_BUY_SOL]).fetchall()
    skip = config.NON_MEME_MINTS | config.QUOTE_MINTS
    seen = {(r["wallet"], r["mint"]) for r in con.execute("SELECT wallet, mint FROM source_buys")}
    from . import dexscreener
    new = [r for r in rows if (r["wallet"], r["mint"]) not in seen and r["mint"] not in skip]
    fresh = [r for r in new if r["ts"] >= now - fresh_hours * 3600]
    fresh_keys = {(r["wallet"], r["mint"]) for r in fresh}
    pairs = dexscreener.best_pairs(list({r["mint"] for r in fresh})) if fresh else {}
    msgs = []
    for r in new:
        alert = (r["wallet"], r["mint"]) in fresh_keys
        if alert:
            k = kids[r["wallet"]]
            tok = dexscreener.summarize(pairs.get(r["mint"]))
            mcap = tok.get("mcap")
            text = (f"🎯 {short(r['wallet'])} (funded by {short(k['root'])} "
                    f"{(now - k['funded_ts']) / 3600:.0f}h ago) bought "
                    f"{tok.get('symbol') or r['mint'][:8]}: {r['sol']:.1f} SOL\n"
                    f"MCap {'$%s' % format(int(mcap), ',') if mcap else '?'} · "
                    f"liquidity {'$%s' % format(int(tok['liquidity']), ',') if tok.get('liquidity') else '?'}\n"
                    f"{tok.get('url') or 'https://dexscreener.com/solana/' + r['mint']}")
            notify(text, log)
            log("   " + text.splitlines()[0])
            msgs.append(text)
        con.execute("INSERT OR IGNORE INTO source_buys VALUES (?,?,?,?,?)",
                    (r["wallet"], r["mint"], r["ts"], r["sol"], 1 if alert else 0))
    con.commit()
    return msgs


def overview(con, now=None):
    """Rows for the dashboard: each source and the wallets it funded."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    rows = [dict(r) for r in con.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM source_buys b WHERE b.wallet=s.address) buys,
                  (SELECT mint FROM source_buys b WHERE b.wallet=s.address ORDER BY ts DESC LIMIT 1) last_mint
           FROM source_wallets s WHERE s.skip=0 ORDER BY s.root, s.funded_ts DESC""")]
    for r in rows:
        r["following"] = bool(r["depth"] and r["funded_ts"] and
                              r["funded_ts"] >= now - config.SOURCE_FOLLOW_DAYS * 86400)
    return rows


def lookback(days=21, hops=2, log=print, csv_path="sources.csv"):
    """One-off report: what the watched sources (and wallets they funded) did
    in the last `days`: who they funded, and what everyone bought. Read-only."""
    import csv
    from collections import defaultdict
    from . import dexscreener
    from .helius import Helius
    hel, now, hubs = Helius(), int(time.time()), {}
    since = now - int(days * 86400)
    nodes = [(s, s, None, 0, None, None) for s in config.WATCH_SOURCES]  # addr, root, parent, depth, sol, ts
    seen, rows, buys = set(config.WATCH_SOURCES), [], defaultdict(lambda: defaultdict(float))
    # A dormant source: look at the `days` before its last activity instead.
    root_since, last_seen = {}, {}
    for src in config.WATCH_SOURCES:
        try:
            res = hel.rpc("getSignaturesForAddress", [src, {"limit": 1}])
        except HeliusError:
            res = []
        last = int(res[0].get("blockTime") or 0) if res else 0
        last_seen[src] = last
        root_since[src] = since if last >= since or not last else last - int(days * 86400)
        log(f"{short(src)} last active: "
            f"{time.strftime('%Y-%m-%d', time.gmtime(last)) if last else 'never'}"
            f"{'' if last >= since or not last else f' (dormant; using the {days:g} days before that)'}")
    i = 0
    while i < len(nodes):
        a, root, parent, depth, sol, ts = nodes[i]
        i += 1
        rs = root_since[root]
        start = rs if depth == 0 else max(rs, (ts or rs) - 3600)
        log(f"{'  ' * depth}{short(a)} ({'source' if not depth else f'{sol:.1f} SOL from {short(parent)}'})"
            f" … [~{hel.credits:,} credits]")
        try:
            for tx in hel.transactions(a, order="asc", full=True, limit=100, max_txs=1500,
                                       filters={"blockTime": {"gte": int(start)}, "status": "succeeded"}):
                for t in extract_trades(tx, a):
                    if t.side == "buy" and t.mint not in config.NON_MEME_MINTS | config.QUOTE_MINTS:
                        buys[a][t.mint] += abs(t.quote_sol or 0)
                if depth >= hops:
                    continue
                for to, amt in transfers_out(tx, a, config.SOURCE_MIN_SOL):
                    if to in seen:
                        continue
                    seen.add(to)
                    if _is_hub(hel, to, now, hubs):
                        rows.append((root, a, to, depth + 1, amt, tx.get("blockTime"), "exchange/hub"))
                        continue
                    nodes.append((to, root, a, depth + 1, amt, tx.get("blockTime")))
        except HeliusError as e:
            log(f"   error on {short(a)}: {e}")
    mints = list({m for b in buys.values() for m in b})
    pairs = dexscreener.best_pairs(mints) if mints else {}
    info = {m: dexscreener.summarize(pairs.get(m)) for m in mints}
    def name(m):
        return info[m].get("symbol") or m[:6]

    log(f"\n=== Last {days:g} days of activity ===")
    for src in config.WATCH_SOURCES:
        log(f"\nSOURCE {src}  (window from "
            f"{time.strftime('%Y-%m-%d', time.gmtime(root_since[src]))}, last active "
            f"{time.strftime('%Y-%m-%d', time.gmtime(last_seen[src])) if last_seen[src] else 'never'})")
        own = sorted(buys[src].items(), key=lambda kv: -kv[1])
        log("  bought itself: " + (", ".join(f"{name(m)} {s:.1f} SOL" for m, s in own[:10]) or "nothing"))
        kids = [n for n in nodes if n[1] == src and n[3] >= 1]
        log(f"  funded {len(kids)} wallet(s):")
        for a, root, parent, depth, sol, ts in sorted(kids, key=lambda n: -(n[5] or 0)):
            b = sorted(buys[a].items(), key=lambda kv: -kv[1])
            via = f" via {short(parent)}" if depth > 1 else ""
            log(f"    {a}  {sol:.1f} SOL{via} on {time.strftime('%b %d', time.gmtime(ts or 0))}"
                f" → {', '.join(f'{name(m)} {s:.1f}' for m, s in b[:6]) or 'no buys yet'}")
            rows.append((root, parent, a, depth, sol, ts, "; ".join(f"{name(m)}:{s:.2f}" for m, s in b)))
    # Tokens more than one wallet in the network bought = the network's current bets.
    by_mint = defaultdict(list)
    for a, b in buys.items():
        for m, s in b.items():
            by_mint[m].append((a, s))
    shared = sorted(((m, v) for m, v in by_mint.items() if len(v) >= 2), key=lambda kv: -len(kv[1]))
    if shared:
        log("\nTokens bought by 2+ wallets in these networks:")
        for m, v in shared[:15]:
            t = info[m]
            mc = f"${t['mcap']:,.0f}" if t.get("mcap") else "?"
            log(f"  {name(m):<10} {len(v)} wallets, {sum(s for _, s in v):.1f} SOL · MCap {mc} "
                f"· 24h {t.get('change_h24')}% · {t.get('url') or m}")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["source", "funded_by", "wallet", "hops", "sol_received", "date", "bought (token:SOL)"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], r[3], round(r[4] or 0, 3),
                        time.strftime("%Y-%m-%d %H:%M", time.gmtime(r[5] or 0)), r[6]])
    log(f"\nSaved {csv_path}. Est. Helius credits: {hel.credits:,}")


# Top of the two funding chains the trace found (each was 3 hops above a winner).
FOLLOW_ROOTS = [
    "EVtyyoCXZ79fc9C7u17bF6skmgfqXueNMNL6HrtWPMe",   # -> CpjcPX… -> FWznbc… (STONK/Agency/SI/CLAUDIA)
    "8STVubP1QgsC2BLquzzg4XB1umY9hFjhkjEkjuo9dhpa",   # -> EHqfgz… -> FBwXQA… (ANSEM/BORDR)
]


def follow(roots=None, hops=5, max_nodes=300, min_sol=0.5, active_days=7, log=print,
           csv_path="follow.csv"):
    """Follow the money forward from `roots`: every wallet they sent SOL to,
    every wallet those sent SOL to, ... (all history, newest first per wallet).
    Stops at exchanges/hubs, `hops` deep, or `max_nodes` wallets. Shows which
    wallets in the tree are active now and what they're buying."""
    import csv
    from collections import defaultdict
    from . import dexscreener
    from .helius import Helius
    hel, now, hubs = Helius(), int(time.time()), {}
    act_since = now - int(active_days * 86400)
    roots = roots or FOLLOW_ROOTS
    # node: dict(addr, root, parent, hop, sol, funded_ts)
    nodes = [dict(addr=r, root=r, parent=None, hop=0, sol=None, funded_ts=None) for r in roots]
    seen = set(roots)
    info = {}          # addr -> last_ts, txs, kids, buys_all{mint:sol}, buys_recent{mint:sol}
    exch = defaultdict(float)   # where money left the tree into exchanges/hubs
    i = 0
    log(f"Following the money from {len(roots)} root(s), up to {hops} hops / {max_nodes} wallets "
        f"(~100 credits per wallet)...")
    while i < len(nodes):
        n = nodes[i]
        i += 1
        a = n["addr"]
        d = info[a] = dict(last_ts=0, txs=0, kids=0, buys_all=defaultdict(float),
                           buys_recent=defaultdict(float))
        try:
            for tx in hel.transactions(a, order="desc", full=True, limit=100, max_txs=1000,
                                       filters={"status": "succeeded"}):
                ts = int(tx.get("blockTime") or 0)
                d["txs"] += 1
                d["last_ts"] = max(d["last_ts"], ts)
                for t in extract_trades(tx, a):
                    if t.side == "buy" and t.mint not in config.NON_MEME_MINTS | config.QUOTE_MINTS:
                        d["buys_all"][t.mint] += abs(t.quote_sol or 0)
                        if ts >= act_since:
                            d["buys_recent"][t.mint] += abs(t.quote_sol or 0)
                if n["hop"] >= hops:
                    continue
                for to, amt in transfers_out(tx, a, min_sol):
                    if to in seen:
                        continue
                    seen.add(to)
                    if _is_hub(hel, to, now, hubs):
                        exch[to] += amt
                        continue
                    d["kids"] += 1
                    if len(nodes) < max_nodes:
                        nodes.append(dict(addr=to, root=n["root"], parent=a, hop=n["hop"] + 1,
                                          sol=amt, funded_ts=ts))
        except HeliusError as e:
            log(f"   error on {short(a)}: {e}")
        act = d["last_ts"] >= act_since
        got = f" · got {n['sol']:.1f} SOL" if n["hop"] else ""
        last = time.strftime("%b %d", time.gmtime(d["last_ts"])) if d["last_ts"] else "never"
        log(f"  {'  ' * n['hop']}{short(a)} hop {n['hop']}{got} · last active {last}"
            f"{' 🟢' if act else ''} · funded {d['kids']}  [{len(nodes)} wallets, ~{hel.credits:,} credits]")
    if len(nodes) >= max_nodes:
        log(f"  (stopped at {max_nodes} wallets; raise --max-nodes to go further)")

    mints = list({m for d in info.values() for m in d["buys_all"]})
    pairs = dexscreener.best_pairs(mints) if mints else {}
    tok = {m: dexscreener.summarize(pairs.get(m)) for m in mints}

    def name(m):
        return tok[m].get("symbol") or m[:6]

    def top(b, k=5):
        return ", ".join(f"{name(m)} {s:.1f}" for m, s in sorted(b.items(), key=lambda kv: -kv[1])[:k])

    by_root = defaultdict(list)
    for n in nodes:
        by_root[n["root"]].append(n)
    log(f"\n=== Funding trees ===")
    for r in roots:
        tree = by_root[r]
        active = [n for n in tree if info[n["addr"]]["last_ts"] >= act_since]
        log(f"\nROOT {r}: {len(tree) - 1} wallets downstream, {len(active)} active in the last "
            f"{active_days:g} days")
        kids = defaultdict(list)
        for n in tree:
            kids[n["parent"]].append(n)

        def show(n, depth):
            d = info[n["addr"]]
            act = d["last_ts"] >= act_since
            # Print funders and active wallets; dormant dead-ends are only counted.
            if not (kids[n["addr"]] or act or d["buys_all"]) and depth > 1:
                return
            edge = (f" ← {n['sol']:.1f} SOL {time.strftime('%b %d', time.gmtime(n['funded_ts']))}"
                    if n["hop"] else "")
            log(f"  {'   ' * depth}{n['addr'] if act else short(n['addr'])}"
                f"{edge}"
                f" · last {time.strftime('%b %d', time.gmtime(d['last_ts'])) if d['last_ts'] else 'never'}"
                f"{' 🟢 ACTIVE' if act else ''}"
                f"{' · funded ' + str(d['kids']) if d['kids'] else ''}"
                f"{' · bought ' + top(d['buys_all'], 3) if d['buys_all'] else ''}")
            for c in sorted(kids[n["addr"]], key=lambda c: -(c["funded_ts"] or 0)):
                show(c, depth + 1)
        show(tree[0], 0)

    act_nodes = [n for n in nodes if info[n["addr"]]["last_ts"] >= act_since]
    log(f"\n=== Active now (last {active_days:g} days): the next ones ===")
    for n in sorted(act_nodes, key=lambda n: -info[n["addr"]]["last_ts"]):
        d = info[n["addr"]]
        log(f"  {n['addr']}  hop {n['hop']} under {short(n['root'])} · last "
            f"{time.strftime('%b %d %H:%M', time.gmtime(d['last_ts']))} UTC"
            f"{' · funded ' + str(d['kids']) if d['kids'] else ''}"
            f" · buying: {top(d['buys_recent']) or 'nothing'}")
    shared = defaultdict(list)
    for a, d in info.items():
        for m, s in d["buys_recent"].items():
            shared[m].append(s)
    hot = sorted(((m, v) for m, v in shared.items()), key=lambda kv: (-len(kv[1]), -sum(kv[1])))
    if hot:
        log(f"\nTokens the network bought in the last {active_days:g} days:")
        for m, v in hot[:15]:
            t = tok[m]
            log(f"  {name(m):<10} {len(v)} wallet(s), {sum(v):.1f} SOL · MCap "
                f"{'$%s' % format(int(t['mcap']), ',') if t.get('mcap') else '?'} · 24h "
                f"{t.get('change_h24')}% · {t.get('url') or m}")
    if exch:
        log(f"\nMoney that left the tree into {len(exch)} exchange/hub account(s): "
            f"{sum(exch.values()):.0f} SOL (trail goes cold there).")
    funders = [n["addr"] for n in act_nodes if info[n["addr"]]["kids"]]
    traders = [n["addr"] for n in act_nodes if not info[n["addr"]]["kids"]]
    if act_nodes:
        log("\nTo watch these live, set these Replit Secrets and redeploy:")
        if funders:
            log(f"  WATCH_SOURCES = {','.join(funders)}")
        if traders:
            log(f"  add to WATCH_WALLETS: {','.join(traders)}")
    with open(csv_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["root", "hop", "wallet", "funded_by", "sol_received", "funded_date",
                    "last_active", "active", "txs_scanned", "wallets_funded",
                    f"buys_last_{active_days:g}d", "buys_all"])
        for n in nodes:
            d = info[n["addr"]]
            fmt = lambda ts: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else ""
            w.writerow([n["root"], n["hop"], n["addr"], n["parent"] or "",
                        round(n["sol"], 3) if n["sol"] else "", fmt(n["funded_ts"]),
                        fmt(d["last_ts"]), int(d["last_ts"] >= act_since), d["txs"], d["kids"],
                        top(d["buys_recent"], 10), top(d["buys_all"], 10)])
    log(f"\nSaved {csv_path}. Est. Helius credits: {hel.credits:,}")
    return nodes, info
