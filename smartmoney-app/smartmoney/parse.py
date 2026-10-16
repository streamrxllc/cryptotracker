"""Turn a raw Solana transaction into buys/sells for a given wallet.

Works from balance changes (pre/post SOL and token balances), so it does not
care which DEX, aggregator or launchpad was used.
"""
from dataclasses import dataclass, asdict

from . import config

LAMPORTS = 1_000_000_000


@dataclass
class Trade:
    sig: str
    wallet: str
    mint: str
    side: str            # "buy" | "sell" | "transfer_in" | "transfer_out"
    token_amount: float  # always positive
    quote_sol: float     # SOL (incl. wSOL): spent on a buy (+), received on a sell (-)
    quote_usd: float     # USDC/USDT spent (+) or received (-)
    ts: int
    slot: int

    def as_dict(self):
        return asdict(self)

    def price_sol(self, sol_usd=None):
        """Price paid per token in SOL, or None if it can't be computed."""
        if self.token_amount <= 0:
            return None
        spent = abs(self.quote_sol)
        if spent == 0 and self.quote_usd and sol_usd:
            spent = abs(self.quote_usd) / sol_usd
        if spent == 0:
            return None
        return spent / self.token_amount

    def sol_value(self, sol_usd=None):
        v = abs(self.quote_sol)
        if self.quote_usd and sol_usd:
            v += abs(self.quote_usd) / sol_usd
        return v


def account_keys(tx):
    """Full ordered account list, including address-lookup-table accounts."""
    msg = tx["transaction"]["message"]
    keys = msg.get("accountKeys", [])
    out = []
    for k in keys:
        out.append(k["pubkey"] if isinstance(k, dict) else k)
    # jsonParsed already includes looked-up accounts; plain json does not.
    if keys and not isinstance(keys[0], dict):
        loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
        out += loaded.get("writable", []) + loaded.get("readonly", [])
    return out


def signers(tx):
    msg = tx["transaction"]["message"]
    keys = msg.get("accountKeys", [])
    if keys and isinstance(keys[0], dict):
        return [k["pubkey"] for k in keys if k.get("signer")]
    n = (msg.get("header") or {}).get("numRequiredSignatures", 1)
    return [k for k in keys[:n]]


def signature(tx):
    return tx["transaction"]["signatures"][0]


def _ui(entry):
    ui = entry.get("uiTokenAmount") or {}
    s = ui.get("uiAmountString")
    if s not in (None, ""):
        return float(s)
    if ui.get("uiAmount") is not None:
        return float(ui["uiAmount"])
    amt, dec = ui.get("amount"), ui.get("decimals", 0)
    return int(amt) / (10 ** dec) if amt is not None else 0.0


def owner_deltas(tx, owner):
    """Return (sol_delta, {mint: token_delta}) for one wallet.

    sol_delta is native SOL change with the network fee added back, so paying
    a fee is not mistaken for buying something.
    """
    meta = tx.get("meta") or {}
    keys = account_keys(tx)
    sol_delta = 0.0
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    for i, k in enumerate(keys):
        if k == owner and i < len(pre) and i < len(post):
            sol_delta += (post[i] - pre[i]) / LAMPORTS
            if i == 0:
                sol_delta += (meta.get("fee") or 0) / LAMPORTS

    tokens = {}
    for sign, entries in ((-1, meta.get("preTokenBalances") or []),
                          (1, meta.get("postTokenBalances") or [])):
        for e in entries:
            if e.get("owner") != owner:
                continue
            tokens[e["mint"]] = tokens.get(e["mint"], 0.0) + sign * _ui(e)
    return sol_delta, {m: d for m, d in tokens.items() if abs(d) > 1e-12}


def extract_trades(tx, owner, dust_sol=0.0005):
    """Buys/sells of non-quote tokens by `owner` in this transaction."""
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    sol_delta, tok = owner_deltas(tx, owner)

    # wSOL and native SOL are the same money; ignore tiny rent movements.
    sol = sol_delta + sum(d for m, d in tok.items() if m in config.SOL_QUOTES)
    usd = sum(d for m, d in tok.items() if m in config.USD_QUOTES)
    if abs(sol) < dust_sol:
        sol = 0.0
    assets = {m: d for m, d in tok.items() if m not in config.QUOTE_MINTS}

    trades = []
    for mint, d in assets.items():
        others_down = any(x < 0 for m, x in assets.items() if m != mint)
        others_up = any(x > 0 for m, x in assets.items() if m != mint)
        if d > 0:
            paid = sol < 0 or usd < 0 or others_down
            side = "buy" if paid else "transfer_in"
        else:
            got = sol > 0 or usd > 0 or others_up
            side = "sell" if got else "transfer_out"
        # Sign convention: quote_* positive = money spent on a buy,
        # negative = money received on a sell.
        trades.append(Trade(
            sig=signature(tx), wallet=owner, mint=mint, side=side,
            token_amount=abs(d),
            quote_sol=-sol if side in ("buy", "sell") else 0.0,
            quote_usd=-usd if side in ("buy", "sell") else 0.0,
            ts=int(tx.get("blockTime") or 0), slot=int(tx.get("slot") or 0),
        ))
    # With several assets in one tx, split the money between them evenly
    # rather than counting it twice.
    paid_like = [t for t in trades if t.side in ("buy", "sell")]
    if len(paid_like) > 1:
        for t in paid_like:
            t.quote_sol /= len(paid_like)
            t.quote_usd /= len(paid_like)
    return trades


def trader_trade(tx, mint):
    """For discovery: the signer who bought/sold `mint` in this tx, if any."""
    for s in signers(tx):
        for t in extract_trades(tx, s):
            if t.mint == mint and t.side in ("buy", "sell"):
                return t
    return None
