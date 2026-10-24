import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, dexscreener, gecko, patternbt
from tests.test_core import swap

H = 3600
MINT = "COIN111"
SUPPLY = 1_000_000_000      # pump-style supply


def coin_history(accumulators=3, t0=1_000_000):
    """Launch, patient wallets adding over hours, then price climbs to $300k mcap."""
    txs = [swap("DEV", -5, 50_000_000, t0, mint=MINT)]
    # price per token (SOL) = sol / tokens; at SOL=$100, mcap = p * 1e9 * 100
    for k in range(accumulators):
        for j in range(4):                         # 4 buys each, spread over 6h
            txs.append(swap(f"ACC{k}", -1, 1_000_000, t0 + 2 * H + j * 2 * H + k, mint=MINT))
    for i in range(10):                            # one-shot flippers
        txs.append(swap(f"FLIP{i}", -0.5, 500_000, t0 + 3 * H + i * 60, mint=MINT))
        txs.append(swap(f"FLIP{i}", 0.6, -500_000, t0 + 3 * H + i * 60 + 30, mint=MINT))
    for i in range(8):                             # price reaches 3e-6 SOL -> $300k
        txs.append(swap(f"LATE{i}", -3, 1_000_000, t0 + 12 * H + i * 60, mint=MINT))
    txs.append(swap("NEVER", -1, 1, t0 + 99 * H, mint=MINT))
    return txs


def test_scan_and_features():
    cp = patternbt.scan_to_checkpoint(iter(coin_history()), MINT, SUPPLY, 100.0, 300_000)
    assert cp and cp["creator"] == "DEV" and abs(cp["checkpoint_mcap"] - 300_000) < 1
    assert cp["checkpoint_ts"] == 1_000_000 + 12 * H + 2 * 60      # median of last 5 crosses on the 3rd
    f = patternbt.features(cp["trades"], cp["checkpoint_ts"], cp["launch_ts"], cp["creator"])
    assert f["n_accum"] == 3 and f["accum_sol"] == 12
    assert 0 < f["accum_share"] < 1 and f["unique_buyers"] >= 13
    assert patternbt.scan_to_checkpoint(iter(coin_history()[:20]), MINT, SUPPLY, 100.0,
                                        300_000) is None


def test_outcome():
    D = 86400
    candles = [(0, 0, 0, 0, 1e-4, 0), (D, 0, 0, 0, 3e-3, 0), (2 * D, 0, 0, 0, 1e-3, 0)]
    assert round(patternbt.outcome(candles, D, SUPPLY, 300_000), 1) == 10.0


def test_run_compares_runners_and_duds(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "p.db"))
    con = db.connect()
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    [(f"s{i}", "W", f"DUD{i}", "buy", 1, 1, 0, 1, 1) for i in range(3)])
    con.commit()
    monkeypatch.setattr(patternbt, "read_winners", lambda: {"WIN1": None, "WIN2": None})

    class FakeHel:
        credits = 0
        def __init__(self, *a, **k): pass
        def rpc(self, m, p, credits=1): return {"value": {"uiAmountString": str(SUPPLY)}}
        def transactions(self, mint, **k):
            hist = coin_history(accumulators=3 if mint.startswith("WIN") else 0)
            for tx in hist:
                for b in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]:
                    b["mint"] = mint
            return iter(hist)
    monkeypatch.setattr(patternbt, "Helius", FakeHel)
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(gecko, "token_best_pool", lambda m: {"pool": "P" + m, "name": m})
    monkeypatch.setattr(gecko, "daily_ohlcv", lambda p: [
        (1_000_000, 0, 0, 0, 3e-3 if "WIN" in p else 2e-4, 0)])
    out = []
    patternbt.run(controls=3, log=out.append, csv_path=str(tmp_path / "p.csv"))
    text = "\n".join(out)
    assert "5 coins scored: 2 runners" in text and "3 duds" in text
    assert "1+ patient accumulators: catches 2/2 runners, 0/3 duds → 100%" in text
