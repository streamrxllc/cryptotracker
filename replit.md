# Solana Smart-Money Tracker

Tracks Solana wallets that bought past winners early and reports consensus accumulation signals. The imported app is read-only and does not execute trades.

## Run & Operate

- `cd smartmoney-app && PORT=8000 python main.py serve` — dashboard and polling workflow
- `cd smartmoney-app && python main.py winners` — find past winners from public GeckoTerminal price history
- `cd smartmoney-app && python main.py setup` — run winner collection, wallet discovery, and vetting
- `cd smartmoney-app && python main.py discover` — discover early buyers from the automatic and manual winner lists
- `cd smartmoney-app && python main.py vet` — filter candidates and choose wallets to track
- `cd smartmoney-app && python main.py wallets` — list discovered and tracked wallets
- `cd smartmoney-app && python main.py track --once` — run one polling cycle
- Required secret: `HELIUS_API_KEY`
- Optional alert secrets: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)

## Stack

- Python 3.11, Flask, requests, and SQLite for the imported tracker
- pnpm workspaces, Node.js 24, TypeScript 5.9 for the shared project services
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `smartmoney-app/` — imported Python app, dashboard, SQLite database, and tests
- `smartmoney-app/winners.txt` — optional manually selected winner mints
- `smartmoney-app/winners_auto.txt` — generated winner list used by discovery
- `smartmoney-app/README.md` — setup and tracker behavior

## Architecture decisions

- Keep the supplied tracker source and behavior intact; run it as a separate Python workflow.
- Automatic winner selection uses GeckoTerminal price history; the generated list and optional manual picks feed the existing discovery flow.
- The tracker reads public chain data and emits signals only; it has no wallet signing or transaction-submission path.
- The tracker keeps its supplied SQLite database; no database migration is needed.

## Product

Finds historical Solana winners from public price history, discovers wallets that bought them early, filters bots and clusters, monitors wallet activity, and highlights tokens with multiple tracked wallets accumulating. It can optionally send Telegram alerts.

## User preferences

- Use the attached Python app as supplied.

## Gotchas

- `setup` runs a rate-limited GeckoTerminal scan (about 10 minutes) followed by Helius discovery and vetting, which uses Helius credits. It is not run automatically.
- Optional manual winner mints belong in `smartmoney-app/winners.txt`; generated winners are stored separately in `winners_auto.txt`.
- Port 8000 is used for the tracker because the shared API service already uses port 8080.
- Helius and Telegram credentials belong in Replit Secrets, never in source files.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
