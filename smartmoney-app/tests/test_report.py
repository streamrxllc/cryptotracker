import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, dexscreener, report

D9 = "D9Jibi4oneM46vCjF2qq8Azc2kPxCTfJKi6i5uMnz8Aq"
U6 = "6uu3hHQinzVRFvpdy7m44RM8w221ACDxqXpmvLiLUFd6"
NOW = 1_800_000_000


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATCH_WALLETS", [D9, U6])
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(dexscreener, "best_pairs", lambda ms: {m: {
        "baseToken": {"address": m, "symbol": m}, "quoteToken": {"address": config.WSOL},
        "priceNative": "0.002", "priceUsd": "0.2", "marketCap": 827000,
        "url": f"https://dexscreener.com/{m}"} for m in ms})
    con = db.connect(str(tmp_path / "r.db"))
    rows = []
    for i in range(6):   # D9 accumulating NORMIE, 5 buys in last 24h
        rows.append((f"a{i}", D9, "NORMIE", "buy", 1000, 1.0, 0, NOW - (5 - i) * 20000, 1))
    rows.append(("b0", U6, "NORMIE", "buy", 500, 0.5, 0, NOW - 200000, 1))
    rows.append(("c0", U6, "FTR", "buy", 1000, 1.0, 0, NOW - 300000, 1))
    rows.append(("c1", U6, "FTR", "sell", 500, -3.0, 0, NOW - 3600, 1))      # selling today
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return con


def test_render_sections(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    subject, html_body, text, tg = report.render(report.gather(con, NOW))
    assert "1 accumulating, 1 selling" in subject
    acc = text.split("ACCUMULATING")[1].split("SELLING")[0]
    assert "D9Ji" in acc and "NORMIE" in acc and "+5 buys" in acc   # 5 buys within 24h
    assert "6uu3" in text.split("SELLING")[1].split("HELD")[0]
    assert "NORMIE: 6uu3…UFd6, D9Ji…z8Aq" in text           # held by both
    assert "<table" in html_body and "dexscreener.com/NORMIE" in html_body
    assert tg.startswith("☀️") and "🔴 6uu3" in tg


def test_daily_once_after_hour(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    sent = []
    monkeypatch.setattr(report, "send", lambda con, notify, log, now: sent.append(now))
    monkeypatch.setattr(config, "REPORT_TZ", "America/New_York")
    monkeypatch.setattr(config, "REPORT_HOUR", 8)
    # 1_800_000_000 = 2027-01-15 03:00 New York -> before 8am, nothing.
    assert not report.maybe_send_daily(con, None, log=lambda *a: None, now=NOW)
    at9 = NOW + 6 * 3600                      # 09:00
    assert report.maybe_send_daily(con, None, log=lambda *a: None, now=at9)
    assert not report.maybe_send_daily(con, None, log=lambda *a: None, now=at9 + 3600)
    assert report.maybe_send_daily(con, None, log=lambda *a: None, now=at9 + 86400)
    assert len(sent) == 2


def test_send_email_via_resend(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "REPORT_EMAIL_TO", "me@x.com, you@y.com")
    calls = {}

    class R:
        status_code, text = 200, '{"id":"1"}'
    def fake_post(url, timeout, headers, json):
        calls.update(url=url, headers=headers, json=json)
        return R()
    monkeypatch.setattr(report.requests, "post", fake_post)
    report.send(con, None, log=lambda *a: None, now=NOW)
    assert calls["url"] == "https://api.resend.com/emails"
    assert calls["headers"]["Authorization"] == "Bearer re_test"
    assert calls["json"]["to"] == ["me@x.com", "you@y.com"]
    assert calls["json"]["subject"].startswith("Smart money") and "<table" in calls["json"]["html"]


def test_send_email_via_smtp(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    for k, v in {"SMTP_USER": "me@gmail.com", "SMTP_PASSWORD": "app-pass",
                 "REPORT_EMAIL_TO": "me@gmail.com"}.items():
        monkeypatch.setattr(config, k, v)
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): calls["host"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, u, p): calls["login"] = (u, p)
        def sendmail(self, f, to, msg): calls["mail"] = (f, to, msg)
    monkeypatch.setattr(report.smtplib, "SMTP_SSL", FakeSMTP)
    report.send(con, None, log=lambda *a: None, now=NOW)
    assert calls["host"] == ("smtp.gmail.com", 465) and calls["login"][0] == "me@gmail.com"
    assert calls["mail"][1] == ["me@gmail.com"] and "Smart money" in calls["mail"][2]


def test_send_via_webhook(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "REPORT_WEBHOOK_URL", "https://hooks.zapier.com/hooks/catch/1/abc/")
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "SMTP_USER", "")
    got = {}

    class R:
        status_code, text = 200, "ok"
    def fake_post(url, timeout, json, **k):
        got.update(url=url, json=json)
        return R()
    monkeypatch.setattr(report.requests, "post", fake_post)
    report.send(con, None, log=lambda *a: None, now=NOW)
    assert got["url"].startswith("https://hooks.zapier.com")
    assert set(got["json"]) == {"subject", "html", "text", "telegram", "sms", "date"}
    sms = got["json"]["sms"]
    assert len(sms) <= 153 and sms.isascii()
    assert "SELL 6uu3 FTR" in sms and "BUY D9Ji NORMIE x5" in sms


def test_sms_truncates_cleanly(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    data = report.gather(con, NOW)
    many = [dict(data["positions"][0], symbol=f"TOKEN{i:02d}LONGNAME") for i in range(20)]
    sms = report.sms_text({**data, "positions": many})
    assert len(sms) <= 153 and sms.endswith("more")


def test_accumulation_board(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    data = report.gather(con, NOW)
    norm = next(p for p in data["positions"] if p["wallet"] == D9 and p["mint"] == "NORMIE")
    assert sum(norm["daily"]) == 6 and len(norm["daily"]) == report.BOARD_DAYS
    assert norm["daily"][-1] >= 1          # bought today
    _, html_body, text, _ = report.render(data)
    board = text.split("ACCUMULATION BOARD")[1].split("ACCUMULATING")[0]
    assert "NORMIE" in board and "6 buys" in board
    assert "FTR" not in board               # only 1 buy -> not on the board
    assert "Accumulation board" in html_body and "🤝" in html_body
