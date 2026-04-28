# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Portfolio Bot (`portfolio-bot/main.py`)

Standalone Python Telegram bot (workflow: **Portfolio Bot**) — not part of the pnpm workspace. Holds positions in `portfolio.json`, runs daily HOLD/SELL analysis via Claude with yfinance fundamentals + NewsAPI headlines, and a Flask keep-alive on port 5000.

### Trade rules (analyst-target based)
- `STOP_LOSS_PCT = -7.0` — forced SELL at -7% from entry
- `ABOVE_TARGET_FRACTION = 1.00` — forced SELL when price ≥ analyst target
- `APPROACH_TARGET_FRACTION = 0.90` — soft alert at ≥ 90% of target
- `LOW_TARGET_FRACTION = 0.70` — info: price ≤ 70% of target → high upside
- Analyst targets fetched from yfinance `targetMeanPrice` on `/buy` and refreshed monthly (1st of month, before daily analysis).

### Earnings calendar (Alpha Vantage + yfinance)
- `ALPHA_VANTAGE_KEY` secret required.
- `fetch_earnings_calendar(ticker)` → AV `EARNINGS_CALENDAR` CSV endpoint (3-month horizon).
- `get_last_quarter_eps(ticker)` → yfinance `get_earnings_dates()` (requires `lxml`) for beat/miss summary.
- Daily 08:00 UTC sweep alerts when any holding has earnings ≤ `EARNINGS_ALERT_DAYS` (3) days away, including position size, live P&L, EPS estimate, last-quarter beat/miss, and 3 actionable options.
- `/earnings` lists upcoming earnings within 30 days.

### Schedules (UTC)
- 08:00 — earnings calendar sweep
- 09:00 — full `/analyze` portfolio review
- Day 1 of month — analyst target refresh, then `/analyze`

### Commands
`/buy`, `/sell`, `/trim`, `/portfolio`, `/earnings`, `/analyze`, `/monthly`, `/help`
