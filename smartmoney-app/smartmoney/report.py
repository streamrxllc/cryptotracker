"""Daily morning report: what the watched wallets hold and are accumulating.

Sent by email (Gmail SMTP with an app password) and summarized on Telegram,
once a day at REPORT_HOUR in REPORT_TZ, from inside the tracker loop.
"""
import html
import smtplib

import requests
import time
from collections import defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config, db, dexscreener, paper
from .review import positions

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def _tz():
    try:
        return ZoneInfo(config.REPORT_TZ)
    except Exception:
        return None


def local_now(now=None):
    return datetime.fromtimestamp(now or time.time(), _tz())


def _usd(x):
    if x is None:
        return "–"
    if abs(x) >= 1e6:
        return f"${x / 1e6:.2f}M"
    if abs(x) >= 1e3:
        return f"${x / 1e3:.1f}k"
    return f"${x:,.0f}"


def _short(a):
    return f"{a[:4]}…{a[-4:]}"


def _date(ts):
    return local_now(ts).strftime("%b %d")


BOARD_DAYS = 7


def gather(con, now=None):
    """Collect the report's data. Returns a dict (no network beyond Dexscreener)."""
    now = now or int(time.time())
    ws = [w for w in config.WATCH_WALLETS if w]
    since = now - config.REPORT_LOOKBACK_DAYS * 86400
    q = ",".join("?" * len(ws)) or "''"
    rows = [dict(r) for r in con.execute(
        f"SELECT * FROM trades WHERE wallet IN ({q}) AND ts >= ? AND side IN ('buy','sell')",
        ws + [since])] if ws else []
    rows = [r for r in rows if r["mint"] not in config.NON_MEME_MINTS]
    mints = list({r["mint"] for r in rows})
    sol_usd = dexscreener.sol_usd() or 0
    pairs = dexscreener.best_pairs(mints) if mints else {}
    prices = {m: dexscreener.price_in_sol(p, sol_usd) for m, p in pairs.items()}
    info = {m: dexscreener.summarize(p) for m, p in pairs.items()}
    pos = positions(rows, prices)

    day = now - 86400
    recent = defaultdict(lambda: {"buys": 0, "sells": 0, "sol_buy": 0.0})
    for r in rows:
        if r["ts"] >= day:
            k = (r["wallet"], r["mint"])
            if r["side"] == "buy":
                recent[k]["buys"] += 1
                recent[k]["sol_buy"] += abs(r["quote_sol"] or 0)
            else:
                recent[k]["sells"] += 1

    # Buys per local calendar day for the last BOARD_DAYS days, oldest first.
    today0 = local_now(now).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [int(today0.timestamp()) - d * 86400 for d in range(BOARD_DAYS - 1, -1, -1)]
    daily = defaultdict(lambda: [0] * BOARD_DAYS)
    last_buy = {}
    for r in rows:
        if r["side"] != "buy":
            continue
        k = (r["wallet"], r["mint"])
        last_buy[k] = max(last_buy.get(k, 0), r["ts"])
        for i in range(BOARD_DAYS - 1, -1, -1):
            if r["ts"] >= starts[i]:
                daily[k][i] += 1
                break

    for p in pos:
        k = (p["wallet"], p["mint"])
        p["daily"] = daily[k]
        p["last_buy"] = last_buy.get(k)
        i = info.get(p["mint"], {})
        p["symbol"] = i.get("symbol") or p["mint"][:6]
        p["mcap"], p["url"] = i.get("mcap"), i.get("url")
        p["change_h24"] = i.get("change_h24")
        p["recent"] = recent.get((p["wallet"], p["mint"]), {"buys": 0, "sells": 0, "sol_buy": 0})

    holders = defaultdict(set)
    for p in pos:
        if p["status"] != "closed":
            holders[p["mint"]].add(p["wallet"])
    return {"now": now, "sol_usd": sol_usd, "wallets": ws, "positions": pos,
            "shared": {m: w for m, w in holders.items() if len(w) >= 2},
            "day_labels": [local_now(t).strftime("%a")[0] for t in starts],
            "paper": paper.summary(con)}


def render(data):
    """(subject, html_body, text_body, telegram_text). Pure."""
    sol = data["sol_usd"] or 0
    pos = data["positions"]
    open_ = [p for p in pos if p["status"] != "closed"]
    acc = sorted([p for p in open_ if p["recent"]["buys"] and not p["recent"]["sells"]],
                 key=lambda p: -p["recent"]["sol_buy"])
    selling = [p for p in pos if p["recent"]["sells"]]
    today = local_now(data["now"]).strftime("%a %b %d")
    subject = (f"Smart money {today}: {len(acc)} accumulating, {len(selling)} selling"
               if pos else f"Smart money {today}: no recent activity")

    css = ("font-family:-apple-system,Segoe UI,sans-serif;font-size:14px;color:#111")
    th = "text-align:left;padding:6px 8px;border-bottom:2px solid #ddd;font-size:12px;color:#666"
    td = "padding:6px 8px;border-bottom:1px solid #eee"

    def tok(p):
        name = html.escape(p["symbol"])
        return f"<a href='{html.escape(p['url'])}'>{name}</a>" if p.get("url") else name

    def pct(x):
        color = "#1a7f37" if x > 0 else "#cf222e"
        return f"<span style='color:{color}'>{x:+.0f}%</span>"

    def table(headers, rows):
        h = "".join(f"<th style='{th}'>{x}</th>" for x in headers)
        b = "".join("<tr>" + "".join(f"<td style='{td}'>{c}</td>" for c in r) + "</tr>"
                    for r in rows)
        return f"<table style='border-collapse:collapse;width:100%'><tr>{h}</tr>{b}</table>"

    parts = [f"<div style='{css}'><h2 style='margin:0 0 4px'>Smart money report · {today}</h2>"
             f"<p style='color:#666;margin:0 0 16px'>Watched wallets, last "
             f"{config.REPORT_LOOKBACK_DAYS:g} days of tracked trades. SOL = ${sol:,.0f}.</p>"]
    text = [f"Smart money report · {today} (SOL ${sol:,.0f})", ""]

    # --- Accumulation board: positions being built, most buys first -------
    board = sorted([p for p in open_ if p["buys"] >= 2],
                   key=lambda p: (-p["buys"], -p["sol_in"]))
    shared = data["shared"]

    def strip(counts):
        cells = []
        for c in counts:
            shade = ("#eee" if c == 0 else "#b7e4c7" if c <= 2 else
                     "#52b788" if c <= 5 else "#1b7f4b")
            fg = "#999" if c == 0 else ("#fff" if c > 5 else "#111")
            cells.append(f"<span style='display:inline-block;width:20px;text-align:center;"
                         f"background:{shade};color:{fg};border-radius:3px;margin-right:2px;"
                         f"font-size:11px;line-height:18px'>{c or '·'}</span>")
        return "".join(cells)

    def days_building(p):
        if not p.get("last_buy"):
            return "–"
        d = (p["last_buy"] - p["first_buy"]) / 86400
        return "same day" if d < 1 else ("1 day" if d < 1.5 else f"{d:.0f} days")

    labels = "".join(f"<span style='display:inline-block;width:20px;text-align:center;"
                     f"margin-right:2px;color:#999;font-size:10px'>{x}</span>"
                     for x in data.get("day_labels", []))
    parts.append("<h3 style='margin-bottom:4px'>📈 Accumulation board</h3>"
                 "<p style='color:#666;margin:0 0 8px;font-size:13px'>Open positions the "
                 "watched wallets have bought 2+ times, most buys first. The strip shows buys "
                 f"per day over the last {BOARD_DAYS} days (oldest → today). 🤝 = held by 2+ "
                 "watched wallets.</p>")
    text.append("ACCUMULATION BOARD (open, 2+ buys, most buys first)")
    if board:
        parts.append(table(
            ["Token", "Wallet", "Buys", f"Buys per day<br>{labels}", "Building for", "Sells",
             "Put in", "Worth now", "Result", "MCap"],
            [[tok(p) + (" 🤝" if p["mint"] in shared else ""), _short(p["wallet"]),
              f"<b>{p['buys']}</b>", strip(p["daily"]), days_building(p), p["sells"],
              f"{p['sol_in']:.1f} SOL", f"{p['held_value']:.1f} SOL", pct(p["pnl_pct"]),
              _usd(p["mcap"])] for p in board[:25]]))
        text += [f"  {p['symbol']:<10} {_short(p['wallet'])}  {p['buys']:>3} buys  "
                 f"[{' '.join(str(c) for c in p['daily'])}]  {days_building(p)}  "
                 f"{p['sells']} sells  {p['pnl_pct']:+.0f}%" for p in board[:25]]
    else:
        parts.append("<p>No positions with 2+ buys yet.</p>")
        text.append("  none")
    text.append("")

    parts.append("<h3>🟢 Accumulating in the last 24h</h3>")
    text.append("ACCUMULATING (last 24h)")
    if acc:
        parts.append(table(["Wallet", "Token", "Buys 24h", "SOL 24h", "Total buys", "Result",
                            "MCap"],
                           [[_short(p["wallet"]), tok(p), p["recent"]["buys"],
                             f"{p['recent']['sol_buy']:.1f}", p["buys"], pct(p["pnl_pct"]),
                             _usd(p["mcap"])] for p in acc]))
        text += [f"  {_short(p['wallet'])} {p['symbol']}: +{p['recent']['buys']} buys "
                 f"({p['recent']['sol_buy']:.1f} SOL), {p['buys']} total, {p['pnl_pct']:+.0f}%, "
                 f"mcap {_usd(p['mcap'])}" for p in acc]
    else:
        parts.append("<p>None.</p>")
        text.append("  none")

    parts.append("<h3>🔴 Selling in the last 24h</h3>")
    text += ["", "SELLING (last 24h)"]
    if selling:
        parts.append(table(["Wallet", "Token", "Sells 24h", "Result", "Status"],
                           [[_short(p["wallet"]), tok(p), p["recent"]["sells"], pct(p["pnl_pct"]),
                             p["status"]] for p in selling]))
        text += [f"  {_short(p['wallet'])} {p['symbol']}: {p['recent']['sells']} sells, "
                 f"{p['pnl_pct']:+.0f}% ({p['status']})" for p in selling]
    else:
        parts.append("<p>None.</p>")
        text.append("  none")

    if data["shared"]:
        parts.append("<h3>🤝 Held by 2+ watched wallets</h3><ul>")
        text += ["", "HELD BY 2+ WALLETS"]
        by_m = {p["mint"]: p for p in open_}
        for m, ws in data["shared"].items():
            p = by_m[m]
            parts.append(f"<li>{tok(p)}: {', '.join(_short(w) for w in sorted(ws))}"
                         f" · MCap {_usd(p['mcap'])}</li>")
            text.append(f"  {p['symbol']}: {', '.join(_short(w) for w in sorted(ws))}")
        parts.append("</ul>")

    parts.append("<h3>Open positions by wallet</h3>")
    text += ["", "OPEN POSITIONS"]
    for w in data["wallets"]:
        mine = sorted([p for p in open_ if p["wallet"] == w], key=lambda p: -p["held_value"])
        parts.append(f"<p style='margin:14px 0 4px'><b>{_short(w)}</b> "
                     f"<a style='color:#666;font-size:12px' href='https://solscan.io/account/{w}'>"
                     f"solscan</a></p>")
        text.append(f"  {_short(w)}")
        if not mine:
            parts.append("<p style='color:#666'>No open positions seen.</p>")
            text.append("    none")
            continue
        parts.append(table(["Token", "Put in", "Worth now", "Buys / sells", "Result", "First buy"],
                           [[tok(p), f"{p['sol_in']:.1f} SOL", f"{p['held_value']:.1f} SOL",
                             f"{p['buys']} / {p['sells']}", pct(p["pnl_pct"]),
                             _date(p["first_buy"])] for p in mine[:12]]))
        text += [f"    {p['symbol']}: in {p['sol_in']:.1f} SOL, worth {p['held_value']:.1f}, "
                 f"{p['buys']}/{p['sells']} buys/sells, {p['pnl_pct']:+.0f}%" for p in mine[:12]]

    ps = data["paper"]
    n = len(ps["open"]) + len(ps["closed"])
    if n:
        wr = f"{ps['win_rate']:.0f}%" if ps["win_rate"] is not None else "–"
        line = (f"Paper trading: {n} trades · realized ${ps['realized_usd']:+,.0f} "
                f"(win rate {wr}) · open ${ps['unrealized_usd']:+,.0f}")
        parts.append(f"<h3>Paper trading</h3><p>{html.escape(line)}</p>")
        text += ["", line]

    parts.append("<p style='color:#999;font-size:12px;margin-top:20px'>Positions only include "
                 "buys the tracker has seen; older holdings may be missing. Not financial "
                 "advice.</p></div>")

    tg = [f"☀️ Smart money · {today}"]
    tg += [f"🟢 {_short(p['wallet'])} +{p['recent']['buys']} buys {p['symbol']} "
           f"({p['recent']['sol_buy']:.1f} SOL, {p['buys']} total, {p['pnl_pct']:+.0f}%)"
           for p in acc[:8]] or ["🟢 No new accumulation in 24h"]
    tg += [f"🔴 {_short(p['wallet'])} selling {p['symbol']} ({p['pnl_pct']:+.0f}%)"
           for p in selling[:5]]
    tg.append("Full report in your email.")
    return subject, "".join(parts), "\n".join(text), "\n".join(tg)


def sms_text(data, limit=153):
    """One plain-ASCII line for SMS (SMS by Zapier cuts at 153 characters)."""
    pos = data["positions"]
    acc = sorted([p for p in pos if p["status"] != "closed" and p["recent"]["buys"]
                  and not p["recent"]["sells"]], key=lambda p: -p["recent"]["sol_buy"])
    sell = [p for p in pos if p["recent"]["sells"]]
    head = f"SmartMoney {local_now(data['now']).strftime('%m/%d')}:"
    items = [f"SELL {p['wallet'][:4]} {p['symbol']}" for p in sell] + \
            [f"BUY {p['wallet'][:4]} {p['symbol']} x{p['recent']['buys']}" for p in acc]
    if not items:
        return f"{head} no new buys or sells in 24h"
    out = head
    for i, it in enumerate(items):
        extra = f" +{len(items) - i} more" if i < len(items) else ""
        if len(out) + len(it) + 2 + len(" +99 more") > limit:
            return (out + extra)[:limit]
        out += (" " if out == head else ", ") + it
    return out[:limit]


def send_webhook(subject, html_body, text_body, tg, now, sms=""):
    r = requests.post(config.REPORT_WEBHOOK_URL, timeout=30, json={
        "subject": subject, "html": html_body, "text": text_body, "telegram": tg, "sms": sms,
        "date": local_now(now).strftime("%Y-%m-%d")})
    if r.status_code >= 300:
        raise RuntimeError(f"Webhook said {r.status_code}: {r.text[:200]}")


def email_configured():
    return bool(config.REPORT_EMAIL_TO and (config.RESEND_API_KEY or
                                             (config.SMTP_USER and config.SMTP_PASSWORD)))


def send_email(subject, html_body, text_body):
    if not config.REPORT_EMAIL_TO:
        raise RuntimeError("Set REPORT_EMAIL_TO in Replit Secrets.")
    to = [a.strip() for a in config.REPORT_EMAIL_TO.split(",") if a.strip()]
    if config.RESEND_API_KEY:
        r = requests.post("https://api.resend.com/emails", timeout=30,
                          headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
                          json={"from": config.REPORT_EMAIL_FROM, "to": to, "subject": subject,
                                "html": html_body, "text": text_body})
        if r.status_code >= 300:
            raise RuntimeError(f"Resend said {r.status_code}: {r.text}")
        return "Resend"
    if not (config.SMTP_USER and config.SMTP_PASSWORD):
        raise RuntimeError("Set RESEND_API_KEY (or SMTP_USER and SMTP_PASSWORD) in Replit Secrets.")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, config.SMTP_USER, config.REPORT_EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.sendmail(config.SMTP_USER, to, msg.as_string())
    return "Gmail"


def send(con=None, notify=None, log=print, now=None):
    con = con or db.connect()
    data = gather(con, now)
    subject, html_body, text_body, tg = render(data)
    sent = []
    if config.REPORT_WEBHOOK_URL:
        send_webhook(subject, html_body, text_body, tg, now, sms_text(data))
        sent.append("Zapier webhook")
    if email_configured():
        via = send_email(subject, html_body, text_body)
        sent.append(f"email to {config.REPORT_EMAIL_TO} via {via}")
    if notify and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        notify(tg, log)
        sent.append("Telegram summary")
    log(f"   daily report: {subject} -> {', '.join(sent) or 'nowhere (no email/Telegram set)'}")
    return subject, text_body


def maybe_send_daily(con, notify, log=print, now=None):
    """Send once per local day, at or after REPORT_HOUR. Returns True if sent."""
    lt = local_now(now)
    if lt.hour < config.REPORT_HOUR:
        return False
    key = lt.strftime("%Y-%m-%d")
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    last = con.execute("SELECT v FROM meta WHERE k='report_sent'").fetchone()
    if last and last[0] == key:
        return False
    send(con, notify, log, now)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('report_sent', ?)", (key,))
    con.commit()
    return True
