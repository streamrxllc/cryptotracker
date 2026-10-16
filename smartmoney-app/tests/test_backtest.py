import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import backtest


def t(side, sol, amt, ts, w="A", m="X"):
    return {"wallet": w, "mint": m, "side": side, "quote_sol": sol if side == "buy" else -sol,
            "token_amount": amt, "ts": ts}


def test_enter_at_nth_buy_and_exit_with_wallet():
    trades = [t("buy", 1, 1000, 1), t("buy", 1, 500, 2), t("buy", 1, 250, 3),   # prices .001 .002 .004
              t("sell", 8, 875, 4)]                                              # sells half at .00914
    r = {n: x[2] for n, x in ((n, backtest.copy_returns(trades, n, {"X": 0.004}, slip=0)[0])
                              for n in (1, 3))}
    # The wallet sold half its final position at 8/875; the rest is valued at 0.004.
    exit_value = 0.5 * (8 / 875) + 0.5 * 0.004
    assert abs(r[3] / 100 - (exit_value / 0.004 - 1)) < 1e-9
    assert abs(r[1] / 100 - (exit_value / 0.001 - 1)) < 1e-9
    assert backtest.copy_returns(trades, 4, {}, slip=0) == []


def test_timing():
    trades = [t("buy", 1, 1000, 0), t("buy", 1, 500, 600), t("buy", 1, 250, 1200),
              t("sell", 8, 875, 4800)]
    gaps, to_sell, run_up = backtest.timing(trades, 3)
    assert gaps == [20.0] and to_sell == [60.0] and run_up == [300.0]
