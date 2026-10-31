import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, dexscreener, sources
from tests.test_core import swap

L = 1_000_000_000
NOW = 1_800_000_000
SRC = "SRC1111111111111111111111111111111111111111"


def send(frm, to, sol, ts, slot=None):
    return {"slot": slot or ts, "blockTime": ts,
            "transaction": {"signatures": [f"send-{frm}-{to}-{ts}"],
                            "message": {"accountKeys": [{"pubkey": frm, "signer": True},
                                                        {"pubkey": to, "signer": False}]}},
            "meta": {"err": None, "fee": 5000, "preBalances": [100 * L, 0],
                     "postBalances": [100 * L - int(sol * L) - 5000, int(sol * L)],
                     "preTokenBalances": [], "postTokenBalances": []}}


class Hel:
    def __init__(self, txs, hubs=()):
        self.txs, self.hubs, self.credits, self.n = txs, set(hubs), 0, 0
    def latest_signature(self, a):
        return f"sig-{a}-{self.n}"
    def transactions(self, a, order="asc", full=True, limit=100, filters=None, max_txs=None):
        if not full:                                   # hub check
            return iter([{}] * (2000 if a in self.hubs else 3))
        gt = (filters or {}).get("slot", {}).get("gt", 0)
        return iter([t for t in self.txs.get(a, []) if t["slot"] > gt])


def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WATCH_SOURCES", [SRC])
    monkeypatch.setattr(dexscreener, "best_pairs", lambda m: {})
    return db.connect(str(tmp_path / "t.db"))


def test_transfers_ignore_swaps():
    assert sources.transfers_out(send(SRC, "KID", 3, NOW), SRC, 0.5) == [("KID", 3.0)]
    assert sources.transfers_out(swap(SRC, -3, 100, NOW), SRC, 0.5) == []
    assert sources.transfers_out(send(SRC, "KID", 0.1, NOW), SRC, 0.5) == []


def test_funding_flow(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    msgs = []
    note = lambda t, log=None: msgs.append(t)
    hel = Hel({SRC: [send(SRC, "OLD", 5, NOW - 3 * 86400), send(SRC, "CEX", 9, NOW - 86400)]},
              hubs={"CEX"})
    sources.check_funding(hel, con, note, log=lambda *a: None, now=NOW)
    assert sources.active(con, NOW) == ["OLD"]                 # exchange skipped
    assert len(msgs) == 1 and "👀" in msgs[0]                  # first scan: one summary
    # Later: source funds a new wallet, which funds a 2nd-hop wallet.
    hel.txs[SRC].append(send(SRC, "NEW", 4, NOW + 600))
    hel.txs["NEW"] = [send("NEW", "HOP2", 2, NOW + 700)]
    hel.n += 1
    msgs.clear()
    sources.check_funding(hel, con, note, log=lambda *a: None, now=NOW + 900)
    assert any("💸" in m and "NEW"[:4] in m for m in msgs)
    sources.check_funding(hel, con, note, log=lambda *a: None, now=NOW + 1000)
    assert set(sources.active(con, NOW + 1000)) == {"OLD", "NEW", "HOP2"}
    assert sum("💸" in m for m in msgs) == 2                    # NEW, then HOP2 via NEW
    # Tracked 14 days only
    assert sources.active(con, NOW + 20 * 86400) == []


def test_buy_alerts(tmp_path, monkeypatch):
    con = setup(tmp_path, monkeypatch)
    con.executescript(sources.SCHEMA)
    con.execute("INSERT INTO source_wallets (address, root, depth, funded_ts, funded_sol, skip) "
                "VALUES ('KID', ?, 1, ?, 5, 0)", (SRC, NOW - 3600))
    old = dict(sig="a", wallet="KID", mint="OLDMINT", side="buy", token_amount=1,
               quote_sol=-2, quote_usd=None, ts=NOW - 5 * 3600, slot=1)
    new = dict(old, sig="b", mint="NEWMINT", ts=NOW - 60)
    from types import SimpleNamespace as NS
    db.insert_trades(con, [NS(**old), NS(**new)])
    msgs = sources.check_buys(con, lambda t, log=None: None, log=lambda *a: None, now=NOW)
    assert len(msgs) == 1 and "🎯" in msgs[0] and "NEWMINT"[:8] in msgs[0]
    assert sources.check_buys(con, lambda t, log=None: None, log=lambda *a: None, now=NOW) == []


def test_follow_finds_active_frontier(monkeypatch, tmp_path):
    import time
    import smartmoney.helius as hm
    t = int(time.time())
    R = "ROOT111111111111111111111111111111111111111"
    h = Hel({R: [send(R, "A1", 10, t - 90 * 86400)],
             "A1": [send("A1", "B1", 5, t - 60 * 86400), send("A1", "CEX", 2, t - 40 * 86400)],
             "B1": [send("B1", "C1", 2, t - 2 * 86400)],
             "C1": [swap("C1", -1.5, 5, t - 3600)]}, hubs={"CEX"})
    monkeypatch.setattr(hm, "Helius", lambda: h)
    monkeypatch.setattr(dexscreener, "best_pairs", lambda m: {})
    nodes, info = sources.follow(roots=[R], log=lambda *a: None, csv_path=str(tmp_path / "f.csv"))
    assert [n["addr"] for n in nodes] == [R, "A1", "B1", "C1"]          # CEX not followed
    assert [n["hop"] for n in nodes] == [0, 1, 2, 3]
    assert info["C1"]["buys_recent"] and info["B1"]["kids"] == 1


def test_prune_and_reset(tmp_path, monkeypatch):
    from smartmoney import tracker
    con = setup(tmp_path, monkeypatch)
    con.executescript(sources.SCHEMA)
    con.execute("INSERT INTO source_wallets (address, root, depth) VALUES ('OLDSRC','OLDSRC',0)")
    con.execute("INSERT INTO source_wallets (address, root, depth, funded_ts) VALUES ('OLDKID','OLDSRC',1,?)", (NOW,))
    con.execute("INSERT INTO source_buys VALUES ('OLDKID','M',1,1,1)")
    con.execute("CREATE TABLE IF NOT EXISTS alerts (mint TEXT PRIMARY KEY, sent_at INTEGER)")
    con.execute("INSERT INTO alerts VALUES ('M', 1)")
    con.commit()
    assert tracker.reset_if_watch_changed(con, log=lambda *a: None)
    assert con.execute("SELECT COUNT(*) FROM source_wallets").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM source_buys").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert not tracker.reset_if_watch_changed(con, log=lambda *a: None)   # only once
