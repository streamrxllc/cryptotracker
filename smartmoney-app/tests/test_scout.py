import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import scout


def pos(w, m, pct, status="closed", first=0, hold=600):
    return {"wallet": w, "mint": m, "pnl_pct": pct, "pnl_sol": pct / 100, "status": status,
            "first_buy": first, "hold_min": hold if status == "closed" else None}


def test_score_counts_dead_bags_as_losses():
    rows = [pos("A", f"M{i}", 50) for i in range(9)] + [pos("A", "D", -80, status="open")]
    rows += [pos("B", f"M{i}", 50) for i in range(10)]
    rows += [pos("C", "X", 50)]                                   # too few trades
    s = {x["wallet"]: x for x in scout.score(rows, 5, 40, 0.9)}
    assert s["A"]["trades"] == 10 and s["A"]["win_rate"] == 0.9 and s["A"]["pass"]
    assert s["B"]["win_rate"] == 1.0 and "C" not in s
    rows.append(pos("A", "E", -10))                               # 9/11 -> fails
    assert not {x["wallet"]: x for x in scout.score(rows, 5, 40, 0.9)}["A"]["pass"]


def test_groups_need_repeated_co_buys():
    rows = [pos("A", "T1", 50, first=1000), pos("B", "T1", 50, first=1060),
            pos("A", "T2", 50, first=5000), pos("B", "T2", 50, first=5030),
            pos("C", "T1", 50, first=1100),                       # only one co-buy
            pos("D", "T2", 50, first=9000)]                       # too late
    grp, shared = scout.groups(rows, {"A", "B", "C", "D"})
    assert grp == [["A", "B"]] and shared[("A", "B")] == {"T1", "T2"}
