"""Tiny phone-friendly dashboard served by Replit's webview."""
import html
import threading
import time

from flask import Flask, jsonify

from . import db, tracker

app = Flask(__name__)


def _ago(ts):
    if not ts:
        return "–"
    m = int((time.time() - ts) / 60)
    return f"{m}m ago" if m < 120 else f"{m // 60}h ago"


@app.route("/api/signals")
def api_signals():
    return jsonify(tracker.STATE)


@app.route("/")
def index():
    con = db.connect()
    st = tracker.STATE
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
    wl = "".join(
        f"<li><a href='https://solscan.io/account/{w['address']}'>{w['label'] or w['address'][:6] + '…' + w['address'][-4:]}</a>"
        f" · {w['hits'] or 0} hits</li>" for w in tracked)
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Smart Money</title>
<style>
:root{{--bg:#fff;--fg:#111;--mut:#666;--line:#e5e5e5;--warn:#fff4e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#111;--fg:#eee;--mut:#999;--line:#333;--warn:#3a2a10}}}}
body{{font:15px system-ui;background:var(--bg);color:var(--fg);margin:0;padding:16px;max-width:900px}}
table{{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line)}}
th{{color:var(--mut);font-weight:500;font-size:12px}} tr.late{{background:var(--warn)}}
a{{color:inherit}} .mut{{color:var(--mut);font-size:13px}} .wrap{{overflow-x:auto}}
</style>
<h2>Consensus buys</h2>
<p class=mut>Tokens {tracker.config.MIN_WALLETS_FOR_SIGNAL}+ tracked wallets are net buying in the last
{tracker.config.SIGNAL_WINDOW_HOURS:g}h. Last check: {_ago(st['last_cycle'])}.
{('Error: ' + html.escape(st['last_error'])) if st['last_error'] else ''}</p>
<div class=wrap><table><tr><th>Token</th><th>Wallets</th><th>Adding</th><th>Net SOL</th>
<th>MCap</th><th>24h</th><th>Last buy</th></tr>{rows}</table></div>
<p class=mut>⚠️ = already up {tracker.config.LATE_PCT_24H:g}%+ in 24h. Not financial advice; verify on-chain before buying.</p>
<h3>Tracked wallets ({len(tracked)})</h3><ul class=mut>{wl}</ul>"""


def serve(port, log=print):
    threading.Thread(target=tracker.loop, kwargs={"log": log}, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
