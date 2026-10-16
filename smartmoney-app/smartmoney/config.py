"""All tunable settings in one place. Override any of them with environment
variables (Replit: Tools -> Secrets) using the same name in UPPER CASE."""
import os


def _env(name, default, cast=str):
    raw = os.environ.get(name.upper())
    if raw is None or raw == "":
        return default
    if cast is bool:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


# --- API keys -------------------------------------------------------------
HELIUS_API_KEY = _env("helius_api_key", "")
TELEGRAM_BOT_TOKEN = _env("telegram_bot_token", "")   # optional alerts
TELEGRAM_CHAT_ID = _env("telegram_chat_id", "")

# --- Storage ---------------------------------------------------------------
DB_PATH = _env("db_path", "smartmoney.db")
WINNERS_FILE = _env("winners_file", "winners.txt")

# --- Discovery: who bought the winners early? -------------------------------
# A buy counts as "early" if the price paid was at least this many times
# cheaper than the token's reference price (its current price on Dexscreener).
MIN_ENTRY_MULTIPLE = _env("min_entry_multiple", 5.0, float)
# Ignore buys in the first N seconds after the token's first transaction.
# Those are launch snipers, bundlers and the dev — not copyable.
SNIPE_SECONDS = _env("snipe_seconds", 60, int)
# Ignore tiny positions.
MIN_POSITION_SOL = _env("min_position_sol", 0.1, float)
# Stop scanning a token once this many hours after launch have been read...
MAX_SCAN_HOURS = _env("max_scan_hours", 96, float)
# ...or once this many transactions have been read (cost control).
MAX_SCAN_TXS = _env("max_scan_txs", 20000, int)
# Stop once trades have been above the "cheap" cutoff this many trades in a row.
EXPENSIVE_RUN_STOP = _env("expensive_run_stop", 400, int)
# A wallet becomes a candidate once it was early on this many winners.
MIN_HITS = _env("min_hits", 2, int)

# --- Vetting: drop bots and wallet clusters --------------------------------
MAX_TX_PER_DAY = _env("max_tx_per_day", 400, float)
# A funder shared by more candidates than this is probably an exchange.
MAX_CLUSTER_SIZE = _env("max_cluster_size", 5, int)
# How many vetted wallets to start tracking automatically.
AUTO_TRACK_TOP = _env("auto_track_top", 40, int)

# --- Tracking & signals -----------------------------------------------------
POLL_MINUTES = _env("poll_minutes", 15, float)
BACKFILL_HOURS = _env("backfill_hours", 24, float)  # history pulled for a newly tracked wallet
SIGNAL_WINDOW_HOURS = _env("signal_window_hours", 48, float)
MIN_WALLETS_FOR_SIGNAL = _env("min_wallets_for_signal", 3, int)
MIN_BUY_SOL = _env("min_buy_sol", 0.05, float)
# Flag a signal as "late" if the token is already up this much in 24h.
LATE_PCT_24H = _env("late_pct_24h", 300.0, float)
ALERT_COOLDOWN_HOURS = _env("alert_cooldown_hours", 12, float)

# --- Networking ---------------------------------------------------------------
REQUESTS_PER_SECOND = _env("requests_per_second", 5.0, float)
PORT = _env("port", 8080, int)

# Mints treated as "money" rather than as the asset being accumulated.
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
SOL_QUOTES = {WSOL}
USD_QUOTES = {USDC, USDT}
QUOTE_MINTS = SOL_QUOTES | USD_QUOTES
