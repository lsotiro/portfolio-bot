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

### Portfolio Health Score (0–10)
- `calculate_health_score(portfolio, fundamentals=None)` averages four sub-scores:
  - **Diversification** — distinct sectors (4+ → 10, 3 → 7, 2 → 4, ≤1 → 1)
  - **Stop-loss health** — `10 - 2 × (positions below stop)`, floor 0
  - **Upside remaining** — avg `(target − current) / current` (>30% → 10, ≥20% → 7, ≥10% → 4, else 1)
  - **Momentum** — `(positions with positive P&L / total) × 10`
- Rating bands: **8–10 💪 Strong**, **6–7.9 👍 Healthy**, **4–5.9 ⚠️ Needs attention**, **<4 🚨 Critical**.
- Sector pulled from `info["sector"]` via `FUNDAMENTAL_FIELDS` so `/portfolio` reuses one bulk fetch (no extra API calls).
- One-line summary embedded at top of `/portfolio`; full breakdown via `/health` and the daily 08:30 UTC push.
- When data is missing for a component, score floors at 1.0 with an "(insufficient data)" note (matches the spec's lowest published bucket).

### Deep Analysis Framework (5-pillar)
- `get_rich_fundamentals(ticker)` — superset of basic snapshot adding valuation
  (PEG, P/S, P/B, EV/EBITDA), quarterly growth fields, margins (gross/op/net),
  health (D/E, current ratio, FCF, ROE, ROA), ownership %, and computed:
  - **Analyst conviction** = `10 - ((targetHigh - targetLow) / targetMean × 10)` clamped 0-10. Tighter spread = higher conviction.
  - **Earnings trend** — last 4 reported EPS via `get_earnings_dates(limit=12)` → 3 q/q growth rates → `accelerating` / `decelerating` / `mixed` / `insufficient`.
- **5-pillar Claude prompt** (`FRAMEWORK_INSTRUCTIONS`): Business Quality / Growth Trajectory / Valuation / Catalyst / Risk, each scored 1-5. Total /25 → **STRONG BUY 20-25, BUY 15-19, HOLD 10-14, SELL <10**. Each pick gets concrete price target, stop loss, time horizon (weeks), bull/bear paragraphs.
- Used by:
  - `/analyze` daily monitor — batch call scores all non-forced positions.
  - `/monthly` — basic fetch on SP500 → hard filter → rich fetch only on the survivors → framework + top-2 pick.
  - `/deep TICKER` — single-stock on-demand deep dive.

### News Sentiment
- `headline_sentiment(text)` — keyword regex with word boundaries; returns -1 / 0 / +1.
- Each article carries a `sentiment` field; `aggregate_sentiment(articles)` averages to [-1, +1].
- `format_news_block` includes per-headline POS/NEG/NEU tag and aggregate score, fed to all Claude prompts.

### Feedback Loop & Performance Tracking
Every recommendation the bot makes is appended to `recommendations_log.json`,
graded against SPY at the 4-week and 8-week marks, and surfaced via
`/performance` and `/review`.

- **Sources logged**: monthly screen picks, `/deep TICKER` results,
  `/buy` confirmations (deep analysis runs in a background thread so
  the user gets the position confirmation immediately), and any SELL
  signals from the daily monitor.
- **Schema** (`recommendations_log.json`): `id`, `date`, `ticker`,
  `source`, `signal`, `framework_score`, `price_at_recommendation`,
  `claude_target`, `analyst_target`, `stop_loss`, `bull_case`,
  `bear_case`, `sp500_at_recommendation`, `status` (open/closed),
  plus `review_4w_*` / `review_8w_*` fields populated at review time.
- **Atomic, lock-guarded writes** — `save_recs` writes via
  temp-file + `os.replace` and `_recs_lock` brackets every read/write
  so concurrent `/buy` background threads can't corrupt the log.
- **Daily 07:30 UTC** `check_recommendation_reviews` — for each open
  rec hitting its 4w or 8w date, fetches current price + current SPY,
  compares the two returns, and marks **CORRECT** (beat SPY),
  **INCORRECT** (lagged), or **STOPPED** (touched stop-loss intraday
  inside the window). 8w review additionally sets `status=closed`.
  If SPY history is unavailable that day, the review is **deferred**
  rather than scored against a 0% benchmark — preserves win-rate integrity.
- **Sunday 08:00 UTC** weekly summary — recap of any reviews finalized
  in the past 7 days + running win rate + one-line Claude commentary
  on what the strategy results suggest.
- **`/performance`** — track record using **closed (8w-final) recs only**
  for headline stats (4w-only mixing would distort returns). Shows total,
  win rate, avg return vs SPY, best/worst call, win rate split by signal
  type (STRONG BUY vs BUY) and by framework score range (20-25 vs 15-19),
  plus the last 5 calls regardless of status.
- **`/review`** — every open rec with current price, return-so-far, and
  countdowns to the 4w/8w review dates.
- **Adaptive monthly prompt** — once 10+ recs have closed,
  `get_track_record_for_prompt()` injects historical win rates by
  signal type and score range into the monthly screen prompt so Claude
  can weight the most predictive factors more heavily going forward.

### Schedules (UTC)
- 07:30 — recommendation review check (4w/8w grading)
- 08:00 — earnings calendar sweep (and Sunday-only weekly summary)
- 08:30 — morning portfolio health score push
- 21:30 — lightweight daily monitor (runs 30 min after US market close
  so today's % move from `regularMarketPreviousClose` is meaningful;
  a pre-open run would compare yesterday's close to itself and the
  >3% mover trigger would never fire)

### Daily Monitor (lightweight)
- Bulk-fetches basic fundamentals (one API call) → gives current price,
  previous close, and fresh analyst target for every holding.
- **Refreshes the analyst target on every position every run** (replaces
  the old day-1-of-month bulk refresh). Resets the per-position alert
  flags whenever the target changes meaningfully.
- One-line-per-position summary with hard-rule SELL signals (stop-loss,
  above analyst fair value) — no Claude framework call by default.
- **>3% single-day move →** auto-fires `analyze_stock_deep` in a
  background thread for that ticker, sends the full 5-pillar analysis
  to Telegram, and logs it to the feedback loop with `source=daily-mover`.
- **>5% single-day move →** also alerts if the analyst target shifted
  by ≥5% on the same day ("re-evaluate the thesis").
- `/buy` separately triggers its own deep analysis on the new position
  (background thread → confirmation Telegram + feedback-loop log).

### Commands
`/buy`, `/sell`, `/trim`, `/portfolio`, `/health`, `/earnings`, `/analyze`, `/deep TICKER`, `/monthly`, `/review`, `/performance`, `/help`
