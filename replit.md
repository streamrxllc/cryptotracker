# Solana Smart-Money Tracker

Tracks Solana wallets that bought past winners early and reports consensus accumulation signals. The imported app is read-only and does not execute trades.

## Run & Operate

- `cd smartmoney-app && PORT=8000 python main.py serve` — dashboard and polling workflow
- `cd smartmoney-app && python main.py discover` — discover early buyers from `winners.txt`
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
- `smartmoney-app/winners.txt` — input addresses used by discovery
- `smartmoney-app/README.md` — setup and tracker behavior

## Architecture decisions

- Keep the supplied tracker source and behavior intact; run it as a separate Python workflow.
- The tracker reads public chain data and emits signals only; it has no wallet signing or transaction-submission path.
- The tracker uses its supplied SQLite database and local `winners.txt`; no database migration is needed.

## Product

Discovers wallets that bought previous winners early, filters bots and clusters, monitors wallet activity, and highlights tokens with multiple tracked wallets accumulating. It can optionally send Telegram alerts.

## User preferences

- Use the attached Python app as supplied.

## Gotchas

- Add token addresses to `smartmoney-app/winners.txt`, then run `discover` and `vet` before expecting tracked-wallet signals.
- Port 8000 is used for the tracker because the shared API service already uses port 8080.
- Helius and Telegram credentials belong in Replit Secrets, never in source files.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
