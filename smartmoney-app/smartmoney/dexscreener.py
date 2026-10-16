"""Dexscreener public API (free, no key) for prices and token info."""
import time

import requests

from . import config

BASE = "https://api.dexscreener.com"
_session = requests.Session()


def _get(path):
    for attempt in range(4):
        try:
            r = _session.get(BASE + path, timeout=30)
            if r.status_code == 429:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return []


def best_pairs(mints):
    """{mint: best (most liquid) pair dict} for up to many mints."""
    mints = list(dict.fromkeys(mints))
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        pairs = _get(f"/tokens/v1/solana/{','.join(chunk)}") or []
        for p in pairs:
            m = (p.get("baseToken") or {}).get("address")
            if m not in chunk:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            cur = out.get(m)
            if cur is None or liq > ((cur.get("liquidity") or {}).get("usd") or 0):
                out[m] = p
    return out


def sol_usd():
    p = best_pairs([config.WSOL]).get(config.WSOL)
    return float(p["priceUsd"]) if p and p.get("priceUsd") else None


def price_in_sol(pair, sol_usd_price):
    """Current token price expressed in SOL."""
    quote = (pair.get("quoteToken") or {}).get("address")
    if quote in config.SOL_QUOTES and pair.get("priceNative"):
        return float(pair["priceNative"])
    if pair.get("priceUsd") and sol_usd_price:
        return float(pair["priceUsd"]) / sol_usd_price
    return None


def summarize(pair):
    if not pair:
        return {}
    return {
        "symbol": (pair.get("baseToken") or {}).get("symbol"),
        "name": (pair.get("baseToken") or {}).get("name"),
        "price_usd": float(pair["priceUsd"]) if pair.get("priceUsd") else None,
        "mcap": pair.get("marketCap") or pair.get("fdv"),
        "liquidity": (pair.get("liquidity") or {}).get("usd"),
        "change_h24": (pair.get("priceChange") or {}).get("h24"),
        "pair_created": (pair.get("pairCreatedAt") or 0) // 1000,
        "url": pair.get("url"),
    }
