# Smart Money Tracker (Solana)

Finds wallets that repeatedly bought winners early, tracks them, and flags
tokens that several of them are accumulating at the same time.

```
winners.txt ──► discover ──► vet ──► track (every 15 min) ──► signals ──► dashboard / Telegram
 past runners   early buyers  drop bots   new buys & sells      consensus
                on 2+ winners & clusters
```

## Setup on Replit (5 minutes)

1. Create a new Python Repl and upload this folder (or the zip, then unzip).
2. Get a free API key at **dashboard.helius.dev**. In Replit, open **Tools → Secrets**
   and add `HELIUS_API_KEY`.
3. Optional phone alerts: create a Telegram bot with @BotFather, then add
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as Secrets.
4. Put 20–40 winning token addresses in `winners.txt`.
5. In the Shell:
   ```
   pip install -r requirements.txt
   python main.py discover     # find early buyers across the winners
   python main.py vet          # drop bots/clusters, start tracking the best
   ```
6. Press **Run**. That starts the tracker loop and a dashboard in the webview.
   To keep it running 24/7, deploy it as a Reserved VM (Deploy → Reserved VM).

Other commands: `python main.py wallets`, `add WALLET [label]`, `remove WALLET`,
`track --once`.

## What counts as what

| Step | Rule (all adjustable in `smartmoney/config.py` or as Secrets) |
|---|---|
| Early buy | Paid ≥ **5x** below the reference price (`MIN_ENTRY_MULTIPLE`) |
| Not a sniper | Bought > **60 s** after the token's first transaction; the creator is excluded |
| Real position | ≥ **0.1 SOL** total on that token |
| Candidate | Early on ≥ **2** winners (`MIN_HITS`; 3 is stricter and better with 30+ winners) |
| Bot | > **400** transactions/day |
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

- Uses Helius `getTransactionsForAddress` (oldest-first history with
  filters). If your key's plan rejects it, the error will say so.
- Trades are read from balance changes, so any DEX, aggregator or
  launchpad (pump.fun, Raydium, Meteora, Jupiter…) is covered.
- Re-run `discover` + `vet` every few weeks with fresh winners. Smart wallets
  go cold, and some start getting copied (and dumping on followers).
- This produces signals only. It does not trade. Check the chart and the
  wallets' actual buys before acting.
