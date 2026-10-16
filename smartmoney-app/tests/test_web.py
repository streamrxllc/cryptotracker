import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, dexscreener, web
import smartmoney.web as w


def client(tmp_path, monkeypatch, pw="s3cret"):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "w.db"))
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", pw)
    monkeypatch.setattr(config, "WATCH_WALLETS", [])
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(dexscreener, "best_pairs", lambda ms: {})
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    w._cache["at"] = 0
    web.app.secret_key = None   # like a fresh restart
    return web.app.test_client()


def test_requires_login(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert c.get("/api/signals").status_code == 302


def test_wrong_then_right_password(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    assert "Wrong password" in c.post("/login", data={"password": "nope"}).data.decode()
    assert c.get("/").status_code == 302
    r = c.post("/login", data={"password": "s3cret"})
    assert r.status_code == 302
    page = c.get("/").data.decode()
    assert "Accumulation board" in page and "Sign out" in page and "refresh content=300" in page
    c.get("/logout")
    assert c.get("/").status_code == 302


def test_changing_password_logs_out(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    c.post("/login", data={"password": "s3cret"})
    assert c.get("/").status_code == 200
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "new-one")
    assert c.get("/").status_code == 302


def test_no_password_configured(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch, pw="")
    r = c.get("/")
    assert r.status_code == 403 and "DASHBOARD_PASSWORD" in r.data.decode()
