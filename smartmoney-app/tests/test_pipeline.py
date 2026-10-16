"""End-to-end wiring test with fake Helius and Dexscreener (no network)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.test_core import swap, MINT
from smartmoney import config, db, discover, vet, tracker, dexscreener, web
import smartmoney.helius as H

MINT2 = "Meme222222222222222222222222222222222222222"
NOW = int(time.time())


class FakeHelius:
    def __init__(self, *a, **k): self.credits = 0
    def rpc(self, m, p, credits=1): return 123
    def latest_signature(self, a): return f"new-{a}"
    def transactions(self, address, order="asc", full=True, limit=100, filters=None, max_txs=None):
        self.credits += 10
        if address in (MINT, MINT2):
            m = address
            txs = [swap("DEV", -5, 5000, 1000, mint=m)]
            txs += [swap(w, -1, 1000, 2000 + i, mint=m) for i, w in enumerate(["W1", "W2", "W3", "BOT"])]
            txs += [swap(f"L{i}", -1, 10, 9000 + i, mint=m) for i in range(500)]
            return iter(txs)
        if order == "asc" and limit == 20:  # funding lookup
            return iter([])
        if order == "desc":  # vetting: recent history
            gap = 5 if address == "BOT" else 3 * 3600
            return iter([swap(address, -1, 10, NOW - i * gap, mint=f"T{i % 3}") for i in range(50)])
        # tracking: each wallet buys token Z
        return iter([swap(address, -1, 100, NOW - 600, mint="Zzz")])


def fake_pairs(mints):
    return {m: {"baseToken": {"address": m, "symbol": m[:4]}, "quoteToken": {"address": config.WSOL},
                "priceNative": "0.01", "priceUsd": "1.5", "liquidity": {"usd": 1e5},
                "marketCap": 1e6, "priceChange": {"h24": 20}, "url": "https://x"} for m in mints}


def test_pipeline(tmp_path, monkeypatch):
    winners = tmp_path / "w.txt"; winners.write_text(f"{MINT}\n{MINT2}  # comment\n")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "WINNERS_FILE", str(winners))
    monkeypatch.setattr(config, "EXPENSIVE_RUN_STOP", 100)
    monkeypatch.setattr(config, "MIN_HITS", 2)
    monkeypatch.setattr(config, "WATCH_WALLETS", [])
    for mod in (discover, vet):
        monkeypatch.setattr(mod, "Helius", FakeHelius)
    monkeypatch.setattr(dexscreener, "best_pairs", fake_pairs)
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 150.0)
    out = []
    discover.run(log=out.append)
    con = db.connect()
    assert con.execute("SELECT COUNT(*) FROM wallets").fetchone()[0] == 4
    vet.run(log=out.append)
    tracked = {r[0] for r in con.execute("SELECT address FROM wallets WHERE tracked=1")}
    assert tracked == {"W1", "W2", "W3"}
    # A wallet measured only by the old vetting must not be picked; pick is free.
    db.upsert_wallet(con, "LEGACY", source="discovered", hits=9, tx_per_day=1.0)
    picked = {c["address"] for c in vet.pick(con, log=out.append)}
    assert picked == {"W1", "W2", "W3"}
    sigs = tracker.cycle(FakeHelius(), con, log=out.append)
    assert sigs and sigs[0]["mint"] == "Zzz" and sigs[0]["wallets"] == 3
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "pw")
    client = web.app.test_client()
    client.post("/login", data={"password": "pw"})
    page = client.get("/").data.decode()
    assert "Zzz"[:4] in page and "Tracked wallets (3)" in page
    assert "Paper trades" in page and "PAPER BUY" in "\n".join(out)
    print("\n".join(out))
