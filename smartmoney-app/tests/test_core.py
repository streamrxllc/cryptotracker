"""Offline tests: parsing, early-buyer scan, vetting helpers, signals."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from smartmoney import config, discover, signals, vet  # noqa: E402
from smartmoney.parse import extract_trades, trader_trade  # noqa: E402

L = 1_000_000_000
MINT = "Meme111111111111111111111111111111111111111"
POOL = "Pool111111111111111111111111111111111111111"
_n = [0]


def tb(idx, mint, owner, ui):
    return {"accountIndex": idx, "mint": mint, "owner": owner,
            "uiTokenAmount": {"uiAmountString": str(ui), "decimals": 6}}


def swap(trader, sol_change, tok_change, ts, mint=MINT, fee=5000, wsol=False):
    """A trader swaps SOL<->token against a pool. sol_change from trader's view."""
    _n[0] += 1
    keys = [{"pubkey": trader, "signer": True}, {"pubkey": POOL, "signer": False}]
    pre_sol = [10 * L, 1000 * L]
    sol_l = int(sol_change * L)
    post_sol = [pre_sol[0] + (0 if wsol else sol_l) - fee, pre_sol[1] - (0 if wsol else sol_l)]
    pre_t = [tb(2, mint, trader, 100), tb(3, mint, POOL, 1_000_000)]
    post_t = [tb(2, mint, trader, 100 + tok_change), tb(3, mint, POOL, 1_000_000 - tok_change)]
    if wsol:
        pre_t += [tb(4, config.WSOL, trader, 5)]
        post_t += [tb(4, config.WSOL, trader, 5 + sol_change)]
    return {"slot": ts, "blockTime": ts,
            "transaction": {"signatures": [f"sig{_n[0]}"], "message": {"accountKeys": keys}},
            "meta": {"err": None, "fee": fee, "preBalances": pre_sol, "postBalances": post_sol,
                     "preTokenBalances": pre_t, "postTokenBalances": post_t}}


def test_buy_and_sell_detected():
    t = extract_trades(swap("A", -1.0, 1000, 100), "A")
    assert len(t) == 1 and t[0].side == "buy"
    assert abs(t[0].quote_sol - 1.0) < 1e-9 and t[0].token_amount == 1000
    assert abs(t[0].price_sol() - 0.001) < 1e-12
    s = extract_trades(swap("A", 2.0, -500, 101), "A")[0]
    assert s.side == "sell" and abs(s.quote_sol + 2.0) < 1e-9


def test_wsol_counts_as_sol_and_fee_ignored():
    t = extract_trades(swap("A", -0.5, 10, 100, wsol=True), "A")[0]
    assert t.side == "buy" and abs(t.quote_sol - 0.5) < 1e-9


def test_failed_tx_ignored():
    tx = swap("A", -1, 10, 100)
    tx["meta"]["err"] = {"InstructionError": [0, "Custom"]}
    assert extract_trades(tx, "A") == []


def test_trader_trade_finds_signer():
    t = trader_trade(swap("B", -1, 10, 100), MINT)
    assert t.wallet == "B" and t.side == "buy"


def test_scan_token_early_buyers():
    cfg = types.SimpleNamespace(MIN_ENTRY_MULTIPLE=5, SNIPE_SECONDS=60, MIN_POSITION_SOL=0.1,
                                MAX_SCAN_HOURS=96, MAX_SCAN_TXS=10_000, EXPENSIVE_RUN_STOP=50)
    ref = 0.01  # today's price, SOL per token; cheap = <= 0.002
    txs = [swap("DEV", -5, 5000, 1000)]                        # creator
    txs += [swap("SNIPER", -2, 2000, 1005)]                    # inside snipe window
    txs += [swap("EARLY", -1, 1000, 2000), swap("EARLY", -1, 800, 3000)]   # 0.001, 0.00125
    txs += [swap("DUST", -0.05, 100, 2500)]                    # too small
    txs += [swap(f"LATE{i}", -1, 50, 10_000 + i) for i in range(200)]     # 0.02 > cutoff
    txs += [swap("NEVER", -1, 1000, 99_999)]                   # after stop
    found, meta = discover.scan_token(iter(txs), MINT, ref, cfg=cfg)
    assert set(found) == {"EARLY"}
    assert found["EARLY"]["buys"] == 2 and round(found["EARLY"]["avg_multiple"], 2) == 9.0
    assert meta["stop"] == "price left cheap zone" and meta["creator"] == "DEV"


def test_vet_helpers():
    sigs = [{"blockTime": 1000 + i * 10} for i in range(100)]   # 100 tx in ~1000 s
    assert vet.tx_rate(sigs) > 1000
    fund = swap("NEW", 3.0, 0, 50)   # wallet receives 3 SOL from POOL-like funder
    fund["meta"]["preTokenBalances"] = fund["meta"]["postTokenBalances"] = []
    assert vet.find_funder([fund], "NEW") == POOL
    c = vet.assign_clusters({"a": "F", "b": "F", "c": "G", "d": "EX", "e": "EX", "f": "EX"}, 2)
    assert c == {"a": "F", "b": "F"}


def row(w, mint, side, sol, ts):
    return {"wallet": w, "mint": mint, "side": side, "quote_sol": sol if side == "buy" else -sol,
            "quote_usd": 0, "ts": ts}


def test_signals_consensus():
    cfg = types.SimpleNamespace(MIN_BUY_SOL=0.05, MIN_WALLETS_FOR_SIGNAL=3)
    tr = [row("a", "X", "buy", 1, 1), row("a", "X", "buy", 1, 5),
          row("b", "X", "buy", 2, 2), row("c", "X", "buy", 0.5, 3),
          row("d", "X", "buy", 1, 4), row("d", "X", "sell", 3, 6),     # d exited
          row("a", "Y", "buy", 1, 1), row("b", "Y", "buy", 1, 1),     # only 2 wallets
          row("e", "X", "buy", 0.01, 1)]                               # dust
    out = signals.compute(tr, {"a": 3, "b": 2}, cfg=cfg)
    assert [s["mint"] for s in out] == ["X"]
    s = out[0]
    assert s["wallets"] == 3 and s["sellers"] == 1 and s["accumulators"] == 1
    assert s["net_sol"] == 4.5 and s["score"] == 3 + 2 + 1 - 0.5
