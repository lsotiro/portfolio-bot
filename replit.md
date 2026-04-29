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

### Position rules (`apply_position_rules`)
For OWNED positions, `/portfolio` overlays the **trailing stop loss** on top
of the momentum score so SELL fires on EITHER trigger:

| Condition                                          | Signal        |
|----------------------------------------------------|---------------|
| Score < 35  **OR**  current ≤ trailing stop price  | **SELL 🔴** |
| Score ≥ 80  AND  price above stop                  | STRONG BUY 🟢 |
| Score ≥ 65  AND  price above stop                  | BUY 🟢        |
| Score ≥ 50  AND  price above stop                  | HOLD 🟢       |
| Score 35–50 AND  price above stop                  | WATCH 🟡      |
| Score `None` (insufficient history)                | WATCH 🟡      |

### Signal confirmation (anti-flipping)
Raw SELL signals from momentum are filtered by `handle_portfolio` before
being sent or displayed:

| Trigger                                  | Result                                |
|------------------------------------------|---------------------------------------|
| Trailing stop breached                   | SELL fires immediately (no confirmation) |
| Score < 35 for **2 consecutive days**    | SELL confirmed                        |
| Score dropped **> 30 pts** in one day    | SELL confirmed (dramatic collapse)    |
| Score < 35 on day 1 only                 | Downgraded to **WATCH** (unconfirmed) |

**Monthly-screen buy protection** (~5 trading days = 7 calendar days):
When a stock is bought after a monthly screen recommendation, a second gate
applies for the first 7 calendar days:
- Trailing stop breach → SELL fires
- 2 consecutive days < 35 → SELL fires
- Dramatic drop only → downgraded to WATCH with protection note

### Signal stability indicator
Every `/portfolio` report shows per-position:
- **Stable** — same signal as yesterday
- **Changing ⚠️** — different from yesterday, both scores shown side-by-side

### Score history (`score_history`)
Rolling 5-entry list `[{"date": "YYYY-MM-DD", "score": N}]` stored per
position in `portfolio.json`. Updated every time `handle_portfolio` runs.

### Monthly-screen sell filter (`signal_log.json`)
When a confirmed SELL is logged by the scheduled 09:00 run, the ticker is
stamped in `signal_log.json`. The monthly screen filters out any ticker that
had a SELL signal within the last 7 days before recommending it as a BUY.

### Monthly picks (`monthly_picks.json`)
`run_monthly_screen` records every pick to `{ticker: ISO-date}`. When the
user runs `/buy` on a ticker that was monthly-recommended within the last
30 days, the position is stamped with `monthly_buy_date` and gets the
5-day SELL-suppression protection.

`apply_position_rules(score, current, stop_price)` now takes the
**precomputed trailing stop price** so the caller (always
`compute_trailing_stop`) decides whether the stop is peak-based,
ATR-adjusted, or tightened (see "Trailing stop" below). Returns
`(signal, color, stop_breached)`.

For `/deep` and `/monthly` (no portfolio context) the **pure**
`get_momentum_signal(score)` is still used — same thresholds, no stop overlay.

### Bonus context flag (display-only, NOT a sell trigger)
- When `current > analyst_target`, the position line shows
  `⚠️ above target — may be overextended` next to the upside %.
  Claude's per-position reason also calls this out.
- Stops are rendered but no longer drive a forced sell **above** the
  stop — only **at or below** triggers SELL via the rule above.
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
- 08:00 — **gap-down pre-market check** (`check_gap_down`) — for every
  position, fetch `preMarketPrice` (via `_premarket_price` → yfinance
  `info`) and 🚨 alert if it's already below the trailing stop. Runs in
  the same 08:00 slot as the earnings sweep so the user gets both
  pre-open warnings together.
- 08:30 — morning portfolio health score push
- 09:00 — unified `/portfolio` daily momentum review (one msg per
  position + summary, plus prepended SELL/WARNING alerts). Pre-US-open
  briefing using the 100-point momentum scoring system.

### Position management v2 — trailing stops, ATR, RVOL, sector cap, cash
Layered on top of the 100-point momentum scoring engine. Eight new
mechanics, all gated by constants at the top of `main.py`:

**Trailing stop loss from peak** (`compute_trailing_stop(pos, current)`)
- Every position tracks `peak_price` (high-water mark since entry).
  `handle_portfolio` bumps it on every scan when `current > peak_price`.
- Default stop is `peak × (1 − atr_stop_pct/100)`, returned with mode
  `"trailing"`.
- When a position is in the **watch zone** (score 35–49) the stop is
  tightened at the moment of transition to
  `max(current × 0.97, peak × (1 − atr_stop_pct/100))` — i.e. the
  watch-zone tighten can only ever **raise** the stop, never lower it,
  so entering watch zone with an already-breached price still fires
  SELL. The tightened price is **frozen** in
  `pos["tightened_stop_price"]` so subsequent dips don't keep walking
  the stop down — the snapshot is reused until score recovers ≥ 50,
  at which point the position exits watch zone, `peak_price` is
  rebased to `max(peak, current)`, and the stop reverts to peak-based.
- **`/trim`** also credits proceeds at the current price via
  `adjust_cash` (parity with `/sell`) so cash bookkeeping doesn't
  drift on partial exits.

**ATR-adjusted stop %** (`calculate_atr` + `get_atr_stop_pct`)
- Plain-Python 14-day True Range loop (no pandas import) over yfinance
  `Ticker.history(period="30d")`.
- Buckets: ATR ≥ `ATR_HIGH_VOL_THRESHOLD` (4%) → `−10%` stop,
  `ATR_LOW_VOL_THRESHOLD` (2%) ≤ ATR < 4% → `−7%`,
  ATR < 2% → `−5%`.
- Computed once at `/buy` (with high-volatility warning if ≥ 4%) and
  refreshed every `ATR_RECALC_DAYS` (7) days inside `handle_portfolio`.
- Stored as `pos["atr_pct"]`, `pos["atr_stop_pct"]`, `pos["atr_last_calc"]`.

**RVOL volume scoring** (in `score_momentum`)
- Replaces the old up-day / down-day mean ratio with **today's volume
  ÷ 20-day average volume** — only credits volume on green (up) days
  to avoid rewarding panic selling.
- Buckets: `rvol > 2.0` → 15 pts, `≥ 1.5` → 10, `≥ 1.0` → 7, else 0
  (must be on an up day).
- `details["rvol"]` + `details["rvol_up_day"]` populated; legacy
  `details["vol_ratio"]` is kept (mirrors `rvol`) for back-compat with
  `_format_momentum_for_prompt` and the rec log. `_vol_label(rvol,
  up_day)` renders "RVOL 1.45× (Above Avg, ↑)".

**Sector cap** (`MAX_POSITIONS_PER_SECTOR = 3`)
- `handle_buy` looks up the sector via yfinance `info.get("sector")`,
  counts how many existing positions share it, and refuses the buy
  with a clear "sector cap reached: N/3 in <Sector>" message if a 4th
  would be added.
- `find_replacement_after_exit` also pre-filters out any sector
  already at the cap so a post-sell rescan never recommends something
  the user couldn't actually `/buy`.

**Cash bookkeeping** (`cash.json`, `load_cash` / `save_cash` / `adjust_cash`)
- Atomic, lock-guarded (`_cash_lock`) JSON file. Bootstrapped on first
  read to `INITIAL_CASH` ($5000) — for an existing portfolio that had
  no `cash.json`, the user can just edit the file directly.
- `handle_buy` deducts `shares × price` from cash (and warns, but does
  not block, if the result is negative — the user is the source of truth).
- `handle_sell` credits `shares × exit_price` back before removing the
  position so the next `/cash` reflects reality.

**`/cash` command** (`handle_cash`)
- Shows total portfolio value (cash + market value of holdings),
  invested cost basis, idle cash, and — when idle cash >
  `IDLE_CASH_THRESHOLD` ($500) — a 💡 nudge to park it in **BIL** or
  **SGOV** (≈5% yield, daily liquidity) with the estimated annual
  income.

**Immediate replacement scan after exit** (`find_replacement_after_exit`)
- `handle_sell` (full close only) spawns a daemon thread that
  `time.sleep(RESCAN_DELAY_SECONDS)` (5 min) then runs the scan: top
  100 S&P 500 tickers → `passes_buy_screen` → `score_momentum_bulk` →
  highest score ≥ 65 not in current portfolio and not in any
  capped sector. Sends a 💡 *REPLACEMENT OPPORTUNITY* message OR a
  🛑 *NO REPLACEMENT FOUND* + BIL/SGOV nudge if nothing qualifies.
  The 5-min delay gives the market a moment to settle after the exit
  print before scanning.

### Reliability
- **`_send_chunked(text, chat_id, parse_mode)`** — splits any message >
  4000 chars on `\n` boundaries, with a hard mid-line fallback for
  pathological cases. Used by every multi-line aggregator handler
  (`/portfolio`, `/health`, `/cash`, `/performance`, `/review`,
  `/earnings`, replacement scanner).
- **"Analyzing… please wait" pre-msg** — sent immediately by the poll
  loop before kicking the slow handlers (`/portfolio`, `/earnings`,
  `/health`, `/performance`, `/review`) to a daemon thread so the
  Telegram long-poll never blocks > 1s. `/cash` is fast enough to skip
  the pre-msg but still backgrounded for safety.
- **Per-stock try/except** — every loop in `handle_portfolio`,
  `handle_earnings`, `find_replacement_after_exit`, and
  `check_gap_down` wraps the per-ticker work in try/except so one bad
  yfinance / Alpha Vantage fetch can't crash the entire command.

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
- **Stop-loss trigger line** — when `apply_position_rules` flags
  `stop_breached`, the per-position message includes a dedicated
  `⛔ STOP LOSS BREACHED — current $X ≤ stop $Y (-7% from entry)` line
  ABOVE the P&L line so the trigger can't be missed.
- **Scheduled-only urgent alerts** (`scheduled=True` from `scheduled_run`):
  - 🚨 SELL ALERT — sent immediately for any position whose final signal
    is SELL. Two distinct alert texts based on which trigger fired:
    - Stop-loss breach → `Stop loss breached — current $X ≤ stop $Y (P&L -X% from entry)`
    - Momentum < 35    → `Momentum score collapsed to X/100 (< 35 SELL threshold)`
  - ⚠️ WARNING — sent for any position whose score dropped > 20 pts vs
    yesterday (catches sudden momentum collapses BEFORE they hit the SELL
    threshold).
  Both alerts are sent BEFORE the regular per-position messages so they
  surface to the top of the user's notifications.
- `/buy` separately runs `score_momentum` in a background thread on the
  new position, persists the initial score to `portfolio.json`, and logs
  the rec with `momentum_score_at_recommendation`.

### Commands
`/buy` (sector-capped), `/sell` (auto-rescan after 5 min), `/trim`,
`/portfolio`, `/cash`, `/health` (with sector breakdown), `/earnings`,
`/deep TICKER`, `/monthly`, `/review`, `/performance`, `/help`

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
