# Portfolio Management Bot

A Telegram bot that manages a stock portfolio using momentum-based scoring, trailing stops, and provides deep analysis and performance tracking.

## Run & Operate

- `pnpm run typecheck` — Full typecheck across all packages.
- `pnpm run build` — Typecheck and build all packages.
- `pnpm --filter @workspace/api-spec run codegen` — Regenerate API hooks and Zod schemas from OpenAPI spec.
- `pnpm --filter @workspace/db run push` — Push DB schema changes (development only).
- `pnpm --filter @workspace/api-server run dev` — Run API server locally.
- **Environment Variables**: `ALPHA_VANTAGE_KEY` for earnings calendar.

## Stack

- **Monorepo**: pnpm workspaces
- **Node.js**: 24
- **Package Manager**: pnpm
- **TypeScript**: 5.9
- **API Framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod, `drizzle-zod`
- **API Codegen**: Orval (from OpenAPI spec)
- **Build Tool**: esbuild (CJS bundle)

## Where things live

- `portfolio-bot/main.py`: Main Python Telegram bot logic.
- `portfolio-bot/dashboard.py`: Web dashboard for portfolio visualization.
- `portfolio.json`: Stores current portfolio positions and historical scores.
- `recommendations_log.json`: Logs all bot recommendations and their performance.
- `signal_log.json`: Tracks tickers with recent SELL signals to filter monthly screens.
- `monthly_picks.json`: Records monthly screen recommendations.
- `cash.json`: Manages cash balance.
- `pnpm-workspace.yaml`: Defines pnpm workspace structure.
- `packages/api-spec/openapi.yaml`: OpenAPI specification for API codegen.
- `packages/db/drizzle.config.ts`: Drizzle ORM configuration and schema.

## Architecture decisions

- **Dual Sentiment System**: Two distinct news sentiment systems exist: one for momentum scoring (keyword count) and another for legacy display/Claude prompts (headline-level regex). This allows for a clean transition to the new scoring system while maintaining backward compatibility.
- **Atomic, Lock-Guarded File Operations**: `recommendations_log.json` and `cash.json` use atomic writes (temp-file + `os.replace`) and locks to prevent data corruption from concurrent access, especially in background threads.
- **Dynamic Trailing Stop Adjustment**: Trailing stops are dynamically adjusted based on the stock's volatility (ATR) and tightened when a position enters a "watch zone" (lower momentum score) to aggressively protect capital.
- **Pre-computed Trailing Stop Price**: The `apply_position_rules` function now takes a pre-computed trailing stop price, decoupling the signal generation from the stop calculation logic.
- **Deferred Recommendation Review**: If SPY history is unavailable on a review date, the review is deferred to maintain the integrity of win-rate calculations, rather than scoring against a zero benchmark.

## Product

- **Automated Portfolio Management**: Manages stock positions with buy/sell signals based on a 100-point momentum scoring system.
- **Trailing Stop Loss**: Implements dynamic trailing stops adjusted by ATR and tightened in low-momentum scenarios.
- **Earnings Calendar & Alerts**: Tracks upcoming earnings for holdings and provides timely alerts with actionable options.
- **Portfolio Health Scoring**: Calculates and displays a composite health score based on diversification, stop-loss health, upside potential, and momentum.
- **Deep Analysis**: Provides on-demand detailed momentum breakdown and AI commentary for any ticker.
- **Performance Tracking**: Logs all recommendations, grades them against SPY at 4-week and 8-week intervals, and provides performance summaries.
- **Sector Diversification**: Enforces a maximum number of positions per sector to promote diversification.
- **Cash Management**: Tracks cash balance, deducts on buys, credits on sells, and suggests parking idle cash.
- **Web Dashboard**: Offers a real-time, mobile-friendly web interface for portfolio overview, health, and recommendation history.

## User preferences

_Populate as you build_

## Gotchas

- **Pnpm filtering**: Remember to use `pnpm --filter <package_name> run <command>` for package-specific actions.
- **API Codegen**: Always run `pnpm --filter @workspace/api-spec run codegen` after modifying the OpenAPI spec to update client hooks and Zod schemas.
- **DB Schema Push**: `pnpm --filter @workspace/db run push` is for development only; use proper migration strategies for production.
- **Python Bot Dependencies**: The Python bot (`portfolio-bot/main.py`) is separate from the pnpm workspace and manages its own Python dependencies.
- **Double Execution of `main.py`**: Avoid directly executing `main.py` multiple times, especially in environments that might re-evaluate modules (e.g., some IDEs or frameworks). The `sys.modules` aliasing prevents `dashboard.py` from re-initializing the bot.

## Pointers

- [pnpm Workspaces Documentation](https://pnpm.io/workspaces)
- [TypeScript Documentation](https://www.typescriptlang.org/docs/)
- [Express.js Documentation](https://expressjs.com/)
- [Drizzle ORM Documentation](https://orm.drizzle.team/)
- [Zod Documentation](https://zod.dev/)
- [Orval Documentation](https://orval.dev/)
- [yfinance Documentation](https://pypi.org/project/yfinance/)
- [Telegram Bot API Documentation](https://core.telegram.org/bots/api)