"""Small Helius RPC client with throttling, retries and a credit estimate."""
import time

import requests

from . import config


class HeliusError(RuntimeError):
    pass


class Helius:
    def __init__(self, api_key=None, rps=None):
        self.api_key = api_key or config.HELIUS_API_KEY
        if not self.api_key:
            raise HeliusError(
                "HELIUS_API_KEY is not set. Get a free key at dashboard.helius.dev "
                "and add it in Replit under Tools -> Secrets.")
        self.url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        self.min_gap = 1.0 / (rps or config.REQUESTS_PER_SECOND)
        self._last = 0.0
        self.credits = 0
        self.session = requests.Session()

    # -- plumbing ---------------------------------------------------------
    def rpc(self, method, params, credits=1):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt in range(6):
            wait = self.min_gap - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.session.post(self.url, json=body, timeout=60)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                last_err = e
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                last_err = f"HTTP {r.status_code}"
                continue
            data = r.json()
            if "error" in data:
                raise HeliusError(f"{method}: {data['error']}")
            self.credits += credits
            return data["result"]
        raise HeliusError(f"{method} failed after retries: {last_err}")

    # -- endpoints --------------------------------------------------------
    def latest_signature(self, address):
        """Newest signature touching an address (1 credit). Cheap change check."""
        res = self.rpc("getSignaturesForAddress", [address, {"limit": 1}])
        return res[0]["signature"] if res else None

    def transactions(self, address, *, order="asc", full=True, limit=100,
                     filters=None, max_txs=None):
        """Generator over getTransactionsForAddress pages.

        Full mode costs ~10 credits per 100 transactions; signature mode 10
        credits per page.
        """
        cfg = {
            "transactionDetails": "full" if full else "signatures",
            "sortOrder": order,
            "limit": limit,
            "commitment": "finalized",
            "maxSupportedTransactionVersion": 1,  # per Helius docs: includes v0 txs
        }
        if full:
            cfg["encoding"] = "jsonParsed"
        if filters:
            cfg["filters"] = filters
        seen = 0
        token = None
        while True:
            if token:
                cfg["paginationToken"] = token
            res = self.rpc("getTransactionsForAddress", [address, cfg],
                           credits=max(10, 10 * -(-limit // 100)))
            for tx in res.get("data") or []:
                yield tx
                seen += 1
                if max_txs and seen >= max_txs:
                    return
            token = res.get("paginationToken")
            if not token:
                return
