import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import accumulation, config, db, dexscreener

W = config.WATCH_WALLETS[0]


def row(side, sol, amt, ts, w=W, m="NORM", sig=None):
    return (sig or f"{w}{m}{side}{ts}", w, m, side, amt, sol if side == "buy" else -sol, 0, ts, ts)


def test_events_milestones_and_sell():
    tr = [dict(zip(["sig", "wallet", "mint", "side", "token_amount", "quote_sol", "quote_usd", "ts", "slot"],
                   row("buy", 1, 100, 1000 + i))) for i in range(5)]
    tr.append({"wallet": W, "mint": "NORM", "side": "buy", "quote_sol": 0.01, "token_amount": 1, "ts": 1010})  # dust
    kinds = {e[2] for e in accumulation.events(tr, [3, 5, 8], 2000, 72, 0.5, set())}
    assert kinds == {"buy3", "buy5"}
    tr.append({"wallet": W, "mint": "NORM", "side": "sell", "quote_sol": -2, "token_amount": 50, "ts": 1100})
    kinds = {e[2] for e in accumulation.events(tr, [3, 5, 8], 2000, 72, 0.5, set())}
    assert "sell" in kinds
    assert accumulation.events(tr, [3, 5, 8], 2000, 72, 0.5, {"NORM"}) == []


def test_check_seeds_then_alerts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(dexscreener, "best_pairs", lambda ms: {m: {
        "baseToken": {"symbol": "Normie"}, "priceUsd": "0.001", "marketCap": 827000,
        "liquidity": {"usd": 89000}, "priceChange": {"h24": 38}, "url": "https://x"} for m in ms})
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    con = db.connect(str(tmp_path / "a.db"))
    sent = []
    notify = lambda text, log=None: sent.append(text)
    now = 1_000_000
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    [row("buy", 1, 100, now - 3600 + i) for i in range(3)])
    assert accumulation.check(con, notify, log=lambda *a: None, now=now) == []   # seeded silently
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    [row("buy", 1, 100, now - 600 + i) for i in range(3)])         # now 6 buys
    msgs = accumulation.check(con, notify, log=lambda *a: None, now=now)
    assert len(msgs) == 1 and "buy #5 on Normie" in msgs[0] and "6 buys" in msgs[0]
    assert accumulation.check(con, notify, log=lambda *a: None, now=now) == []     # once only
    con.execute("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)", row("sell", 3, 50, now - 60))
    msgs = accumulation.check(con, notify, log=lambda *a: None, now=now)
    assert len(msgs) == 1 and "SELLING Normie" in msgs[0]
    assert len(accumulation.recent(con)) == 2


def test_sell_alert_for_position_built_long_ago():
    day = 86400
    tr = [{"wallet": W, "mint": "NORM", "side": "buy", "quote_sol": 5, "token_amount": 100,
           "ts": 1_000_000 + i * 600} for i in range(6)]
    tr.append({"wallet": W, "mint": "NORM", "side": "sell", "quote_sol": -20,
               "token_amount": 300, "ts": 1_000_000 + 6 * day})
    now = 1_000_000 + 6 * day + 60
    ev = accumulation.events(tr, [3, 5, 8], now, 72, 0.5, set(), sell_lookback_hours=30 * 24)
    assert [e[2] for e in ev] == ["sell"] and ev[0][3]["sold_sol"] == 20
