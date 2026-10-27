from smartmoney.trace import group_sources


def test_merges_same_ancestor_across_depths():
    chains = {"a": ["X"], "b": ["Y", "X"], "c": ["Z", "Q", "X"], "d": ["Y", "X"]}
    profits = {"a": 1, "b": 2, "c": 3, "d": 4}
    coins = {"a": {"m1"}, "b": {"m2"}, "c": {"m3"}, "d": {"m2"}}
    src = group_sources(chains, profits, coins)
    top = src[0]
    assert top["source"] == "X" and top["depth"] == 1
    assert top["wallets"] == ["a", "b", "c", "d"] and len(top["coins"]) == 3
    # Y covers b,d (a different set) -> kept; no duplicate sets
    assert {s["source"] for s in src} == {"X", "Y"}
