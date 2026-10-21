import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from smartmoney import config, db, dexscreener, holders
from smartmoney.helius import HeliusError

H = 3600


def tr(mint, side, sol, amt, ts):
    return {"mint": mint, "side": side, "quote_sol": sol if side == "buy" else -sol,
            "token_amount": amt, "ts": ts}


def test_position_story_and_conviction():
    launch = 1_000_000
    t = [tr("N", "buy", 1, 1000, launch + 5 * H), tr("N", "buy", 1, 800, launch + 20 * H),
         tr("N", "buy", 1, 700, launch + 40 * H), tr("N", "sell", 2, 500, launch + 60 * H),
         tr("X", "buy", 9, 9, launch)]
    s = holders.position_story(t, "N", launch, price_now_sol=0.004, holding=2000)
    assert s["buys"] == 3 and s["span_h"] == 35 and not s["launch_buyer"]
    assert abs(s["avg_price_sol"] - 3 / 2500) < 1e-12 and round(s["multiple"], 2) == 3.33
    assert s["sold_share"] == 0.2 and s["bought_share"] == 1.0 and s["value_sol"] == 8.0
    assert holders.is_conviction(s)
    # Insider: got most of it by transfer.
    t2 = t + [{"mint": "N", "side": "transfer_in", "quote_sol": 0, "token_amount": 50000,
               "ts": launch}]
    assert not holders.is_conviction(holders.position_story(t2, "N", launch, 0.004, 52000))
    # Sniper: first buy in the launch minute.
    t3 = [tr("N", "buy", 1, 1000, launch + 10)] + t[1:3]
    assert holders.position_story(t3, "N", launch, 0.004, 2500)["launch_buyer"]


class FakeHel:
    credits = 0

    def __init__(self, *a, **k):
        self.calls = []

    def rpc(self, method, params, credits=1):
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "TA_POOL", "uiAmountString": "900000"},
                              {"address": "TA_ALICE", "uiAmountString": "2500"},
                              {"address": "TA_BOB", "uiAmountString": "1000"}]}
        addrs = params[0]
        if params[1]["encoding"] == "jsonParsed":
            own = {"TA_POOL": "POOLPDA", "TA_ALICE": "ALICE", "TA_BOB": "BOB"}
            return {"value": [{"data": {"parsed": {"info": {"owner": own[a]}}}} for a in addrs]}
        return {"value": [None if a == "POOLPDA" else {"owner": holders.SYSTEM_PROGRAM}
                          for a in addrs]}

    def transactions(self, address, full=True, filters=None, **k):
        from tests.test_core import swap
        if not full:
            return iter([{"blockTime": 1}] * 5)
        if filters and "tokenTransfer" in filters:
            raise HeliusError("filter not supported")       # exercise the fallback
        launch = 1_000_000
        if address == "ALICE":
            return iter([swap("ALICE", -1, 1000, launch + 5 * H, mint="NORM"),
                         swap("ALICE", -1, 800, launch + 20 * H, mint="NORM"),
                         swap("ALICE", -1, 700, launch + 40 * H, mint="NORM")])
        return iter([swap("BOB", -3, 1000, launch + 5, mint="NORM")])


def test_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "h.db"))
    monkeypatch.setattr(holders, "Helius", FakeHel)
    monkeypatch.setattr(holders, "read_winners", lambda: {"NORM": 2.0})
    monkeypatch.setattr(dexscreener, "sol_usd", lambda: 100.0)
    monkeypatch.setattr(dexscreener, "best_pairs", lambda ms: {m: {
        "baseToken": {"address": m, "symbol": "Normie"}, "quoteToken": {"address": config.WSOL},
        "priceNative": "0.004", "priceUsd": "0.4", "liquidity": {"usd": 90000},
        "pairCreatedAt": 1_000_000_000} for m in ms})
    out = []
    holders.run(log=out.append, csv_path=str(tmp_path / "h.csv"))
    text = "\n".join(out)
    assert "2 wallet holders, 1 built it with conviction" in text
    assert "ALICE   coins: 1" in text and "BOB   coins" not in text
    rows = open(tmp_path / "h.csv").read()
    assert "ALICE" in rows and "BOB" in rows and "POOLPDA" not in rows
