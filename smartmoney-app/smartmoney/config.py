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
WINNERS_FILE = _env("winners_file", "winners.txt")            # your own picks (optional)
WINNERS_AUTO_FILE = _env("winners_auto_file", "winners_auto.txt")  # written by `winners`

# --- Winners: which past tokens count as "ran"? ------------------------------
WINNER_MIN_AGE_DAYS = _env("winner_min_age_days", 7, float)    # launched at least this long ago
WINNER_MAX_AGE_DAYS = _env("winner_max_age_days", 120, float)  # ...and no longer ago than this
EARLY_DAYS = _env("early_days", 3, float)            # "early low" = lowest price in first N days
MIN_RUN_MULTIPLE = _env("min_run_multiple", 10, float)   # early low -> peak
MIN_PEAK_MCAP_USD = _env("min_peak_mcap_usd", 3_000_000, float)
MAX_WINNERS = _env("max_winners", 40, int)
MAX_WINNERS_PER_WEEK = _env("max_winners_per_week", 5, int)  # spread across launch weeks

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
MIN_HITS = _env("min_hits", 3, int)

# --- Vetting: drop bots and wallet clusters --------------------------------
# Only the strongest candidates (by hits) get vetted; vetting costs ~60 credits each.
VET_TOP = _env("vet_top", 600, int)
VET_RECENT_TXS = _env("vet_recent_txs", 500, int)   # recent history read per wallet
MAX_TX_PER_DAY = _env("max_tx_per_day", 400, float)
# "Spray" wallets buy almost every new launch; their hits are volume, not skill.
MAX_NEW_TOKENS_PER_DAY = _env("max_new_tokens_per_day", 25, float)
# Wallets that usually sell within minutes can't be copied in time: tracking
# polls every POLL_MINUTES, so a buy-and-sell inside that window nets to nothing.
MIN_MEDIAN_HOLD_MIN = _env("min_median_hold_min", 30, float)
# A position counts as exited once this share of it is sold.
EXIT_SHARE = _env("exit_share", 0.75, float)
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

# --- Accumulation alerts -------------------------------------------------------
# Wallets whose repeat buying you want pinged about (comma-separated in Secrets).
# Defaults: the three tracked wallets that made money in the Oct 16 review.
WATCH_WALLETS = [w.strip() for w in _env(
    "watch_wallets",
    "D9Jibi4oneM46vCjF2qq8Azc2kPxCTfJKi6i5uMnz8Aq,"
    "6uu3hHQinzVRFvpdy7m44RM8w221ACDxqXpmvLiLUFd6,"
    "AmpmyC9C7k9eKpPHt7N2qPR5cDPmrv1mQPjKjz7xZqRw").split(",") if w.strip()]
ACCUM_MILESTONES = [int(x) for x in _env("accum_milestones", "3,5,8").split(",")]
ACCUM_WINDOW_HOURS = _env("accum_window_hours", 72, float)
ACCUM_MIN_BUY_SOL = _env("accum_min_buy_sol", 0.5, float)   # ignore dust buys
# Staked SOL and similar: steady buying here isn't memecoin accumulation.
NON_MEME_MINTS = {
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",   # mSOL
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # JitoSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",   # bSOL
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v",   # JupSOL
}

# --- Paper trading (fake money, scored like real trades) ----------------------
PAPER_SIZE_USD = _env("paper_size_usd", 100, float)
PAPER_SLIPPAGE_PCT = _env("paper_slippage_pct", 3, float)      # per side, incl. fees
PAPER_MIN_LIQUIDITY_USD = _env("paper_min_liquidity_usd", 20000, float)
PAPER_STOP_LOSS_PCT = _env("paper_stop_loss_pct", 40, float)
PAPER_TRAIL_ARM_X = _env("paper_trail_arm_x", 2.0, float)      # trailing stop starts at 2x
PAPER_TRAIL_PCT = _env("paper_trail_pct", 30, float)           # ...sells 30% off the high
PAPER_MAX_HOLD_HOURS = _env("paper_max_hold_hours", 168, float)
PAPER_REENTRY_HOURS = _env("paper_reentry_hours", 24, float)   # no re-buy right after exit

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
