"""Daily morning report: what the watched wallets hold and are accumulating.

Sent by email (Gmail SMTP with an app password) and summarized on Telegram,
once a day at REPORT_HOUR in REPORT_TZ, from inside the tracker loop.
"""
import html
import smtplib
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

    for p in pos:
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


def send_email(subject, html_body, text_body):
    if not (config.SMTP_USER and config.SMTP_PASSWORD and config.REPORT_EMAIL_TO):
        raise RuntimeError("Set SMTP_USER, SMTP_PASSWORD and REPORT_EMAIL_TO in Replit Secrets.")
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, config.SMTP_USER, config.REPORT_EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30) as s:
        s.login(config.SMTP_USER, config.SMTP_PASSWORD)
        s.sendmail(config.SMTP_USER, [a.strip() for a in config.REPORT_EMAIL_TO.split(",")],
                   msg.as_string())


def send(con=None, notify=None, log=print, now=None):
    con = con or db.connect()
    subject, html_body, text_body, tg = render(gather(con, now))
    sent = []
    if config.SMTP_USER and config.SMTP_PASSWORD and config.REPORT_EMAIL_TO:
        send_email(subject, html_body, text_body)
        sent.append(f"email to {config.REPORT_EMAIL_TO}")
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
