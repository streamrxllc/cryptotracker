"""Step 3: poll tracked wallets, store their trades, compute and alert signals."""
import time

import requests

from . import accumulation, config, db, dexscreener, paper, report, signals
from .helius import Helius, HeliusError
from .parse import extract_trades

STATE = {"last_cycle": None, "last_error": None, "credits": 0, "signals": [],
         "sol_usd": None}


def poll_wallet(hel, con, w):
    """Fetch and store new trades for one wallet. Returns number of new trades."""
    latest = hel.latest_signature(w["address"])        # 1 credit
    if latest is None or latest == w["last_sig"]:
        return 0
    if w["last_slot"]:
        filters = {"slot": {"gt": w["last_slot"]}}
    else:
        filters = {"blockTime": {"gte": int(time.time() - config.BACKFILL_HOURS * 3600)}}
    filters["status"] = "succeeded"
    new, max_slot = [], w["last_slot"] or 0
    for tx in hel.transactions(w["address"], order="asc", full=True, limit=100,
                               filters=filters, max_txs=2000):
        max_slot = max(max_slot, int(tx.get("slot") or 0))
        new += extract_trades(tx, w["address"])
    if not max_slot:
        max_slot = hel.rpc("getSlot", [])  # nothing new; remember where we are
    db.insert_trades(con, new)
    db.upsert_wallet(con, w["address"], last_sig=latest, last_slot=max_slot or None)
    return len(new)


def current_signals(con, sol_usd=None):
    since = int(time.time() - config.SIGNAL_WINDOW_HOURS * 3600)
    rows = con.execute("""SELECT t.* FROM trades t JOIN wallets w ON w.address=t.wallet
                          WHERE w.tracked=1 AND t.ts>=?""", (since,)).fetchall()
    weights = {r["address"]: r["hits"] or 1 for r in
               con.execute("SELECT address, hits FROM wallets WHERE tracked=1")}
    sigs = signals.compute([dict(r) for r in rows], weights, sol_usd)
    if sigs:
        pairs = dexscreener.best_pairs([s["mint"] for s in sigs])
        for s in sigs:
            info = dexscreener.summarize(pairs.get(s["mint"]))
            db.upsert_token(con, s["mint"], info)
            s.update({k: info.get(k) for k in
                      ("symbol", "name", "price_usd", "mcap", "liquidity", "change_h24", "url")})
            ch = info.get("change_h24") or 0
            s["late"] = ch >= config.LATE_PCT_24H
    return sigs


def notify(text, log=print):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    except requests.RequestException as e:
        log(f"telegram failed: {e}")


def send_alerts(con, sigs, log=print):
    if not (config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return
    now = int(time.time())
    for s in sigs:
        row = con.execute("SELECT sent_at FROM alerts WHERE mint=?", (s["mint"],)).fetchone()
        if row and now - row["sent_at"] < config.ALERT_COOLDOWN_HOURS * 3600:
            continue
        mcap = f"${s['mcap']:,.0f}" if s.get("mcap") else "?"
        text = (f"🟢 {s.get('symbol') or s['mint'][:8]}: {s['wallets']} tracked wallets "
                f"net buying ({s['net_sol']} SOL, {s['accumulators']} adding repeatedly)\n"
                f"MCap {mcap} · 24h {s.get('change_h24')}%"
                f"{' · ⚠️ already ran' if s.get('late') else ''}\n"
                f"{s.get('url') or s['mint']}")
        try:
            requests.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
                          json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=15)
            con.execute("INSERT OR REPLACE INTO alerts VALUES (?,?)", (s["mint"], now))
            con.commit()
        except requests.RequestException as e:
            log(f"telegram alert failed: {e}")


def cycle(hel, con, log=print):
    wallets = con.execute("SELECT * FROM wallets WHERE tracked=1").fetchall()
    total = 0
    for w in wallets:
        try:
            total += poll_wallet(hel, con, w)
        except HeliusError as e:
            log(f"  {w['address'][:8]}… error: {e}")
            STATE["last_error"] = str(e)
    # Watched wallets are polled even if vetting didn't pick them.
    tracked = {w["address"] for w in wallets}
    for addr in config.WATCH_WALLETS:
        if addr not in tracked:
            db.upsert_wallet(con, addr, source="manual", label="watched", tracked=1)
    try:
        accumulation.check(con, notify, log)
    except Exception as e:  # alerts must never stop tracking
        log(f"accumulation alert error: {e}")
    try:
        report.maybe_send_daily(con, notify, log)
    except Exception as e:
        log(f"daily report error: {e}")
    sol_usd = dexscreener.sol_usd()
    sigs = current_signals(con, sol_usd)
    send_alerts(con, sigs, log)
    try:
        for e in paper.update(con, sigs, log=log):
            notify(e, log)
    except Exception as e:  # paper trading must never stop tracking
        log(f"paper trading error: {e}")
    STATE.update(last_cycle=int(time.time()), credits=hel.credits, signals=sigs,
                 sol_usd=sol_usd)
    log(f"[{time.strftime('%H:%M')}] {len(wallets)} wallets polled, {total} new trades, "
        f"{len(sigs)} signals, ~{hel.credits:,} credits this session")
    for s in sigs[:10]:
        log(f"   {s.get('symbol') or s['mint'][:8]:<10} wallets={s['wallets']} "
            f"net={s['net_sol']} SOL score={s['score']}{'  (late)' if s.get('late') else ''}")
    return sigs


def loop(log=print):
    hel, con = Helius(), db.connect()
    n = con.execute("SELECT COUNT(*) FROM wallets WHERE tracked=1").fetchone()[0]
    per_day = n * (24 * 60 / config.POLL_MINUTES)
    log(f"Tracking {n} wallets every {config.POLL_MINUTES:g} min "
        f"(~{per_day * 30:,.0f} credits/month for change checks, plus fetches).")
    while True:
        try:
            cycle(hel, con, log)
        except Exception as e:  # keep the loop alive on Replit
            STATE["last_error"] = str(e)
            log(f"cycle error: {e}")
        time.sleep(config.POLL_MINUTES * 60)
