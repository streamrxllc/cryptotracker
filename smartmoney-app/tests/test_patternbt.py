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


def test_find_checkpoint():
    D = 86400
    launch = 10 * D
    c = lambda d, close: (launch + d * D, 0, 0, 0, close, 0)
    # day0 50k, day1 80k, day2 opens at day1 close (80k: too small), day3 opens at 400k
    cs = [c(0, 5e-5), c(1, 8e-5), c(2, 4e-4), c(3, 1e-3), c(4, 9e-3)]
    assert patternbt.find_checkpoint(cs, launch, SUPPLY, 2, 100_000, 1_500_000) == \
        (launch + 3 * D, 400_000)
    fast = [c(0, 5e-3), c(1, 6e-3), c(2, 7e-3), c(3, 8e-3)]           # $5M+ by day 2
    assert patternbt.find_checkpoint(fast, launch, SUPPLY, 2, 100_000, 1_500_000)[0] == "ran"
    assert patternbt.find_checkpoint(cs[:2], launch, SUPPLY, 2, 100_000, 1_500_000) is None


def test_run_compares_runners_and_duds(tmp_path, monkeypatch):
    D = 86400
    launch = 1_000_000 - 3 * D
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "p.db"))
    con = db.connect()
    con.executemany("INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?)",
                    [(f"s{i}", "W", f"DUD{i}", "buy", 1, 1, 0, 1, 1) for i in range(3)] +
                    [("x", "W", "TSLAx", "buy", 1, 1, 0, 1, 1)])
    con.commit()
    monkeypatch.setattr(patternbt, "read_winners", lambda: {"WIN1": None, "WIN2": None})

    class FakeHel:
        credits = 0
        def __init__(self, *a, **k): pass
        def rpc(self, m, p, credits=1): return {"value": {"uiAmountString": str(SUPPLY)}}
        def transactions(self, mint, **k):
            hist = coin_history(accumulators=3 if mint.startswith("WIN") else 0,
                                t0=1_000_000 - 40 * H)      # inside the 48h window
            for tx in hist:
                for b in tx["meta"]["preTokenBalances"] + tx["meta"]["postTokenBalances"]:
                    b["mint"] = mint
            return iter(hist[:-1])                          # drop the post-window trade
    monkeypatch.setattr(patternbt, "Helius", FakeHel)
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)

    def pool(m):
        quote = config.USDC if m == "TSLAx" else config.WSOL
        return {"pool": "P" + m, "name": m, "quote": quote, "created": launch}
    monkeypatch.setattr(gecko, "token_best_pool", pool)

    def candles(p):
        peak = 5e-3 if "WIN" in p else 3e-4
        return [(launch, 0, 0, 0, 1e-4, 0), (launch + D, 0, 0, 0, 2e-4, 0),
                (launch + 2 * D, 0, 0, 0, 3e-4, 0), (launch + 3 * D, 0, 0, 0, peak, 0)]
    monkeypatch.setattr(gecko, "daily_ohlcv", candles)
    import time as _t
    monkeypatch.setattr(_t, "time", lambda: 1_000_000 + D)
    out = []
    patternbt.run(controls=3, log=out.append, csv_path=str(tmp_path / "p.csv"))
    text = "\n".join(out)
    assert "5 coins scored: 2 runners" in text and "3 duds" in text, text
    assert "1 not a recent SOL-paired coin" in text
    assert "1+ patient accumulators: catches 2/2 runners, 0/3 duds" in text
