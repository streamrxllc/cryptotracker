import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, dexscreener, trace
from tests.test_core import swap


def test_sol_recipients():
    tx = swap("SRC", 0, 0, 100)                       # SRC signs, sends SOL to POOL slot
    tx["meta"]["preTokenBalances"] = tx["meta"]["postTokenBalances"] = []
    tx["meta"]["preBalances"] = [10e9, 0]
    tx["meta"]["postBalances"] = [7e9, 3e9]
    assert trace.sol_recipients(tx, "SRC") == [("Pool111111111111111111111111111111111111111", 3.0)]
    assert trace.sol_recipients(tx, "SOMEONE_ELSE") == []


def test_group_sources():
    chains = {"A": ["F1", "BOSS"], "B": ["F2", "BOSS"], "C": ["F1", "BOSS"], "D": ["LONE"]}
    profits = {"A": 50, "B": 20, "C": 5, "D": 99}
    coins = {"A": {"X"}, "B": {"Y"}, "C": {"X"}, "D": {"Z"}}
    srcs = {s["source"]: s for s in trace.group_sources(chains, profits, coins)}
    assert set(srcs) == {"F1", "BOSS"}
    assert srcs["BOSS"]["wallets"] == ["A", "B", "C"] and srcs["BOSS"]["coins"] == {"X", "Y"}
    assert srcs["BOSS"]["profit_sol"] == 75 and srcs["BOSS"]["depth"] == 2
    assert trace.group_sources(chains, profits, coins)[0]["source"] == "BOSS"


def test_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    con = db.connect()
    con.executemany("INSERT INTO discovery_hits VALUES (?,?,?,?,?,?,?)",
                    [(w, m, m, 0, 1, 1.0, 10.0) for w in ("A", "B", "LOSER")
                     for m in ("COINX", "COINY")])
    con.commit()
    funders = {"A": "MID1", "B": "MID2", "MID1": "BOSS", "MID2": "BOSS", "BOSS": "BINANCE",
               "LOSER": "BINANCE"}

    class H:
        credits = 0
        def __init__(self, *a, **k): pass
        def transactions(self, a, full=True, limit=100, order="asc", **k):
            if not full:                                     # hub check
                return iter([{"blockTime": 1}] * (1000 if a == "BINANCE" else 3))
            if limit == 20:                                  # funding lookup
                tx = swap(a, 0, 0, 1)
                tx["transaction"]["message"]["accountKeys"] = [
                    {"pubkey": funders[a], "signer": True}, {"pubkey": a, "signer": False}]
                tx["meta"].update(preBalances=[10e9, 0], postBalances=[5e9, 5e9],
                                  preTokenBalances=[], postTokenBalances=[])
                return iter([tx])
            if a == "BOSS":                                  # BOSS funds a fresh wallet
                tx = swap("BOSS", 0, 0, 2_000_000_000)
                tx["transaction"]["message"]["accountKeys"][1]["pubkey"] = "FRESH"
                tx["meta"].update(preBalances=[10e9, 0], postBalances=[8e9, 2e9],
                                  preTokenBalances=[], postTokenBalances=[])
                return iter([tx])
            raise AssertionError(a)
    monkeypatch.setattr(trace, "Helius", H)

    def hist(hel, w, mint, max_txs=300):
        if w == "LOSER":
            return [swap(w, -10, 1000, 1, mint=mint), swap(w, 2, -1000, 2, mint=mint)]
        return [swap(w, -10, 1000, 1, mint=mint), swap(w, 60, -1000, 2, mint=mint)]
    monkeypatch.setattr(trace, "token_history", hist)
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(dexscreener, "best_pairs", lambda ms: {})
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 2_000_000_100)
    out = []
    srcs = trace.run(log=out.append, csv_path=str(tmp_path / "t.csv"))
    text = "\n".join(out)
    assert "2 wallets made 5+ SOL and 2x+ on at least one winner (2 on 2+)" in text
    assert srcs[0]["source"] == "BOSS" and srcs[0]["wallets"] == ["A", "B"]
    assert "FRESH  2.0 SOL" in text
    rows = open(tmp_path / "t.csv").read()
    assert "MID1,BOSS" in rows and "LOSER" not in rows
