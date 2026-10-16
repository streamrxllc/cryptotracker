"""GeckoTerminal public API (free, no key): pool lists and daily price history.

Keyless limit is roughly 10-30 calls/minute, so calls are spaced out.
"""
import time
from datetime import datetime

import requests

BASE = "https://api.geckoterminal.com/api/v2"
_session = requests.Session()
_last = [0.0]
GAP_SECONDS = 6.5  # stay under ~10 calls/min


def _get(path, params=None):
    for attempt in range(5):
        wait = GAP_SECONDS - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = _session.get(BASE + path, params=params, timeout=30,
                             headers={"Accept": "application/json"})
        except requests.RequestException:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(30 * (attempt + 1))
            continue
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()
    return {}


def _mint(rel):
    """'solana_<mint>' -> '<mint>'."""
    tid = (((rel or {}).get("data")) or {}).get("id") or ""
    return tid.split("_", 1)[1] if "_" in tid else tid


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def pool_rows(payload):
    out = []
    for p in payload.get("data") or []:
        a, rel = p.get("attributes") or {}, p.get("relationships") or {}
        created = a.get("pool_created_at")
        try:
            created_ts = int(datetime.fromisoformat(created.replace("Z", "+00:00"))
                             .timestamp()) if created else None
        except ValueError:
            created_ts = None
        out.append({
            "pool": a.get("address") or p.get("id", "").split("_", 1)[-1],
            "name": a.get("name"),
            "base": _mint(rel.get("base_token")),
            "quote": _mint(rel.get("quote_token")),
            "created": created_ts,
            "price_usd": _num(a.get("base_token_price_usd")),
            "fdv": _num(a.get("fdv_usd")),
            "mcap": _num(a.get("market_cap_usd")),
            "liquidity": _num(a.get("reserve_in_usd")),
        })
    return out


def top_pools(pages=10, sort="h24_volume_usd_desc"):
    rows = []
    for page in range(1, pages + 1):
        data = _get("/networks/solana/pools", {"page": page, "sort": sort})
        got = pool_rows(data)
        if not got:
            break
        rows += got
    return rows


def trending_pools(pages=5, duration="24h"):
    rows = []
    for page in range(1, pages + 1):
        data = _get("/networks/solana/trending_pools", {"page": page, "duration": duration})
        got = pool_rows(data)
        if not got:
            break
        rows += got
    return rows


def daily_ohlcv(pool):
    """[(ts, open, high, low, close, volume), ...] oldest first, USD, base token."""
    data = _get(f"/networks/solana/pools/{pool}/ohlcv/day",
                {"aggregate": 1, "limit": 1000, "currency": "usd", "token": "base"})
    rows = (((data.get("data") or {}).get("attributes") or {}).get("ohlcv_list")) or []
    rows = [tuple(float(v) for v in r[:6]) for r in rows if len(r) >= 6]
    return sorted(rows, key=lambda r: r[0])
