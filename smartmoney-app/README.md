# Smart Money Tracker (Solana)

Finds wallets that repeatedly bought winners early, tracks them, and flags
tokens that several of them are accumulating at the same time.

```
winners ──► discover ──► vet ──► track (every 15 min) ──► signals ──► dashboard / Telegram
past runners  early buyers  drop bots   new buys & sells      consensus
(price history) on 2+ winners & clusters
```

## Setup on Replit (5 minutes)

1. Create a new Python Repl and upload this folder (or the zip, then unzip).
2. Get a free API key at **dashboard.helius.dev**. In Replit, open **Tools → Secrets**
   and add `HELIUS_API_KEY`.
3. Optional phone alerts: create a Telegram bot with @BotFather, then add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Secrets.
4. In the Shell:
   ```
   pip install -r requirements.txt
   python main.py setup
   ```
   `setup` runs three steps (each can also be run on its own):
   - `winners`: finds past winners from GeckoTerminal's free price history
     (no key needed). Takes ~10 minutes because of its rate limit.
   - `discover`: finds wallets that bought those winners early (Helius).
   - `vet`: drops bots/clusters and starts tracking the best wallets.

   Optional: add your own picks to `winners.txt`; they're included too.
5. Press **Run**. That starts the tracker loop and a dashboard in the webview.
   To keep it running 24/7, deploy it as a Reserved VM (Deploy → Reserved VM).

Other commands: `python main.py wallets`, `add WALLET [label]`, `remove WALLET`,
`track --once`, and `pick` (re-apply vetting thresholds to stored stats after
changing them; costs no credits).

## What counts as what

| Step | Rule (all adjustable in `smartmoney/config.py` or as Secrets) |
|---|---|
| Winner | Launched **7–120 days** ago, rose **10x+** from its first-3-day low to its peak, peaked at **$3M+** market cap; max 40, at most 5 per launch week |
| Early buy | Paid ≥ **5x** below the token's peak price (`MIN_ENTRY_MULTIPLE`) |
| Not a sniper | Bought > **60 s** after the token's first transaction; the creator is excluded |
| Real position | ≥ **0.1 SOL** total on that token |
| Candidate | Early on ≥ **3** winners (`MIN_HITS`); the top **600** by hits get vetted (`VET_TOP`) |
| Bot | > **400** transactions/day that the wallet itself signed (incoming spam ignored) |
| Spray buyer | Buys > **25** different tokens/day: hits come from volume, not skill |
| Quick flipper | Median time to sell **75%** of a position < **30 minutes** (taking initials early is fine) |
| Cluster | Wallets first funded by the same address (2–5 wallets); one is kept |
| Signal | ≥ **3** tracked wallets net buying the same token within **48 h** |
| "Adding" | Wallets that bought it more than once (accumulating, not one-shot) |
| ⚠️ Late | Token already up ≥ **300%** in 24 h |

The signal score weights each buyer by how many winners they were early on,
and subtracts half a point for each tracked wallet that is net selling.

## Accumulation alerts

Watched wallets (`WATCH_WALLETS`, comma-separated; defaults to D9Ji…, 6uu3…
and Ampm…, the ones that made money in the first review) trigger a Telegram
alert and a dashboard entry when, within 72 hours, they:

- make their **3rd, 5th or 8th** buy (0.5 SOL+) of the same token, and
- **start selling** a token they had bought 3+ times (your exit signal).

Each alert fires once. Staked-SOL tokens (mSOL, JitoSOL…) are ignored.
Alerts need the tracker running (Run button, or a Reserved VM for 24/7)
and the Telegram secrets set.

## Dashboard (home page)

The home page is the accumulation report: the Accumulation board (positions
watched wallets bought 2+ times, with a 7-day buys-per-day strip), 24-hour
buys and sells, tokens held by 2+ wallets, and each wallet's open positions.
Alerts, paper trades, consensus buys and tracked wallets are below it.

It's password-protected: set the Secret `DASHBOARD_PASSWORD`. Sign-in lasts
30 days; changing the password signs everyone out. Wallet trades update every
`POLL_MINUTES` (15); prices refresh on load (cached 1 minute); the page
reloads itself every 5 minutes.

## Daily morning report (email)

Every day at 8am New York time (`REPORT_HOUR`, `REPORT_TZ`) the tracker emails a
report on the watched wallets: what each one accumulated or sold in the last
24 hours, tokens held by 2+ of them, every open position (put in, worth now,
buys/sells, result), and paper-trading results. A short version goes to
Telegram too.

Setup (Zapier): make a Zap with trigger "Webhooks by Zapier → Catch Hook",
copy its URL into the Secret `REPORT_WEBHOOK_URL`, run
`python main.py report --send` once so Zapier sees sample data, then add an
email action using the `subject` and `html` fields. For a text message
instead, use "SMS by Zapier" with the `sms` field: a single line of 153
characters or fewer, e.g. `SmartMoney 10/17: SELL 6uu3 FTR, BUY D9Ji NORMIE x5`
(US/UK numbers; not T-Mobile).

Setup (Resend): add Secrets `RESEND_API_KEY`, `REPORT_EMAIL_TO` and, if you
have a verified domain in Resend, `REPORT_EMAIL_FROM` (e.g.
`Smart Money <alerts@yourdomain.com>`). Without a verified domain the default
sender `onboarding@resend.dev` only delivers to your Resend account's email.

Setup (Gmail, instead of Resend): turn on 2-Step Verification, create an app password at
myaccount.google.com/apppasswords, then add Secrets `SMTP_USER` (your Gmail
address), `SMTP_PASSWORD` (the 16-character app password) and
`REPORT_EMAIL_TO` (where to send it). Test with `python main.py report --send`;
preview in the Shell with `python main.py report`.

## Paper trading

Every signal automatically opens a fake $100 position (`PAPER_SIZE_USD`) at
the current price plus 3% slippage, if the token has $20k+ liquidity and
hasn't already run. Each position closes on the first of:

| Exit | Rule |
|---|---|
| Stop loss | down **40%** |
| Trailing stop | once up **2x**, sells if it falls **30%** from its high |
| Smart money sold | half or more of the wallets that triggered the buy have sold |
| Time stop | held **7 days** |

Results are on the dashboard and via `python main.py paper`. Buys and sells
also go to Telegram if it's set up. Run this for a couple of weeks before
putting real money behind the signals.

## Cost (Helius free plan = 1M credits/month)

- Discovery: up to ~2,000 credits per winner (stops early once the price
  leaves the cheap zone). 30 winners ≈ 60k max.
- Vetting: ~20 credits per candidate.
- Tracking: 1 credit per wallet per check, plus ~10 when a wallet traded.
  40 wallets every 15 min is roughly 300–700k/month. If you run short, set
  `POLL_MINUTES` to 30, or upgrade to Developer ($49/mo).

Each command prints its estimated credit use.

## Notes

- Winners come from pools that are still active on GeckoTerminal, so
  tokens that ran and then fully died are under-represented. Add any you
  remember to `winners.txt`.
- Uses Helius `getTransactionsForAddress` (oldest-first history with
  filters). If your key's plan rejects it, the error will say so.
- Trades are read from balance changes, so any DEX, aggregator or
  launchpad (pump.fun, Raydium, Meteora, Jupiter…) is covered.
- Re-run `discover` + `vet` every few weeks with fresh winners. Smart wallets
  go cold, and some start getting copied (and dumping on followers).
- This produces signals only. It does not trade. Check the chart and the
  wallets' actual buys before acting.
