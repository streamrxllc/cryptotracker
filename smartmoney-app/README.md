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
