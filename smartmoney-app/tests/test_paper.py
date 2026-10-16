import json, os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, paper

CFG = types.SimpleNamespace(PAPER_STOP_LOSS_PCT=40, PAPER_TRAIL_ARM_X=2.0, PAPER_TRAIL_PCT=30,
                            PAPER_MAX_HOLD_HOURS=168, PAPER_MIN_LIQUIDITY_USD=20000)


def trade(entry=1.0, peak=1.0, opened=0, wallets=("a", "b", "c", "d")):
    return {"entry_price": entry, "peak_price": peak, "opened_at": opened,
            "wallets": json.dumps(list(wallets))}


def test_exit_rules():
    assert paper.decide_exit(trade(), 0.59, 0, 10, CFG) == "stop loss"
    assert paper.decide_exit(trade(), 0.7, 0, 10, CFG) is None
    assert paper.decide_exit(trade(peak=3.0), 2.0, 0, 10, CFG) == "trailing stop"   # 3 -> 2 = -33%
    assert paper.decide_exit(trade(peak=3.0), 2.5, 0, 10, CFG) is None
    assert paper.decide_exit(trade(peak=1.5), 1.0, 0, 10, CFG) is None              # trail not armed
    assert paper.decide_exit(trade(), 1.2, 2, 10, CFG) == "smart money sold"        # 2 of 4
    assert paper.decide_exit(trade(), 1.2, 1, 10, CFG) is None
    assert paper.decide_exit(trade(), 1.2, 0, 168 * 3600, CFG) == "time stop"


def test_entry_rules():
    s = {"mint": "X", "price_usd": 1.0, "liquidity": 50000}
    assert paper.should_open(s, set(), set(), CFG) is None
    assert paper.should_open(s, {"X"}, set(), CFG) == "already open"
    assert paper.should_open(s, set(), {"X"}, CFG) == "traded recently"
    assert paper.should_open({**s, "late": True}, set(), set(), CFG) == "already ran"
    assert paper.should_open({**s, "liquidity": 5000}, set(), set(), CFG) == "liquidity too low"


def test_open_then_close(tmp_path):
    con = db.connect(str(tmp_path / "p.db"))
    sig = {"mint": "X", "symbol": "XX", "wallets": 3, "score": 5, "buyer_list": ["a", "b", "c"]}
    ev = paper.update(con, [sig], now=1000, log=lambda *a: None,
                      prices={"X": {"price_usd": 1.0, "liquidity": 50000}})
    assert ev and ev[0].startswith("PAPER BUY XX")
    # Same signal again: no second position.
    assert paper.update(con, [sig], now=2000, log=lambda *a: None,
                        prices={"X": {"price_usd": 1.5, "liquidity": 50000}}) == []
    # Two of the three entry wallets sell -> exit.
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    [(f"s{w}", w, "X", "sell", 1, -1, 0, 3000, 1) for w in "ab"])
    ev = paper.update(con, [], now=3500, log=lambda *a: None,
                      prices={"X": {"price_usd": 2.0, "liquidity": 50000}})
    assert ev and "smart money sold" in ev[0]
    s = paper.summary(con)
    slip = config.PAPER_SLIPPAGE_PCT / 100
    expected = (2.0 * (1 - slip)) / (1.0 * (1 + slip)) - 1
    assert len(s["closed"]) == 1 and abs(s["closed"][0]["pnl_pct"] / 100 - expected) < 1e-9
    assert s["win_rate"] == 100
