import os, sys, types
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, gecko, winners, discover

D = 86400


def candles(prices_low_high):
    return [(1_000_000 + i * D, 0, h, l, 0, 0) for i, (l, h) in enumerate(prices_low_high)]


def test_measure_run():
    c = candles([(0.001, 0.002), (0.0008, 0.0015), (0.002, 0.004), (0.01, 0.02), (0.005, 0.01)])
    mult, low, peak, ts = winners.measure_run(c, early_days=3)
    assert low == 0.0008 and peak == 0.02 and round(mult) == 25


def test_peak_must_come_after_low():
    c = candles([(0.01, 0.05), (0.001, 0.002), (0.001, 0.003)])
    mult, low, peak, _ = winners.measure_run(c, early_days=3)
    assert low == 0.001 and peak == 0.003


def test_select_spreads_weeks():
    cfg = types.SimpleNamespace(MAX_WINNERS=3, MAX_WINNERS_PER_WEEK=2)
    cands = [{"run": r, "created": 0} for r in (50, 40, 30)] + [{"run": 5, "created": 30 * D}]
    out = winners.select(cands, cfg)
    assert [c["run"] for c in out] == [50, 40, 5]


def test_pool_rows():
    payload = {"data": [{"id": "solana_POOL", "attributes": {
        "address": "POOL", "name": "MEME / SOL", "pool_created_at": "2026-08-01T00:00:00Z",
        "base_token_price_usd": "0.01", "fdv_usd": "5000000", "market_cap_usd": None,
        "reserve_in_usd": "100000"},
        "relationships": {"base_token": {"data": {"id": "solana_MINT"}},
                          "quote_token": {"data": {"id": f"solana_{config.WSOL}"}}}}]}
    r = gecko.pool_rows(payload)[0]
    assert r["base"] == "MINT" and r["quote"] == config.WSOL and r["fdv"] == 5e6
    assert r["created"] == 1785542400


def test_winners_run_writes_file(tmp_path, monkeypatch):
    now = 1785542400 + 60 * D
    pool = {"pool": "P1", "name": "MEME / SOL", "base": "MINT1", "quote": config.WSOL,
            "created": 1785542400, "price_usd": 0.01, "fdv": 4e6, "mcap": None, "liquidity": 1e5}
    old = dict(pool, base="OLD", pool="P2", created=now - 400 * D)
    monkeypatch.setattr(gecko, "top_pools", lambda **k: [pool, old])
    monkeypatch.setattr(gecko, "trending_pools", lambda **k: [])
    monkeypatch.setattr(gecko, "daily_ohlcv", lambda p: candles([(0.0005, 0.001), (0.002, 0.05), (0.01, 0.02)]))
    out_file = tmp_path / "auto.txt"
    monkeypatch.setattr(config, "WINNERS_AUTO_FILE", str(out_file))
    monkeypatch.setattr(config, "WINNERS_FILE", str(tmp_path / "none.txt"))
    chosen = winners.run(log=lambda *a: None, now=now)
    assert [c["mint"] for c in chosen] == ["MINT1"]
    assert discover.read_winners() == {"MINT1": 0.05}
