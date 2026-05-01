# Portfolio Bot v2.0 — Momentum System

A Telegram bot for active stock portfolio management using a 100-point momentum scoring system, trailing stop losses, and a monthly buy screen across the S&P 500 + Nasdaq 100.

## What it does

- **Momentum scoring (0–100)** across 5 components: price momentum (30pts), relative strength vs SPY (20pts), RVOL volume confirmation (15pts), news sentiment (20pts), earnings momentum (15pts)
- **Daily signals**: STRONG BUY ≥80 / BUY ≥65 / HOLD ≥50 / WATCH ≥35 / SELL <35
- **Signal confirmation**: SELL only fires after 2 consecutive sub-35 days OR a >30pt single-day collapse
- **Trailing stop loss**: ATR-based, -5% (low vol) / -7% (normal) / -10% (high vol), tightened to -3% when score 35–49
- **Hard disqualification rules**: blocks BUY if stock is near/above analyst target, estimates are falling, RVOL < 0.5, or 4-week return > 35%
- **Monthly screen**: scans 500+ stocks (S&P 500 + Nasdaq 100 combined), applies fundamental filter, ranks by momentum
- **Sector cap**: enforces max positions per sector
- **Feedback loop**: logs every recommendation for 4-week and 8-week grading vs SPY

## Telegram commands

| Command | Description |
|---|---|
| `/buy TICKER SHARES PRICE` | Add a position |
| `/sell TICKER [SHARES]` | Sell all or partial |
| `/trim TICKER` | Sell 50% of a position |
| `/portfolio` | Full momentum breakdown for every holding |
| `/deep TICKER` | Single-stock deep dive with disqualification check |
| `/monthly` | Run the S&P 500 + Nasdaq 100 buy screen |
| `/earnings` | Upcoming earnings (next 30 days) |
| `/health` | Portfolio health score 0–10 |
| `/cash` | Cash balance and treasury allocation suggestion |
| `/review` | Open recommendations + countdown to review dates |
| `/performance` | Track record vs S&P 500 |

## Required environment variables

Set these in Railway → Variables:

| Variable | Description |
|---|---|
| `TELEGRAM_TOKEN` | Bot token from @BotFather |
| `ANTHROPIC_API_KEY` | Claude API key for signal reasoning |
| `NEWS_API_KEY` | NewsAPI key for news sentiment |
| `FINNHUB_API_KEY` | Finnhub key for earnings data |

> **Note**: `ALPHA_VANTAGE_KEY` is optional (fallback data source).

## Deploying on Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
3. Select this repository
4. Railway will auto-detect the `Procfile`
5. Go to **Variables** and add all 4 environment variables above
6. Railway will start the worker automatically

The bot uses a **worker** process (not a web server), so no port binding is needed on Railway. The internal Flask server on port 5000 is used only for the `/ping` health check.

## Data files

The bot stores state in JSON files in the project root:

| File | Contents |
|---|---|
| `portfolio.json` | All open positions with score history |
| `cash.json` | Cash balance |
| `monthly_picks.json` | Monthly screen picks with dates |
| `signal_log.json` | Per-ticker signal history |
| `recommendations_log.json` | Full rec history for performance tracking |

## Project structure

```
portfolio-bot/
  main.py          # The entire bot (5100+ lines)
  dashboard.py     # Web dashboard (port 8082)
  requirements.txt # Python dependencies
Procfile           # Railway/Heroku worker definition
```
