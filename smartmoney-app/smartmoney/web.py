"""Password-protected dashboard. Home page = the accumulation report."""
import hashlib
import hmac
import html
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import Flask, jsonify, redirect, request, session
from flask.sessions import SecureCookieSessionInterface

from . import accumulation, config, db, paper, report, sources, tracker

app = Flask(__name__)
app.permanent_session_lifetime = timedelta(days=30)
_cache = {"at": 0, "html": "", "subject": ""}


def _secret():
    # Derived from the password, so changing the password logs everyone out.
    return hashlib.sha256(("smartmoney-session:" + config.DASHBOARD_PASSWORD).encode()).hexdigest()


class _PasswordKeyedSessions(SecureCookieSessionInterface):
    """Sign session cookies with a key derived from the current password.
    (Set here, not in before_request: Flask opens the session before that runs.)"""

    def get_signing_serializer(self, app_):
        app_.secret_key = _secret()
        return super().get_signing_serializer(app_)


app.session_interface = _PasswordKeyedSessions()


def login_required(view):
    @wraps(view)
    def wrapped(*a, **k):
        if not config.DASHBOARD_PASSWORD:
            return (PAGE.format(title="Set a password", body=(
                "<h2>Set a dashboard password</h2><p>Add a Replit Secret named "
                "<code>DASHBOARD_PASSWORD</code>, then restart the tracker.</p>")), 403)
        if session.get("ok") != _secret()[:16]:
            return redirect("/login")
        return view(*a, **k)
    return wrapped


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{font:15px -apple-system,Segoe UI,sans-serif;background:#fff;color:#111;margin:0 auto;
padding:16px;max-width:1000px}}
table{{border-collapse:collapse;font-variant-numeric:tabular-nums}}
.rep table,.wrap{{display:block;overflow-x:auto;white-space:nowrap}}
th,td{{text-align:left;padding:6px 4px;border-bottom:1px solid #e5e5e5}}
th{{color:#666;font-weight:500;font-size:12px}} tr.late{{background:#fff4e5}}
a{{color:inherit}} .up{{color:#1a7f37}} .down{{color:#cf222e}}
.mut{{color:#666;font-size:13px}} .bar{{display:flex;gap:12px;flex-wrap:wrap;align-items:center;
justify-content:space-between;border-bottom:1px solid #eee;padding-bottom:8px;margin-bottom:12px}}
input,button{{font:inherit;padding:10px;border:1px solid #ccc;border-radius:8px}}
button{{background:#111;color:#fff;border-color:#111;cursor:pointer}}
details{{margin-top:28px}} summary{{cursor:pointer;font-weight:600;font-size:17px}}
</style></head><body>{body}</body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        if config.DASHBOARD_PASSWORD and hmac.compare_digest(
                request.form.get("password", ""), config.DASHBOARD_PASSWORD):
            session.permanent = True
            session["ok"] = _secret()[:16]
            return redirect("/")
        time.sleep(1.5)  # slow down guessing
        err = "<p class=down>Wrong password.</p>"
    return PAGE.format(title="Smart Money · Sign in", body=f"""
<form method=post style="max-width:320px;margin:15vh auto;display:grid;gap:10px">
<h2 style="margin:0">Smart Money</h2>{err}
<input type=password name=password placeholder=Password autofocus autocomplete=current-password>
<button>Sign in</button></form>""")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


def _ago(ts):
    if not ts:
        return "–"
    m = int((time.time() - ts) / 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


@app.route("/api/signals")
@login_required
def api_signals():
    return jsonify(tracker.STATE)


def _report_html(con):
    """Rebuild the report at most once a minute (prices come from Dexscreener)."""
    if time.time() - _cache["at"] > 60 or request.args.get("refresh"):
        subject, body, _, _ = report.render(report.gather(con))
        _cache.update(at=time.time(), html=body, subject=subject)
    return _cache["html"]


@app.route("/")
@login_required
def index():
    con = db.connect()
    st = tracker.STATE
    rep = _report_html(con)
    nxt = (st["last_cycle"] + config.POLL_MINUTES * 60 - time.time()) / 60 \
        if st["last_cycle"] else None
    tracked = con.execute("""SELECT address, label, hits, avg_multiple FROM wallets
                             WHERE tracked=1 ORDER BY hits DESC""").fetchall()
    rows = "".join(
        f"<tr class='{'late' if s.get('late') else ''}'>"
        f"<td><a href='{html.escape(s.get('url') or '#')}'>{html.escape(s.get('symbol') or s['mint'][:8])}</a>"
        f"{' ⚠️' if s.get('late') else ''}</td>"
        f"<td>{s['wallets']}</td><td>{s['accumulators']}</td><td>{s['net_sol']}</td>"
        f"<td>{'$%s' % format(int(s['mcap']), ',') if s.get('mcap') else '–'}</td>"
        f"<td>{s.get('change_h24') if s.get('change_h24') is not None else '–'}%</td>"
        f"<td>{_ago(s['last_buy'])}</td></tr>"
        for s in st["signals"]) or "<tr><td colspan=7>No consensus yet.</td></tr>"
    al = accumulation.recent(con)
    arows = "".join(f"<li>{_ago(a['sent_at'])}: {html.escape(a['text'].splitlines()[0])}</li>"
                    for a in al) or "<li>None yet. Fires when a watched wallet makes buy #3, #5 or #8 on a token, or starts selling one.</li>"
    so = sources.overview(con)
    def srow(r):
        a = r["address"]
        link = f"<a href='https://solscan.io/account/{a}'>{a[:4]}…{a[-4:]}</a>"
        if not r["depth"]:
            return (f"<tr><td colspan=5><b>Source {link}</b> · "
                    f"{sum(1 for x in so if x['root'] == a and x['depth'])} wallets funded</td></tr>")
        lm = r["last_mint"]
        return (f"<tr><td>{'&nbsp;' * 4 * r['depth']}{link}"
                f"{'' if r['following'] else ' <span class=mut>(stopped)</span>'}</td>"
                f"<td>{r['depth']} hop{'s' if r['depth'] > 1 else ''}</td>"
                f"<td>{r['funded_sol']:.1f}</td><td>{_ago(r['funded_ts'])}</td>"
                f"<td>{r['buys']}{(' · last ' + '<a href=https://dexscreener.com/solana/' + lm + '>' + lm[:6] + '</a>') if lm else ''}</td></tr>")
    srows = "".join(srow(r) for r in so) or "<tr><td colspan=5>Starting…</td></tr>"
    nfund = sum(1 for r in so if r["depth"] and r["following"])
    ps = paper.summary(con)
    def prow(r):
        cls = "up" if r["pnl_pct"] > 0 else "down"
        state = html.escape(r["exit_reason"] or "open")
        return (f"<tr><td><a href='{html.escape(r['url'] or '#')}'>{html.escape(r['symbol'])}</a></td>"
                f"<td class={cls}>{r['pnl_pct']:+.1f}%</td><td class={cls}>${r['pnl_usd']:+,.0f}</td>"
                f"<td>{state}</td><td>{_ago(r['opened_at'])}</td></tr>")
    prows = "".join(prow(r) for r in ps["open"] + ps["closed"][:30]) or \
        "<tr><td colspan=5>No paper trades yet. One opens automatically on each signal.</td></tr>"
    wr = f"{ps['win_rate']:.0f}%" if ps["win_rate"] is not None else "–"
    wl = "".join(
        f"<li><a href='https://solscan.io/account/{w['address']}'>{w['label'] or w['address'][:6] + '…' + w['address'][-4:]}</a>"
        f" · {w['hits'] or 0} hits</li>" for w in tracked)
    body = f"""
<div class=bar><div><b>Smart Money</b> <span class=mut>· wallets checked {_ago(st['last_cycle'])}
{f"· next check in {max(nxt, 0):.0f} min" if nxt is not None else "· tracker starting…"}
{(' · <span class=down>error: ' + html.escape(st['last_error']) + '</span>') if st['last_error'] else ''}</span></div>
<div class=mut><a href="/?refresh=1">Refresh prices</a> · <a href="/logout">Sign out</a></div></div>
<div class=rep>{rep}</div>
<details open><summary>Alerts</summary>
<ul class=mut>{arows}</ul></details>
<details open><summary>Funding sources · {nfund} funded wallets followed</summary>
<p class=mut>Wallets the watched sources sent {config.SOURCE_MIN_SOL:g}+ SOL to (up to {config.SOURCE_HOPS} hops),
tracked for {config.SOURCE_FOLLOW_DAYS:g} days. 💸 alert when funded, 🎯 when it buys.</p>
<div class=wrap><table><tr><th>Wallet</th><th>From source</th><th>SOL</th><th>Funded</th><th>Tokens bought</th></tr>
{srows}</table></div></details>
<details><summary>Paper trades · realized ${ps['realized_usd']:+,.0f}, open ${ps['unrealized_usd']:+,.0f}</summary>
<p class=mut>${config.PAPER_SIZE_USD:g} per signal, {config.PAPER_SLIPPAGE_PCT:g}% slippage each way.
{len(ps['closed'])} closed (win rate {wr}) · {len(ps['open'])} open.</p>
<div class=wrap><table><tr><th>Token</th><th>P&amp;L</th><th>$</th><th>Status</th><th>Opened</th></tr>
{prows}</table></div></details>
<details><summary>Consensus buys ({len(st['signals'])})</summary>
<p class=mut>Tokens {config.MIN_WALLETS_FOR_SIGNAL}+ tracked wallets are net buying in the last
{config.SIGNAL_WINDOW_HOURS:g}h. ⚠️ = already up {config.LATE_PCT_24H:g}%+ in 24h.</p>
<div class=wrap><table><tr><th>Token</th><th>Wallets</th><th>Adding</th><th>Net SOL</th>
<th>MCap</th><th>24h</th><th>Last buy</th></tr>{rows}</table></div></details>
<details><summary>Tracked wallets ({len(tracked)})</summary><ul class=mut>{wl}</ul></details>
<p class=mut style="margin-top:24px">Wallet trades update every {config.POLL_MINUTES:g} minutes; prices
on each refresh. This page reloads itself every 5 minutes. Not financial advice.</p>"""
    return PAGE.format(title="Smart Money", body=body).replace(
        "<head>", "<head><meta http-equiv=refresh content=300>", 1)


def serve(port, log=print):
    threading.Thread(target=tracker.loop, kwargs={"log": log}, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
