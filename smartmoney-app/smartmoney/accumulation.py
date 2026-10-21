"""Accumulation alerts for watched wallets.

The review showed the profit came from tokens a wallet bought 5+ times, so we
alert when a watched wallet reaches buy #3 and #5 (and #8) on a token within
ACCUM_WINDOW_HOURS, and again when it first sells a token it was accumulating
(the exit signal). Each alert is sent once.
"""
import time
from collections import defaultdict

from . import config, dexscreener

SCHEMA = """
CREATE TABLE IF NOT EXISTS accum_alerts (
    wallet TEXT, mint TEXT, kind TEXT, sent_at INTEGER, text TEXT,
    PRIMARY KEY (wallet, mint, kind)
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def watched(con):
    return [w for w in config.WATCH_WALLETS if w]


def events(trades, milestones, now, window_hours, min_buy_sol, skip_mints,
           sell_lookback_hours=None):
    """Pure: which alerts the trades call for.

    trades: dicts with wallet, mint, side, quote_sol, token_amount, ts.
    Returns [(wallet, mint, kind, info)] where kind is 'buy3', 'buy5', ... (buys
    within window_hours) or 'sell': the first sell after a wallet reached the
    lowest milestone, looking back sell_lookback_hours so a position built last
    week still gets its exit alert.
    """
    since = now - window_hours * 3600
    sell_since = now - (sell_lookback_hours or window_hours) * 3600
    g = defaultdict(list)
    for t in trades:
        if t["ts"] >= sell_since and t["mint"] not in skip_mints:
            g[(t["wallet"], t["mint"])].append(t)
    out = []
    for (w, m), all_ts in g.items():
        all_ts.sort(key=lambda t: t["ts"])
        all_buys = [t for t in all_ts if t["side"] == "buy"
                    and abs(t["quote_sol"] or 0) >= min_buy_sol]
        if len(all_buys) >= milestones[0]:
            third = all_buys[milestones[0] - 1]["ts"]
            sells = [t for t in all_ts if t["side"] == "sell" and t["ts"] > third]
            if sells:
                sol_in = sum(abs(t["quote_sol"] or 0) for t in all_buys)
                out.append((w, m, "sell", {
                    "buys": len(all_buys), "sol_in": sol_in, "avg_price_sol": None,
                    "first_ts": all_buys[0]["ts"], "last_ts": all_buys[-1]["ts"],
                    "sell_ts": sells[0]["ts"],
                    "sold_sol": sum(abs(t["quote_sol"] or 0) for t in sells)}))
        ts = [t for t in all_ts if t["ts"] >= since]
        buys = [t for t in ts if t["side"] == "buy" and abs(t["quote_sol"] or 0) >= min_buy_sol]
        n = len(buys)
        sol_in = sum(abs(t["quote_sol"] or 0) for t in buys)
        tok = sum(t["token_amount"] for t in buys)
        info = {"buys": n, "sol_in": sol_in, "avg_price_sol": sol_in / tok if tok else None,
                "first_ts": buys[0]["ts"] if buys else None,
                "last_ts": buys[-1]["ts"] if buys else None}
        for k in milestones:
            if n >= k:
                out.append((w, m, f"buy{k}", info))
    return out


def _fmt_usd(x):
    if not x:
        return "?"
    return f"${x / 1e6:.2f}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def message(wallet, kind, info, tok, sol_usd):
    who = f"{wallet[:4]}…{wallet[-4:]}"
    sym = tok.get("symbol") or "?"
    span_h = ((info["last_ts"] or 0) - (info["first_ts"] or 0)) / 3600
    now_px = tok.get("price_usd")
    vs = ""
    if now_px and info.get("avg_price_sol") and sol_usd:
        avg_usd = info["avg_price_sol"] * sol_usd
        vs = f" · price now {(now_px / avg_usd - 1) * 100:+.0f}% vs its avg entry"
    head = (f"🔴 {who} started SELLING {sym} ({info['sold_sol']:.1f} SOL so far) after "
            f"{info['buys']} buys" if kind == "sell" else
            f"🟢 {who} made buy #{kind[3:]} on {sym}: {info['buys']} buys, "
            f"{info['sol_in']:.1f} SOL over {span_h:.1f}h")
    return (f"{head}{vs}\nMCap {_fmt_usd(tok.get('mcap'))} · liquidity "
            f"{_fmt_usd(tok.get('liquidity'))} · 24h {tok.get('change_h24')}%\n"
            f"{tok.get('url') or ''}")


def seed(con, wallets, now=None):
    """Record existing milestones for these wallets without alerting (used
    after loading a wallet's history, so old buys/sells don't fire alerts)."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    if not wallets:
        return 0
    q = ",".join("?" * len(wallets))
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM trades WHERE wallet IN ({q}) AND ts >= ?",
        list(wallets) + [now - config.ACCUM_SELL_LOOKBACK_DAYS * 86400])]
    evs = events(rows, config.ACCUM_MILESTONES, now, config.ACCUM_WINDOW_HOURS,
                 config.ACCUM_MIN_BUY_SOL, config.NON_MEME_MINTS | config.QUOTE_MINTS,
                 sell_lookback_hours=config.ACCUM_SELL_LOOKBACK_DAYS * 24)
    con.executemany("INSERT OR IGNORE INTO accum_alerts VALUES (?,?,?,?,?)",
                    [(w, m, k, now, "seeded") for w, m, k, _ in evs])
    con.commit()
    return len(evs)


def check(con, notify, log=print, now=None):
    """Send any new accumulation alerts. Returns the messages sent."""
    con.executescript(SCHEMA)
    now = now or int(time.time())
    ws = watched(con)
    if not ws:
        return []
    q = ",".join("?" * len(ws))
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM trades WHERE wallet IN ({q}) AND ts >= ?",
        ws + [now - config.ACCUM_SELL_LOOKBACK_DAYS * 86400])]
    evs = events(rows, config.ACCUM_MILESTONES, now, config.ACCUM_WINDOW_HOURS,
                 config.ACCUM_MIN_BUY_SOL, config.NON_MEME_MINTS | config.QUOTE_MINTS,
                 sell_lookback_hours=config.ACCUM_SELL_LOOKBACK_DAYS * 24)
    sent = {(r[0], r[1], r[2]) for r in con.execute("SELECT wallet, mint, kind FROM accum_alerts")}
    new = [e for e in evs if (e[0], e[1], e[2]) not in sent]
    # First run: record what already happened without spamming old alerts.
    seeded = con.execute("SELECT v FROM meta WHERE k='accum_seeded'").fetchone()
    if not seeded:
        con.executemany("INSERT OR IGNORE INTO accum_alerts VALUES (?,?,?,?,?)",
                        [(w, m, k, now, "seeded") for w, m, k, _ in new])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('accum_seeded', '1')")
        con.commit()
        log(f"   accumulation alerts armed for {len(ws)} watched wallets "
            f"({len(new)} existing milestones recorded silently)")
        return []
    if not new:
        return []
    # Only the highest new buy milestone per wallet+token, plus any sell.
    best = {}
    for w, m, k, info in new:
        key = (w, m, k == "sell")
        if key not in best or (k != "sell" and int(k[3:]) > int(best[key][2][3:])):
            best[key] = (w, m, k, info)
    pairs = dexscreener.best_pairs(list({m for _, m, _, _ in new}))
    sol_usd = dexscreener.sol_usd()
    msgs = []
    for w, m, k, info in new:
        if (w, m, k == "sell") in best and best[(w, m, k == "sell")][2] == k:
            text = message(w, k, info, dexscreener.summarize(pairs.get(m)), sol_usd)
            notify(text, log)
            log("   ALERT " + text.splitlines()[0])
            msgs.append(text)
        else:
            text = "superseded"
        con.execute("INSERT OR IGNORE INTO accum_alerts VALUES (?,?,?,?,?)", (w, m, k, now, text))
    con.commit()
    return msgs


def recent(con, limit=20):
    con.executescript(SCHEMA)
    return [dict(r) for r in con.execute(
        "SELECT * FROM accum_alerts WHERE text NOT IN ('seeded', 'superseded') "
        "ORDER BY sent_at DESC LIMIT ?", (limit,))]
