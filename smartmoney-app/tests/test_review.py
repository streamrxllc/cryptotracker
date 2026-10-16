import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import review


def t(w, m, side, sol, amt, ts):
    return {"wallet": w, "mint": m, "side": side, "quote_sol": sol if side == "buy" else -sol,
            "token_amount": amt, "ts": ts}


def test_positions_and_overlap():
    trades = [
        t("A", "X", "buy", 1.0, 1000, 100), t("A", "X", "sell", 3.0, 1000, 4000),   # closed +200%
        t("B", "X", "buy", 2.0, 1000, 200),                                         # open, price 0.001 -> 1 SOL
        t("B", "Y", "sell", 5.0, 10, 50),                                           # pre-window sell, ignored
        t("C", "Z", "buy", 1.0, 100, 300), t("C", "Z", "sell", 0.2, 50, 400),       # half sold, rest worthless
    ]
    rows = {(r["wallet"], r["mint"]): r for r in review.positions(trades, {"X": 0.001, "Z": None})}
    a, b, c = rows[("A", "X")], rows[("B", "X")], rows[("C", "Z")]
    assert a["status"] == "closed" and round(a["pnl_pct"]) == 200 and a["hold_min"] == 65
    assert b["status"] == "open" and round(b["pnl_pct"]) == -50
    assert ("B", "Y") not in rows
    assert c["status"] == "open (no price)" and round(c["pnl_pct"]) == -80
    ov = review.overlaps(list(rows.values()))
    assert [m for m, _ in ov] == ["X"] and [r["wallet"] for r in ov[0][1]] == ["A", "B"]
