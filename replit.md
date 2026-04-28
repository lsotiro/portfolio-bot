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

Standalone Python Telegram bot (workflow: **Portfolio Bot**) — not part of the pnpm workspace. Holds positions in `portfolio.json`, runs the daily 100-point momentum scoring system via yfinance prices + NewsAPI headlines + Claude reasoning, and a Flask keep-alive on port 5000.

### Momentum Scoring System (primary signal engine)
Every buy/sell signal is now driven by a **100-point momentum score** computed by `score_momentum(ticker)` (and the parallel bulk variant `score_momentum_bulk`):

| Component         | Pts | Source                                   |
|-------------------|-----|------------------------------------------|
| Price momentum    | 30  | 1w/4w/12w returns from yfinance history (10pts each, positive)  |
| RS vs SPY (4w)    | 20  | Stock 4w return − SPY 4w return (>3% → 20, >0% → 10) |
| Volume confirm    | 15  | Up-day vs down-day mean volume ratio (>1.5 → 15, >0.8 → 7) |
| News sentiment    | 20  | Keyword tally on last 24h headlines (≥70% positive → 20, ≥40% → 10, else 0) |
| Earnings momentum | 15  | Last reported EPS beat (8) + analyst target ≥10% above current (7) |

Earnings data uses `Ticker.earnings_dates` (yfinance's modern API; `quarterly_earnings` is fully deprecated). Newest-reported quarter only — future dates with NaN "Reported EPS" are filtered out before picking `iloc[0]`.

Score → signal:
- **≥80 STRONG BUY 🟢** / **≥65 BUY 🟢** / **≥50 HOLD 🟡** / **≥35 WATCH 🟡** / **<35 SELL 🔴**
- Score `None` (insufficient history) → WATCH 🟡 (never trips a forced sell on a single bad fetch).

### Trade rules (legacy rails — still used as guard rails)
- `STOP_LOSS_PCT = -7.0` — stop-loss line shown alongside every position; the feedback-loop uses it to mark a rec STOPPED if breached intraday.
- Analyst targets fetched from yfinance `targetMeanPrice` on `/buy` and refreshed monthly. Shown as the upside line; no longer drives a forced sell.
- Fundamentals (`passes_buy_screen`) are used ONLY as a hard filter at the top of `/monthly` — they no longer drive any individual buy/sell signal.

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

### Deep Analysis (`/deep TICKER`) — momentum-based
- Computes the full 100-point momentum score for any ticker (does not need to be in the portfolio).
- Sends one Telegram message with the full breakdown (score, 1w/4w/12w returns, RS vs SPY, volume ratio, news, earnings beat, current price + analyst target) plus a one-sentence Claude commentary citing the strongest or weakest momentum component.
- Logged to the rec log with `momentum_score_at_recommendation`.
- The legacy 25-point Claude framework (`analyze_stock_deep`, `FRAMEWORK_INSTRUCTIONS`) and `get_rich_fundamentals` are still defined in `main.py` for back-compat with the historical rec log but are no longer called from any active code path.

### News Sentiment
- Two parallel sentiment systems:
  - **Momentum scorer** (`_news_keyword_score`) — counts headlines matching the positive / negative word lists, returns 20 / 10 / 0 pts based on the positive-to-total ratio. Uses 1-day window for `/portfolio` and `/deep`, 7-day for `/monthly`.
  - **Display sentiment** (`headline_sentiment`, `aggregate_sentiment`) — legacy keyword-regex per headline returning -1 / 0 / +1; still used by the older `format_news_block` helper for any legacy Claude prompts.

### Feedback Loop & Performance Tracking
Every recommendation the bot makes is appended to `recommendations_log.json`,
graded against SPY at the 4-week and 8-week marks, and surfaced via
`/performance` and `/review`.

- **Sources logged**: monthly screen picks, `/deep TICKER` results,
  `/buy` confirmations (deep analysis runs in a background thread so
  the user gets the position confirmation immediately), and any SELL
  signals from the daily monitor.
- **Schema** (`recommendations_log.json`): `id`, `date`, `ticker`,
  `source`, `signal`, `framework_score` (legacy, None on momentum-era recs),
  `momentum_score_at_recommendation` (new — primary signal source),
  `price_at_recommendation`, `claude_target`, `analyst_target`,
  `stop_loss`, `bull_case`, `bear_case`, `sp500_at_recommendation`,
  `status` (open/closed), plus `review_4w_*` / `review_8w_*` fields
  populated at review time.
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
- 09:00 — unified `/portfolio` daily momentum review (one msg per
  position + summary, plus prepended SELL/WARNING alerts). Pre-US-open
  briefing using the 100-point momentum scoring system.

### Unified `/portfolio` (daily momentum view)
- Single command + single 09:00 UTC scheduled push. Replaces the old
  `_quick_judge_position` rule engine end-to-end with the 100-point
  momentum system above.
- Bulk-fetches fundamentals + 1-day news + SPY 3mo history ONCE,
  then runs `score_momentum_bulk` (parallel, max 8 workers) so every
  position is scored in ~5 seconds total.
- Claude reasoning via `_get_quick_reasons` — receives the full momentum
  breakdown (score + per-component details) and returns one sentence per
  ticker citing the SPECIFIC strongest or weakest component. Falls back
  to deterministic `_fallback_reason(signal, score)` if Claude is
  unavailable / errors / parses badly — never blocks the user.
- Sends **one Telegram message per position** in 🔴 SELL → 🟡 WATCH/HOLD
  → 🟢 BUY/STRONG BUY order with the spec breakdown:
  ```
  [emoji] [TICKER] — [SIGNAL]
  Momentum Score: XX/100
  Price: 1W ±X% | 4W ±X% | 12W ±X%
  vs S&P 500 (4W): Stock ±X% vs SPY ±X% → RS: ±X%
  Volume: Buying/selling ratio X.XXx (↑ Bullish / ↓ Bearish)
  News: X positive, X negative
  Earnings: Beat/Missed last Q | Estimates rising/falling
  Entry $X → Current $X | P&L: ±$X (±X%)
  Target: $X (±X% upside) | Stop: $X
  [Claude one-sentence reason]
  ```
- Final 📊 PORTFOLIO SUMMARY message bucketed by signal:
  ```
  🔴 SELL signals: [tickers] — momentum broken
  🟡 WATCH signals: [tickers] — momentum weakening
  🟢 HOLD/BUY signals: [tickers] — momentum intact
  ```
- **Persistence**: every position's freshly computed `momentum_score` is
  written back to `portfolio.json`. The next scheduled run reads the
  prior score to detect day-over-day drops > 20pts (warning trigger).
- **Scheduled-only urgent alerts** (`scheduled=True` from `scheduled_run`):
  - 🚨 SELL ALERT — sent immediately for any position that crossed below
    score 35 since the last run.
  - ⚠️ WARNING — sent for any position whose score dropped > 20 pts vs
    yesterday (catches sudden momentum collapses BEFORE they hit the SELL
    threshold).
  Both alerts are sent BEFORE the regular per-position messages so they
  surface to the top of the user's notifications.
- `/buy` separately runs `score_momentum` in a background thread on the
  new position, persists the initial score to `portfolio.json`, and logs
  the rec with `momentum_score_at_recommendation`.

### Commands
`/buy`, `/sell`, `/trim`, `/portfolio`, `/health`, `/earnings`, `/deep TICKER`, `/monthly`, `/review`, `/performance`, `/help`

### Web Dashboard (`portfolio-bot/dashboard.py`)
- Separate Flask app started in its own daemon thread from `main.py`.
- Tries port **8080** first; if taken (e.g. by the api-server artifact)
  falls back to 8082, 8083, or 8084 — actual port logged at startup.
- Reads `portfolio.json` and `recommendations_log.json`; bulk-fetches
  live prices/fundamentals via yfinance on every page load.
- Renders a dark, mobile-friendly page with: portfolio summary
  (value, P&L $/%, position count, cost basis), 4-component health
  score (same calc as `/health`), a colour-coded holdings table
  (green ≥ entry, yellow between stop and entry, red ≤ stop) with
  analyst target + upside %, last 5 recommendations, and the bot's
  track record (total / closed / win rate / avg return / best+worst).
- Auto-refreshes every 60 seconds via meta-refresh.
- 30-second server-side TTL cache (`_cache` + `_cache_lock`) collapses
  concurrent requests / multiple open tabs into a single yfinance
  fetch and falls back to last-known-good if a refresh fails.
- `main.py` aliases itself in `sys.modules` as both `__main__` and
  `main` at boot so `dashboard.py`'s lazy `from main import …` returns
  the running instance and never double-executes the script (which
  would otherwise re-register every `schedule` job and create a
  second Flask app).
