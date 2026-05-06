"""Portfolio Bot — a Telegram-controlled portfolio assistant.

Commands you can send the bot:
  /buy TICKER SHARES PRICE   add a position (e.g. /buy AAPL 10 189.50)
  /sell TICKER [SHARES]      sell entire position, or only N shares
  /trim TICKER               sell 50% of a position (quick partial exit)
  /portfolio                 unified daily view — every position + signal
  /deep TICKER               full momentum analysis on any single stock
  /monthly                   run the monthly S&P 500 + Nasdaq 100 buy screen
  /earnings                  list upcoming earnings (next 30 days)
  /health                    portfolio health score (0–10) with breakdown
  /market                    current market status (open/closed, ET time, holiday check)
  /settings                  view or change bot settings (delivery time, on/off)
    /settings delivery on|off   toggle automatic daily delivery
    /settings time HH:MM        set delivery time in UTC (e.g. /settings time 08:30)

Signal engine — 100-point momentum score per stock:
  Price momentum (30) + RS vs SPY (20) + volume confirm (15)
    + news sentiment (20) + earnings momentum (15)
  Thresholds: ≥80 STRONG BUY / ≥65 BUY / ≥50 HOLD / ≥35 WATCH / <35 SELL

Daily schedule (UTC):
  08:00 — earnings calendar sweep (alerts for earnings ≤ 3 days away)
  08:30 — morning portfolio health score push
  09:00 — unified /portfolio momentum review. Sends a confirmed SELL
          alert (score < 35 for 2 consecutive days OR >30-pt collapse),
          suppresses SELL for stocks within the 5-day monthly-screen
          protection window, and shows a Signal stability indicator
          (Stable / Changing) per position. Score history (last 5 days)
          is stored per position in portfolio.json.

Fundamentals are now used ONLY as a filter for the monthly buy screen
(rev growth > 10%, profit margin > 0, D/E < 100, fwd PE < 40 etc.) — they
no longer drive any buy/sell signal directly. The 25-point Claude
framework is no longer part of the signal pipeline.
"""

import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from threading import Thread
from zoneinfo import ZoneInfo

# This file is launched as `python3 portfolio-bot/main.py`, so its
# module name in sys.modules is "__main__". Without this alias, any
# helper module that does `from main import ...` would cause Python
# to RE-EXECUTE this entire file as a separate "main" module — which
# would re-register every schedule.every() job, duplicate the Flask
# app instance, and split the global state. Aliasing here ensures
# every importer gets the one and only running instance.
sys.modules.setdefault("main", sys.modules[__name__])

import anthropic
import requests
import schedule
import yfinance as yf
from flask import Flask

BOT_VERSION = "2.0"
BOT_VERSION_LABEL = "Portfolio Bot v2.0 - Momentum System"
print(f"[boot] {BOT_VERSION_LABEL} loading...")

# Hard trade rules (always override fundamentals)
STOP_LOSS_PCT = -7.0              # default trailing stop (normal volatility)
STOP_LOSS_PCT_HIGH_VOL = -10.0    # ATR > 4% — wider stop avoids whipsaw
STOP_LOSS_PCT_LOW_VOL = -5.0      # ATR < 2% — tighter stop for low-vol names
WATCH_TIGHTENED_STOP_PCT = -3.0   # score 35-49 → tighten to -3% from current
ATR_HIGH_VOL_THRESHOLD = 4.0      # ATR% above this → high-vol bucket
ATR_LOW_VOL_THRESHOLD = 2.0       # ATR% below this → low-vol bucket
ATR_RECALC_DAYS = 7               # weekly ATR refresh in /portfolio
ABOVE_TARGET_FRACTION = 1.00      # forced SELL when price ≥ analyst target
APPROACH_TARGET_FRACTION = 0.90   # soft alert when price ≥ 90% of target
LOW_TARGET_FRACTION = 0.70        # info: price ≤ 70% of target → big upside

# Sector concentration cap (per spec: max 3 positions per sector).
MAX_POSITIONS_PER_SECTOR = 3

# Cash management — initial cash, idle threshold, treasury suggestion.
INITIAL_CASH = 5000.0
IDLE_CASH_THRESHOLD = 500.0
CASH_FILE = "cash.json"

# Immediate rescan-after-exit: wait this long before scanning for a
# replacement (lets the user see the sell alert first, and lets the
# market settle a bit if the exit was triggered intraday).
RESCAN_DELAY_SECONDS = 300        # 5 minutes

# Signal confirmation — prevents daily flipping on momentum signals.
SCORE_HISTORY_DAYS = 5             # rolling window of daily scores kept per position
SELL_CONFIRM_DROP = 30             # score drops > 30 pts in one day → confirm immediately
MONTHLY_BUY_PROTECTION_CAL_DAYS = 7  # ≈5 trading days: suppress non-stop SELL after
                                      # a stock is recommended by the monthly screen
SELL_SIGNAL_BLACKOUT_DAYS = 7     # days to exclude a ticker from the monthly
                                   # BUY screen after it generated a SELL signal


# ---------------------------------------------------------------------------
# Market calendar — NYSE trading day + hours awareness
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")

# NYSE observed holidays 2025-2027.  Add new years at the end each January.
_NYSE_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01",
    "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
    "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
    "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
    "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
    "2027-11-25", "2027-12-24",
}


def is_market_holiday(d=None):
    """True if *d* (date, default today ET) is an NYSE holiday."""
    if d is None:
        d = datetime.now(_ET).date()
    return d.strftime("%Y-%m-%d") in _NYSE_HOLIDAYS


def is_trading_day(d=None):
    """True if *d* is Mon–Fri and not an NYSE holiday."""
    if d is None:
        d = datetime.now(_ET).date()
    return d.weekday() < 5 and not is_market_holiday(d)


def is_market_open(dt=None):
    """True if NYSE is currently open (9:30–16:00 ET on a trading day)."""
    if dt is None:
        dt = datetime.now(_ET)
    if not is_trading_day(dt.date()):
        return False
    open_t  = dt.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = dt.replace(hour=16, minute=0,  second=0, microsecond=0)
    return open_t <= dt < close_t


def market_status_line():
    """Short human-readable market status string for Telegram messages."""
    now_et = datetime.now(_ET)
    d = now_et.date()
    day_name = now_et.strftime("%A")
    date_str = now_et.strftime("%b %d")
    if d.weekday() >= 5:
        return f"⏸ Market closed — {day_name} (weekend)"
    if is_market_holiday(d):
        return f"⏸ Market closed — {day_name} {date_str} (US market holiday)"
    open_t  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_t = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now_et < open_t:
        mins = int((open_t - now_et).total_seconds() // 60)
        h, m = divmod(mins, 60)
        eta = f"{h}h {m}m" if h else f"{m}m"
        return f"⏰ Pre-market — opens in {eta} (9:30 AM ET)"
    if now_et >= close_t:
        return f"🔔 After-hours — closed at 4:00 PM ET"
    return f"🟢 Market open — {now_et.strftime('%I:%M %p')} ET"


def _skip_if_not_trading_day(label):
    """Print a skip notice and return True when today is not a trading day.
    Used as a guard at the top of every scheduled job."""
    if not is_trading_day():
        print(f"[{label}] skipping — not a trading day "
              f"({market_status_line()})")
        return True
    return False


def _status_emoji(pl_pct):
    """Three-state P&L emoji.

    🟢 green  — current price ≥ entry (positive P&L)
    🟡 yellow — below entry but still above the -7% stop loss
    🔴 red    — at/below the stop loss (urgent action needed)
    """
    if pl_pct >= 0:
        return "🟢"
    if pl_pct > STOP_LOSS_PCT:
        return "🟡"
    return "🔴"


def _safe_target(value):
    """Return value if it is a positive number, else None.

    yfinance occasionally returns 0, NaN, or None for targetMeanPrice on
    obscure tickers; those values would crash percentage math, so we
    normalise them to None here and skip target-based rules downstream.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    # Guard against NaN (NaN != NaN) and non-positive values.
    if v != v or v <= 0:
        return None
    return v

# Buy-screen filters (tighter — quality businesses only)
SCREEN_MIN_REVENUE_GROWTH = 0.15     # > 15%
SCREEN_MAX_RECOMMENDATION = 2.0      # strong analyst conviction
SCREEN_MAX_FORWARD_PE = 25.0         # not expensive
SCREEN_MIN_PROFIT_MARGIN = 0.15      # 15% minimum
SCREEN_MIN_ROE = 0.15                # ROE > 15%
SCREEN_MIN_EARNINGS_GROWTH = 0.10    # > 10%

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
ALPHA_VANTAGE_KEY = os.environ.get("ALPHA_VANTAGE_KEY")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")

# How many days before earnings we send a heads-up alert.
EARNINGS_ALERT_DAYS = 3
# Within this window the alert is upgraded from ⚠️ → 🚨 URGENT.
URGENT_EARNINGS_DAYS = 2
# Maximum lookahead window for the manual /earnings command.
EARNINGS_LOOKAHEAD_DAYS = 30
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
# Free-tier AV is 5 requests/minute. Space calls 13s apart to stay safe.
ALPHA_VANTAGE_MIN_INTERVAL_SEC = 13.0

PORTFOLIO_FILE = "portfolio.json"   # where positions are stored
LAST_CHAT_FILE = "last_chat.json"   # remembers chat for scheduled reports
RECS_LOG_FILE = "recommendations_log.json"   # feedback-loop history
MONTHLY_PICKS_FILE = "monthly_picks.json"    # {ticker: ISO-date} of last monthly rec
SIGNAL_LOG_FILE = "signal_log.json"          # {ticker: last_sell_signal_date} log
SETTINGS_FILE = "bot_settings.json"          # user-configurable bot settings

# Review windows for recommendation tracking (calendar days).
REVIEW_4W_DAYS = 28
REVIEW_8W_DAYS = 56

NEWS_ENDPOINT = "https://newsapi.org/v2/everything"


# ---------------------------------------------------------------------------
# Flask web server — portfolio summary page + uptime ping
# Runs on port 8080 so the Replit proxy can reach it at the root path.
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/ping")
def ping():
    """Uptime-monitoring endpoint — always returns 200 OK."""
    return "OK", 200


@app.route("/")
def home():
    """Simple portfolio summary page — reads portfolio.json and cash.json
    directly so the page loads fast without any yfinance calls."""
    try:
        portfolio = load_portfolio()
        cash_data = load_cash()
        cash = float(cash_data.get("cash", 0.0))
    except Exception:
        portfolio = {}
        cash = 0.0

    rows = []
    total_cost = 0.0
    for ticker, pos in portfolio.items():
        shares = pos.get("shares", 0)
        entry = pos.get("entry_price", 0)
        cost = shares * entry
        total_cost += cost
        peak = pos.get("peak_price") or entry
        atr_pct = pos.get("atr_stop_pct", -7)
        stop = round(peak * (1 + atr_pct / 100), 2)
        score = pos.get("momentum_score")
        score_str = f"{score}/100" if score is not None else "—"
        sector = pos.get("sector") or "—"
        rows.append((ticker, shares, entry, cost, peak, stop, score_str, sector))

    total_value = total_cost + cash
    num_pos = len(portfolio)

    table_rows = "".join(
        f"<tr>"
        f"<td><b>{t}</b></td>"
        f"<td>{sh:.4g}</td>"
        f"<td>${ep:.2f}</td>"
        f"<td>${cos:.2f}</td>"
        f"<td>${pk:.2f}</td>"
        f"<td>${st:.2f}</td>"
        f"<td>{sc}</td>"
        f"<td>{sec}</td>"
        f"</tr>"
        for t, sh, ep, cos, pk, st, sc, sec in rows
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Portfolio Bot</title>
<style>
  body {{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
         margin:0;padding:24px}}
  h1 {{color:#38bdf8;margin:0 0 4px}}
  .sub {{color:#94a3b8;font-size:.85rem;margin-bottom:24px}}
  .cards {{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px}}
  .card {{background:#1e293b;border-radius:8px;padding:16px 24px;min-width:140px}}
  .card-label {{color:#94a3b8;font-size:.75rem;text-transform:uppercase;
                letter-spacing:.05em}}
  .card-value {{font-size:1.5rem;font-weight:700;color:#f8fafc;margin-top:4px}}
  table {{width:100%;border-collapse:collapse;background:#1e293b;
          border-radius:8px;overflow:hidden}}
  th {{background:#0f172a;color:#94a3b8;font-size:.75rem;text-transform:uppercase;
       letter-spacing:.05em;padding:10px 14px;text-align:left}}
  td {{padding:10px 14px;border-top:1px solid #1e3a5f;font-size:.9rem}}
  tr:hover td {{background:#263548}}
  .ping {{position:fixed;bottom:12px;right:16px;color:#475569;font-size:.7rem}}
</style>
</head>
<body>
<h1>📈 Portfolio Bot</h1>
<p class="sub">Auto-refreshes every 60 s &nbsp;·&nbsp;
  Prices shown are entry prices — run /portfolio in Telegram for live data</p>
<div class="cards">
  <div class="card">
    <div class="card-label">Positions</div>
    <div class="card-value">{num_pos}</div>
  </div>
  <div class="card">
    <div class="card-label">Cost Basis</div>
    <div class="card-value">${total_cost:,.2f}</div>
  </div>
  <div class="card">
    <div class="card-label">Idle Cash</div>
    <div class="card-value">${cash:,.2f}</div>
  </div>
  <div class="card">
    <div class="card-label">Total Value</div>
    <div class="card-value">${total_value:,.2f}</div>
  </div>
</div>
<table>
  <thead>
    <tr>
      <th>Ticker</th><th>Shares</th><th>Entry</th><th>Cost</th>
      <th>Peak</th><th>Stop</th><th>Score</th><th>Sector</th>
    </tr>
  </thead>
  <tbody>{table_rows if table_rows else
    '<tr><td colspan="8" style="text-align:center;color:#475569">No positions</td></tr>'
  }</tbody>
</table>
<div class="ping">
  <a href="/ping" style="color:#475569">/ping</a>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


def keep_alive():
    """Run the Flask server. Started in a background thread.

    Port selection (in priority order):
      1. PORT env var (set by Replit workflow config)
      2. 5000 (default / fallback)
    """
    port = int(os.environ.get("PORT", 5000))
    print(f"[web] portfolio summary server starting on port {port}")
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------------------------------------------------------------------
# Tiny JSON file helpers (used for the portfolio and the last-chat memo)
# ---------------------------------------------------------------------------
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# User settings (bot_settings.json)
# ---------------------------------------------------------------------------
_SETTINGS_DEFAULTS = {
    "daily_delivery": True,        # send /portfolio automatically each day
    "delivery_time_utc": "09:00",  # HH:MM UTC — when to send it
    "timezone_display": "ET",      # label shown in market status lines
}


def load_settings():
    data = load_json(SETTINGS_FILE, {})
    return {**_SETTINGS_DEFAULTS, **data}


def save_settings(data):
    save_json(SETTINGS_FILE, data)


def load_portfolio():
    return load_json(PORTFOLIO_FILE, {})


# ---------------------------------------------------------------------------
# Cash bookkeeping (cash.json)
# ---------------------------------------------------------------------------
_cash_lock = threading.Lock()


def _save_cash_unlocked(data):
    """Atomic write — assumes _cash_lock is already held."""
    tmp = CASH_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CASH_FILE)


def load_cash():
    """Return ``{"cash": float}``. Bootstraps cash.json on first call by
    deducting the cost of every existing position from the seed
    ``INITIAL_CASH`` ($5000) so the user's existing portfolio has a
    sensible starting balance.
    """
    with _cash_lock:
        if not os.path.exists(CASH_FILE):
            try:
                portfolio = load_portfolio()
                invested = sum(
                    float(p.get("shares", 0)) * float(p.get("entry_price", 0))
                    for p in portfolio.values()
                )
            except Exception:
                invested = 0.0
            data = {"cash": round(max(INITIAL_CASH - invested, 0.0), 2)}
            _save_cash_unlocked(data)
            return data
        try:
            return load_json(CASH_FILE, {"cash": 0.0})
        except Exception:
            return {"cash": 0.0}


def save_cash(data):
    with _cash_lock:
        _save_cash_unlocked(data)


def adjust_cash(delta):
    """Atomically add ``delta`` (can be negative) to the cash balance.

    Returns the new balance. Guarded by ``_cash_lock`` so concurrent
    /buy and /sell can't race.
    """
    with _cash_lock:
        if not os.path.exists(CASH_FILE):
            # Bootstrap inside the lock so we never double-init.
            try:
                portfolio = load_portfolio()
                invested = sum(
                    float(p.get("shares", 0)) * float(p.get("entry_price", 0))
                    for p in portfolio.values()
                )
            except Exception:
                invested = 0.0
            data = {"cash": round(max(INITIAL_CASH - invested, 0.0), 2)}
        else:
            try:
                data = load_json(CASH_FILE, {"cash": 0.0})
            except Exception:
                data = {"cash": 0.0}
        new_cash = round(float(data.get("cash", 0.0)) + float(delta), 2)
        data["cash"] = new_cash
        _save_cash_unlocked(data)
        return new_cash


# ---------------------------------------------------------------------------
# Chunked Telegram sender — splits messages > 4000 chars on \n boundaries.
# ---------------------------------------------------------------------------
def _send_chunked(text, chat_id, parse_mode="Markdown", max_chars=4000):
    """Send a (possibly long) message, splitting into ≤max_chars chunks at
    line boundaries when needed. Telegram's hard cap is 4096 chars — we
    target 4000 to leave headroom for parser quirks. Lines longer than
    max_chars are hard-split mid-line.
    """
    if text is None:
        return
    if len(text) <= max_chars:
        send_telegram(text, chat_id, parse_mode=parse_mode)
        return

    chunks = []
    current = []
    current_len = 0
    for line in text.split("\n"):
        if len(line) > max_chars:
            if current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            for i in range(0, len(line), max_chars):
                chunks.append(line[i:i + max_chars])
            continue
        added = len(line) + (1 if current else 0)
        if current_len + added > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += added
    if current:
        chunks.append("\n".join(current))

    for c in chunks:
        send_telegram(c, chat_id, parse_mode=parse_mode)


# ---------------------------------------------------------------------------
# Volatility (ATR) + trailing stop helpers
# ---------------------------------------------------------------------------
def calculate_atr(ticker, hist=None, period=14):
    """14-day Average True Range as a percentage of current price.

    True Range for a bar = max(high-low, |high-prev_close|, |low-prev_close|).
    ATR% = (mean of last `period` TRs) / current_close * 100.

    Returns ``(atr_pct, current_close)`` or ``(None, None)`` on failure.
    Accepts a pre-fetched ``hist`` DataFrame (>= period+1 rows) so callers
    that already have history (e.g. /portfolio) don't re-pay the
    yfinance round-trip.
    """
    try:
        if hist is None:
            hist = yf.Ticker(ticker).history(period="2mo")
        if hist is None or len(hist) < period + 1:
            return None, None
        # Walk the last (period+1) bars in plain Python so we don't need
        # a hard pandas dependency at the module top — yfinance ships
        # pandas under the hood but importing it here would be one more
        # surface to break on environment changes.
        h = hist.tail(period + 1)
        highs = h["High"].tolist()
        lows = h["Low"].tolist()
        closes = h["Close"].tolist()
        true_ranges = []
        for i in range(1, len(closes)):
            hi = float(highs[i])
            lo = float(lows[i])
            prev = float(closes[i - 1])
            tr = max(hi - lo, abs(hi - prev), abs(lo - prev))
            true_ranges.append(tr)
        if not true_ranges:
            return None, None
        atr = sum(true_ranges) / len(true_ranges)
        current = float(closes[-1])
        if current <= 0:
            return None, None
        return round((atr / current) * 100.0, 2), current
    except Exception as exc:
        print(f"[atr {ticker}] failed: {exc}")
        return None, None


def get_atr_stop_pct(atr_pct):
    """Return the trailing-stop percentage (negative) for the given
    ATR%: -10 for high-vol, -5 for low-vol, -7 default.
    """
    if atr_pct is None:
        return STOP_LOSS_PCT
    if atr_pct > ATR_HIGH_VOL_THRESHOLD:
        return STOP_LOSS_PCT_HIGH_VOL
    if atr_pct < ATR_LOW_VOL_THRESHOLD:
        return STOP_LOSS_PCT_LOW_VOL
    return STOP_LOSS_PCT


def compute_trailing_stop(pos, current_price=None):
    """Return ``(stop_price, mode)`` for an owned position.

    ``mode`` is one of:
      - ``"tightened"`` — score is in the watch zone; ``pos`` carries a
        snapshot ``tightened_stop_price`` taken at the moment the
        position transitioned into the watch zone (-3% from current).
      - ``"trailing"`` — normal mode; stop = peak_price × (1 + atr_stop_pct/100).

    The peak / atr_stop_pct fields are backfilled by callers that have
    fresh data; for a position missing them we fall back gracefully to
    entry_price + the default -7% stop so legacy positions still work.
    """
    if pos.get("tightened_stop") and pos.get("tightened_stop_price"):
        return float(pos["tightened_stop_price"]), "tightened"

    entry = float(pos.get("entry_price", 0) or 0)
    peak = float(pos.get("peak_price", entry) or entry)
    atr_stop_pct = float(pos.get("atr_stop_pct", STOP_LOSS_PCT))
    factor = 1.0 + (atr_stop_pct / 100.0)
    stop = peak * factor if peak > 0 else 0.0
    return stop, "trailing"


def save_portfolio(portfolio):
    save_json(PORTFOLIO_FILE, portfolio)


# ---------------------------------------------------------------------------
# Signal-log helpers — persist SELL signals so the monthly screen can
# avoid re-recommending a stock that just generated a SELL.
# ---------------------------------------------------------------------------
def load_signal_log():
    return load_json(SIGNAL_LOG_FILE, {})


def save_signal_log(log):
    save_json(SIGNAL_LOG_FILE, log)


def record_sell_signal(ticker, date_iso):
    """Stamp ticker with the latest SELL-signal date in signal_log.json."""
    try:
        log = load_signal_log()
        log[ticker] = {"last_sell_signal_date": date_iso}
        save_signal_log(log)
    except Exception as exc:
        print(f"[signal-log] failed to record SELL for {ticker}: {exc}")


# ---------------------------------------------------------------------------
# Monthly-picks helpers — track which tickers the monthly screen
# recommended so /buy can grant them the 5-day SELL-protection window.
# ---------------------------------------------------------------------------
def load_monthly_picks():
    return load_json(MONTHLY_PICKS_FILE, {})


def save_monthly_picks(picks):
    save_json(MONTHLY_PICKS_FILE, picks)


# ---------------------------------------------------------------------------
# Score-history helpers — rolling 5-day window per position.
# ---------------------------------------------------------------------------
def _update_score_history(pos, score, today_iso):
    """Append today's score to pos['score_history']; keep last SCORE_HISTORY_DAYS."""
    if score is None:
        return
    history = pos.get("score_history") or []
    # Avoid duplicate entry for today.
    if history and history[-1].get("date") == today_iso:
        history[-1]["score"] = score
    else:
        history.append({"date": today_iso, "score": score})
    pos["score_history"] = history[-SCORE_HISTORY_DAYS:]


# ---------------------------------------------------------------------------
# Signal-confirmation helpers — prevent daily flipping.
# ---------------------------------------------------------------------------
def _cal_days_since(date_str):
    """Calendar days since an ISO date string. Returns 9999 on any error."""
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
        return max(0, (datetime.utcnow().date() - d).days)
    except Exception:
        return 9999


def _is_confirmed_sell(score, pos):
    """Return True if a momentum-based SELL (score < 35) is confirmed.

    Confirmation rules (OR logic — any one is sufficient):
      1. Dramatic collapse: score dropped > SELL_CONFIRM_DROP points since
         the previous stored score.
      2. Two consecutive sub-35 days: previous stored score was also < 35.

    A stop-loss breach is always immediate and never needs confirmation —
    the caller handles that case before reaching here.
    """
    if score is None or score >= 35:
        return False

    prev_score = pos.get("momentum_score")
    if prev_score is None:
        # No baseline yet — don't confirm on first observation.
        return False

    # Rule 1: dramatic single-day collapse.
    if (prev_score - score) > SELL_CONFIRM_DROP:
        return True

    # Rule 2: second consecutive sub-35 day.
    if prev_score < 35:
        return True

    return False


def _in_monthly_buy_protection(pos):
    """Return True if this position is within the monthly-screen buy
    protection window (≈5 trading days ≈ MONTHLY_BUY_PROTECTION_CAL_DAYS
    calendar days). During this window non-stop SELL signals are suppressed.
    """
    date_str = pos.get("monthly_buy_date")
    if not date_str:
        return False
    return _cal_days_since(date_str) < MONTHLY_BUY_PROTECTION_CAL_DAYS


def _derive_signal_from_score(score):
    """Derive the named signal string from a raw momentum score."""
    if score is None:
        return "WATCH"
    if score >= 80:
        return "STRONG BUY"
    if score >= 65:
        return "BUY"
    if score >= 50:
        return "HOLD"
    if score >= 35:
        return "WATCH"
    return "SELL"


def remember_chat(chat_id):
    """Persist the most recent chat ID so the scheduled job knows where to send."""
    save_json(LAST_CHAT_FILE, {"chat_id": chat_id})


def recall_chat():
    return load_json(LAST_CHAT_FILE, {}).get("chat_id")


# ---------------------------------------------------------------------------
# Telegram messaging
# ---------------------------------------------------------------------------
def send_telegram(message, chat_id, parse_mode="Markdown"):
    """Send a message to a Telegram chat. Pass parse_mode=None for plain
    text — the unified /portfolio per-position messages do this so that
    Claude-generated reason text containing `_`, `*`, `[` etc. cannot
    cause Telegram to silently reject the message (`ok: false`).

    Retries up to 3 times on network errors with a 2-second back-off.
    Errors are always logged and never propagated — daemon threads must
    not crash silently on a transient Telegram connectivity blip.
    """
    import time as _time
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    snippet = str(message)[:80].replace("\n", " ")
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=30)
            try:
                rj = resp.json()
                if not rj.get("ok"):
                    print(f"[telegram] attempt {attempt} FAILED ok=false: {rj.get('description')} | msg={snippet!r}")
                    # 429 = rate-limited → wait and retry; other errors → give up
                    if rj.get("error_code") == 429:
                        retry_after = rj.get("parameters", {}).get("retry_after", 5)
                        _time.sleep(retry_after)
                        continue
                    return  # non-retriable API error
            except Exception:
                pass
            return  # success (or unparse-able response — assume delivered)
        except Exception as exc:
            print(f"[telegram] attempt {attempt} network error: {exc} | msg={snippet!r}")
            if attempt < 3:
                _time.sleep(2)
    print(f"[telegram] all 3 attempts failed for msg={snippet!r}")


# ---------------------------------------------------------------------------
# Stock data helpers
# ---------------------------------------------------------------------------
def get_current_price(ticker):
    """Latest close price for a single ticker, or None if unavailable."""
    try:
        data = yf.Ticker(ticker).history(period="2d")
        if data.empty:
            return None
        return round(float(data["Close"].iloc[-1]), 2)
    except Exception:
        return None


def get_sp500_tickers():
    """Return ALL S&P 500 tickers from the canonical public dataset CSV.

    Also used by ``find_replacement_after_exit`` (top-100 slice) and as one
    half of ``get_screening_universe``.
    """
    url = (
        "https://raw.githubusercontent.com/datasets/"
        "s-and-p-500-companies/main/data/constituents.csv"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req, timeout=20)
    lines = response.read().decode().split("\n")[1:]
    tickers = [line.split(",")[0].strip() for line in lines if line.strip()]
    return [t for t in tickers if re.match(r"^[A-Z]{1,5}$", t)]


def get_nasdaq100_tickers():
    """Return Nasdaq-100 tickers scraped from the Wikipedia constituents table.

    The table is table index 4 on the Nasdaq-100 Wikipedia page.  First cell
    of each data row is the ticker symbol.  Retried once on transient failure.
    """
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(2):
        try:
            html = urllib.request.urlopen(req, timeout=20).read().decode(
                "utf-8", errors="replace"
            )
            tables = re.findall(
                r"<table[^>]*wikitable[^>]*>(.*?)</table>", html, re.DOTALL
            )
            # Table 4 (0-indexed) is the 100-component constituents list.
            if len(tables) < 5:
                raise ValueError(
                    f"Expected ≥5 wikitables on Nasdaq-100 page, found {len(tables)}"
                )
            t = tables[4]
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", t, re.DOTALL)
            tickers = []
            for row in rows[1:]:   # skip header row
                m = re.search(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
                if m:
                    raw = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                    if re.match(r"^[A-Z]{1,5}$", raw):
                        tickers.append(raw)
            if tickers:
                return tickers
        except Exception as exc:
            print(f"[nasdaq100] attempt {attempt + 1} failed: {exc}")
    return []


def get_screening_universe():
    """Combine S&P 500 + Nasdaq 100, deduplicate, and return with stats.

    Returns:
        (tickers, sp500_count, ndq100_count, overlap_count)

    Falls back gracefully if one list is unavailable — the universe will just
    contain whichever list succeeded.
    """
    sp500: list[str] = []
    ndq100: list[str] = []

    try:
        sp500 = get_sp500_tickers()
        print(f"[universe] S&P 500: {len(sp500)} tickers")
    except Exception as exc:
        print(f"[universe] S&P 500 fetch failed: {exc}")

    try:
        ndq100 = get_nasdaq100_tickers()
        print(f"[universe] Nasdaq 100: {len(ndq100)} tickers")
    except Exception as exc:
        print(f"[universe] Nasdaq 100 fetch failed: {exc}")

    sp500_set = set(sp500)
    ndq100_set = set(ndq100)
    overlap = sp500_set & ndq100_set
    combined = list(dict.fromkeys(sp500 + [t for t in ndq100 if t not in sp500_set]))

    return combined, len(sp500), len(ndq100), len(overlap)


# Field set we care about from yfinance Ticker.info
FUNDAMENTAL_FIELDS = (
    "trailingPE",
    "forwardPE",
    "revenueGrowth",
    "earningsGrowth",
    "profitMargins",
    "debtToEquity",
    "returnOnEquity",
    "targetMeanPrice",
    "recommendationMean",
    "sector",
    # Used by the daily monitor to compute the single-day move % so we
    # can trigger deep analysis on big movers (>3%) and target alerts
    # on bigger movers (>5%).
    "regularMarketPreviousClose",
)


def get_fundamentals(ticker):
    """Fetch a fundamentals snapshot for a single ticker via yf.Ticker.info.

    Returns a dict with the fields in FUNDAMENTAL_FIELDS plus 'currentPrice'.
    Missing values are returned as None.
    """
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception:
        return None
    snap = {field: info.get(field) for field in FUNDAMENTAL_FIELDS}
    snap["ticker"] = ticker
    snap["currentPrice"] = info.get("currentPrice") or info.get(
        "regularMarketPrice"
    )
    return snap


# --- Rich fundamentals (deep analysis pipeline) ------------------------------
RICH_FUNDAMENTAL_FIELDS = (
    # Price & analyst targets
    "currentPrice", "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "numberOfAnalystOpinions", "recommendationMean",
    # Valuation
    "forwardPE", "trailingPE", "pegRatio",
    "priceToSalesTrailing12Months", "priceToBook", "enterpriseToEbitda",
    # Growth
    "revenueGrowth", "earningsGrowth",
    "earningsQuarterlyGrowth", "revenueQuarterlyGrowth",
    # Margins
    "grossMargins", "operatingMargins", "profitMargins",
    # Health
    "debtToEquity", "currentRatio", "freeCashflow",
    "returnOnEquity", "returnOnAssets",
    # Ownership
    "heldPercentInsiders", "heldPercentInstitutions",
    # Classification
    "sector", "industry", "longName", "shortName",
)


def _analyst_conviction(target_high, target_low, target_mean):
    """Tighter analyst price-target spread → higher conviction (0–10).

    Formula from the spec: 10 - ((high - low) / mean * 10), clamped [0, 10].
    Returns None when any input is missing/invalid.
    """
    high = _safe_float(target_high)
    low = _safe_float(target_low)
    mean = _safe_target(target_mean)
    if not (high and low and mean) or high < low or mean <= 0:
        return None
    spread_ratio = (high - low) / mean
    return max(0.0, min(10.0, 10.0 - spread_ratio * 10.0))


def _earnings_trend(ticker):
    """Look at the last 4 reported quarterly EPS and classify the trend.

    Returns dict with keys: trend ("accelerating" | "decelerating" | "mixed"
    | "insufficient" | "unavailable"), recent_eps (list of last 4 floats,
    chronological), growth_rates (list of 3 q/q growth rates).
    """
    out = {"trend": "unavailable", "recent_eps": [], "growth_rates": []}
    try:
        ed = yf.Ticker(ticker).get_earnings_dates(limit=12)
        if ed is None or ed.empty or "Reported EPS" not in ed.columns:
            return out
        reported = ed.dropna(subset=["Reported EPS"]).sort_index()
        eps = []
        for v in reported["Reported EPS"].tolist():
            sv = _safe_float(v)
            if sv is not None:
                eps.append(sv)
        eps = eps[-4:]
        out["recent_eps"] = eps
        if len(eps) < 4:
            out["trend"] = "insufficient"
            return out
        rates = []
        for i in range(3):
            prev, curr = eps[i], eps[i + 1]
            if prev == 0:
                # Can't compute % growth; treat as mixed.
                out["trend"] = "mixed"
                return out
            rates.append((curr - prev) / abs(prev))
        out["growth_rates"] = rates
        if rates[0] < rates[1] < rates[2]:
            out["trend"] = "accelerating"
        elif rates[0] > rates[1] > rates[2]:
            out["trend"] = "decelerating"
        else:
            out["trend"] = "mixed"
    except Exception as exc:
        print(f"[rich] {ticker}: earnings trend lookup failed: {exc}")
    return out


def get_rich_fundamentals(ticker):
    """Pull the full 'deep analysis' fundamentals snapshot for one ticker.

    Returns a superset of the basic snapshot plus computed fields:
      - analyst_conviction (0–10 from target-price spread)
      - earnings_trend     ("accelerating" / "decelerating" / "mixed" / ...)
      - recent_eps         (last 4 reported quarterly EPS)
      - growth_rates       (q/q growth rates between those 4 quarters)
    Returns None on total failure.
    """
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as exc:
        print(f"[rich] {ticker}: info fetch failed: {exc}")
        return None
    snap = {field: info.get(field) for field in RICH_FUNDAMENTAL_FIELDS}
    snap["ticker"] = ticker
    if not snap.get("currentPrice"):
        snap["currentPrice"] = info.get("regularMarketPrice")
    snap["analyst_conviction"] = _analyst_conviction(
        snap.get("targetHighPrice"),
        snap.get("targetLowPrice"),
        snap.get("targetMeanPrice"),
    )
    trend = _earnings_trend(ticker)
    snap["earnings_trend"] = trend["trend"]
    snap["recent_eps"] = trend["recent_eps"]
    snap["growth_rates"] = trend["growth_rates"]
    return snap


def fetch_rich_fundamentals_bulk(tickers, max_workers=8):
    """Parallel rich-fundamentals fetch. Returns {ticker: snap}."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_rich_fundamentals, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                snap = fut.result()
            except Exception:
                snap = None
            if snap:
                results[t] = snap
    return results


def rich_fundamentals_block(rich):
    """Render a rich-fundamentals dict as a structured prompt block."""
    if not rich:
        return "(no fundamentals data)"

    def f(key, suffix=""):
        v = rich.get(key)
        return format_fund(v, suffix) if v is not None else "n/a"

    eps_str = (
        ", ".join(f"{e:.2f}" for e in rich.get("recent_eps", []))
        or "n/a"
    )
    conviction = rich.get("analyst_conviction")
    conv_str = (
        f"{conviction:.1f}/10" if conviction is not None else "n/a"
    )
    return (
        f"{rich.get('ticker')} ({f('sector')} / {f('industry')}):\n"
        f"  Price: ${f('currentPrice')}  |  "
        f"Target mean/high/low: ${f('targetMeanPrice')} / "
        f"${f('targetHighPrice')} / ${f('targetLowPrice')}  "
        f"(N={f('numberOfAnalystOpinions')}, conviction {conv_str})\n"
        f"  Valuation: trailing PE {f('trailingPE')}, fwd PE "
        f"{f('forwardPE')}, PEG {f('pegRatio')}, "
        f"P/S {f('priceToSalesTrailing12Months')}, "
        f"P/B {f('priceToBook')}, EV/EBITDA {f('enterpriseToEbitda')}\n"
        f"  Growth: revenue {f('revenueGrowth')}, earnings "
        f"{f('earningsGrowth')}, qrtly rev {f('revenueQuarterlyGrowth')}, "
        f"qrtly earn {f('earningsQuarterlyGrowth')}\n"
        f"  Margins: gross {f('grossMargins')}, op {f('operatingMargins')}, "
        f"net {f('profitMargins')}\n"
        f"  Health: D/E {f('debtToEquity')}, current ratio "
        f"{f('currentRatio')}, FCF {f('freeCashflow')}, "
        f"ROE {f('returnOnEquity')}, ROA {f('returnOnAssets')}\n"
        f"  Ownership: insider {f('heldPercentInsiders')}, institutional "
        f"{f('heldPercentInstitutions')}\n"
        f"  Analyst: recommendationMean {f('recommendationMean')} "
        f"(lower = more bullish)\n"
        f"  Last 4 quarterly EPS: [{eps_str}]  →  "
        f"earnings trend: {rich.get('earnings_trend', 'unavailable')}"
    )


# --- Headline sentiment heuristic --------------------------------------------
# Lightweight keyword classifier — not as accurate as an LLM, but free, fast,
# and deterministic. Word boundaries via \b so we don't false-match inside
# unrelated words ("win" inside "winter").
_NEG_PATTERN = re.compile(
    r"\b(miss|missed|misses|cut|cuts|loss|losses|scandal|fraud|"
    r"lawsuit|downgrade|downgraded|resign|resigns|resigned|fired|"
    r"decline|declines|declined|fall|falls|fell|fallen|drop|drops|"
    r"dropped|plunge|plunges|plunged|warn|warns|warning|weak|"
    r"weakness|disappoint|disappoints|disappointing|disappointed|"
    r"layoff|layoffs|breach|breaches|probe|probed|investigation|"
    r"delay|delays|delayed|recall|recalls|recalled|shortfall|"
    r"slump|slumps|slumped|tumble|tumbles|tumbled|sinks|crashed)\b",
    re.IGNORECASE,
)
_POS_PATTERN = re.compile(
    r"\b(beat|beats|beaten|exceed|exceeds|exceeded|raise|raised|"
    r"raises|upgrade|upgraded|upgrades|surge|surges|surged|rally|"
    r"rallies|rallied|record|strong|stronger|strongest|accelerate|"
    r"accelerates|accelerated|accelerating|acceleration|expand|"
    r"expands|expanded|expansion|partnership|wins|won|contract|"
    r"breakthrough|profit|profits|profitable|outperform|outperforms|"
    r"outperformed|soar|soars|soared|jump|jumps|jumped|milestone|"
    r"surpass|surpasses|surpassed|launches|approval)\b",
    re.IGNORECASE,
)


def headline_sentiment(text):
    """Return -1 (negative), 0 (neutral), or +1 (positive) for a headline."""
    if not text:
        return 0
    pos = len(_POS_PATTERN.findall(text))
    neg = len(_NEG_PATTERN.findall(text))
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0


def sentiment_label(score):
    """Map a numeric sentiment score (int or avg float) to an emoji label."""
    if score > 0.2:
        return "🟢 positive"
    if score < -0.2:
        return "🔴 negative"
    return "🟡 neutral"


def get_stock_news(ticker, days=3, page_size=10):
    """Fetch recent NewsAPI headlines for a ticker.

    Queries both the ticker symbol AND company name to maximise recall.
    Each article dict carries:
      title, description, source, publishedAt, url, sentiment

    Returns a list of dicts. Empty list on any error or missing key.
    """
    if not NEWS_API_KEY:
        return []
    # Use a broader query: ticker symbol plus common name variants.
    # e.g. "ORCL" alone rarely matches; "ORCL OR Oracle" is far more effective.
    _COMPANY_NAMES = {
        "ORCL": "Oracle", "MSFT": "Microsoft", "AAPL": "Apple",
        "GOOGL": "Alphabet", "GOOG": "Alphabet", "AMZN": "Amazon",
        "META": "Meta", "NVDA": "NVIDIA", "TSLA": "Tesla",
        "AMD": "AMD", "INTC": "Intel", "CRM": "Salesforce",
        "ADBE": "Adobe", "NOW": "ServiceNow", "SNOW": "Snowflake",
        "PLTR": "Palantir", "UBER": "Uber", "LYFT": "Lyft",
        "NFLX": "Netflix", "DIS": "Disney", "JPM": "JPMorgan",
        "GS": "Goldman", "BAC": "Bank of America", "V": "Visa",
        "MA": "Mastercard", "PYPL": "PayPal", "SQ": "Block",
    }
    company = _COMPANY_NAMES.get(ticker.upper())
    query = f'"{ticker}" OR "{company}"' if company else f'"{ticker}"'

    from_dt = datetime.utcnow() - timedelta(days=days)
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "apiKey": NEWS_API_KEY,
    }
    try:
        r = requests.get(NEWS_ENDPOINT, params=params, timeout=10)
        if r.status_code != 200:
            print(f"[newsapi {ticker}] HTTP {r.status_code}: {r.text[:120]}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"[newsapi {ticker}] request failed: {exc}")
        return []
    out = []
    for art in (data.get("articles") or [])[:page_size]:
        title = (art.get("title") or "").strip()
        desc = (art.get("description") or "").strip()
        out.append(
            {
                "title": title,
                "description": desc,
                "source": (art.get("source") or {}).get("name", ""),
                "publishedAt": art.get("publishedAt", "")[:10],
                "url": art.get("url", ""),
                "sentiment": headline_sentiment(title),
            }
        )
    print(
        f"[newsapi {ticker}] query={query!r}  days={days}  "
        f"returned={len(out)} articles"
    )
    return out


def get_finnhub_news(ticker, days=3):
    """Fetch company news from Finnhub (primary news source for scoring).

    Finnhub /company-news returns articles with `headline` and `summary`.
    We normalise them to {title, description, source, publishedAt} so that
    `_news_keyword_score` can process them identically to NewsAPI articles.

    Returns a list of normalised dicts. Empty list on any error / missing key.
    """
    if not FINNHUB_API_KEY:
        return []
    from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.utcnow().strftime("%Y-%m-%d")
    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={ticker}&from={from_date}&to={to_date}"
        f"&token={FINNHUB_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"[finnhub-news {ticker}] HTTP {r.status_code}: {r.text[:120]}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"[finnhub-news {ticker}] request failed: {exc}")
        return []
    out = []
    for art in (data or [])[:20]:          # cap at 20 — plenty for sentiment
        headline = (art.get("headline") or "").strip()
        summary  = (art.get("summary")  or "").strip()
        if not headline:
            continue
        out.append(
            {
                "title": headline,
                "description": summary,
                "source": art.get("source", ""),
                "publishedAt": str(art.get("datetime", ""))[:10],
                "url": art.get("url", ""),
                "sentiment": headline_sentiment(headline),
            }
        )
    print(
        f"[finnhub-news {ticker}] days={days}  raw={len(data)}  "
        f"normalised={len(out)}"
    )
    return out


def aggregate_sentiment(articles):
    """Average sentiment across a list of articles, in [-1, +1]."""
    if not articles:
        return 0.0
    return sum(a.get("sentiment", 0) for a in articles) / len(articles)


def _fetch_news_one(ticker, days):
    """Fetch news for a single ticker: Finnhub primary, NewsAPI fallback."""
    arts = get_finnhub_news(ticker, days=days)
    if not arts:
        arts = get_stock_news(ticker, days=days, page_size=10)
    return arts


def fetch_news_bulk(tickers, days=3, max_workers=10):
    """Fetch news for many tickers in parallel. Returns {ticker: [articles]}.

    Uses Finnhub as primary source (ticker-specific, 3-day window) and falls
    back to NewsAPI per ticker when Finnhub returns nothing.
    Default window changed from 1 → 3 days to match the single-ticker path.
    """
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_fetch_news_one, t, days): t for t in tickers
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results[t] = fut.result() or []
            except Exception:
                results[t] = []
    return results


def format_news_block(ticker, articles):
    """One-block summary of news for a ticker, suitable for prompts.

    Includes per-headline sentiment label and an aggregate score line so
    the LLM can weight news flow alongside fundamentals.
    """
    if not articles:
        return f"{ticker}: (no recent news) — sentiment: n/a"
    avg = aggregate_sentiment(articles)
    lines = [
        f"{ticker}: aggregate sentiment {avg:+.2f} "
        f"({sentiment_label(avg)}) across {len(articles)} headlines"
    ]
    for a in articles:
        date = a.get("publishedAt") or "?"
        src = a.get("source") or "?"
        title = a.get("title") or "(no title)"
        s = a.get("sentiment", 0)
        s_str = "POS" if s > 0 else ("NEG" if s < 0 else "NEU")
        lines.append(f"  [{s_str}] [{date}] {src}: {title}")
    return "\n".join(lines)


def fetch_fundamentals_bulk(tickers, max_workers=10):
    """Fetch fundamentals for a list of tickers in parallel."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_fundamentals, t): t for t in tickers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                snap = fut.result()
            except Exception:
                snap = None
            if snap:
                results[t] = snap
    return results


def passes_buy_screen(f):
    """Apply the six hard fundamental filters."""
    rg = f.get("revenueGrowth")
    rm = f.get("recommendationMean")
    fpe = f.get("forwardPE")
    pm = f.get("profitMargins")
    roe = f.get("returnOnEquity")
    eg = f.get("earningsGrowth")
    if any(v is None for v in (rg, rm, fpe, pm, roe, eg)):
        return False
    return (
        rg > SCREEN_MIN_REVENUE_GROWTH
        and rm < SCREEN_MAX_RECOMMENDATION
        and fpe < SCREEN_MAX_FORWARD_PE
        and pm > SCREEN_MIN_PROFIT_MARGIN
        and roe > SCREEN_MIN_ROE
        and eg > SCREEN_MIN_EARNINGS_GROWTH
    )


def check_buy_disqualifiers(ticker, score, details, fundamentals):
    """Return a list of hard disqualification reasons for a BUY recommendation.

    Each item is a dict with keys:
      label   – short machine label
      reason  – human-readable disqualification line
      fix     – what would need to change for the stock to qualify
      entry   – suggested entry price string (only for OVEREXTENDED rule)

    An empty list means the stock is clean to recommend.
    """
    disqualifiers = []

    current_px = (fundamentals or {}).get("currentPrice")
    target_px = _safe_target((fundamentals or {}).get("targetMeanPrice"))
    ret_4w = details.get("ret_4w")          # already in percent (e.g. 38.5)
    estimates_rising = details.get("estimates_rising")
    rvol = details.get("rvol")

    # ── Rule 1: near/above analyst target ────────────────────────────────────
    if current_px and target_px:
        upside_pct = (target_px - current_px) / current_px * 100
        if upside_pct < 5.0:
            fix_px = target_px * 0.90  # need target to be >5% above price
            disqualifiers.append({
                "label": "EXTENDED",
                "reason": (
                    f"EXTENDED — no upside to target  "
                    f"(current ${current_px:.2f}, target ${target_px:.2f}, "
                    f"upside {upside_pct:.1f}%)"
                ),
                "fix": (
                    f"Price would need to pull back below ~${fix_px:.2f} "
                    f"to restore 10%+ upside, or analysts raise target."
                ),
                "entry": None,
            })

    # ── Rule 2: deteriorating earnings estimates ──────────────────────────────
    if estimates_rising is False:
        disqualifiers.append({
            "label": "DETERIORATING_ESTIMATES",
            "reason": "DETERIORATING ESTIMATES — earnings revisions are negative",
            "fix": "Wait for analyst estimate revisions to turn positive.",
            "entry": None,
        })

    # ── Rule 3: low relative volume ───────────────────────────────────────────
    if rvol is not None and rvol < 0.5:
        disqualifiers.append({
            "label": "LOW_CONVICTION",
            "reason": f"LOW CONVICTION — weak volume (RVOL {rvol:.2f}, need ≥ 0.5)",
            "fix": "Wait for volume to confirm the move (RVOL above 0.5 on an up-day).",
            "entry": None,
        })

    # ── Rule 4: extended after a big run (4-week return > 35%) ───────────────
    if ret_4w is not None and ret_4w > 35.0:
        if current_px:
            lo_entry = current_px * 0.85
            hi_entry = current_px * 0.90
            entry_str = f"${lo_entry:.2f} – ${hi_entry:.2f}"
        else:
            entry_str = "10–15% below current"
        disqualifiers.append({
            "label": "OVEREXTENDED",
            "reason": (
                f"OVEREXTENDED — up {ret_4w:.1f}% in 4 weeks; "
                "wait for a pullback"
            ),
            "fix": (
                f"Wait for the stock to consolidate. "
                f"Suggested entry zone: {entry_str} (−10–15%)."
            ),
            "entry": entry_str,
        })

    return disqualifiers


def format_buy_disqualifier_block(ticker, score, dqs):
    """Render a ⚠️ DISQUALIFIED block for Telegram (plain text)."""
    lines = [f"⚠️ {ticker} — DISQUALIFIED  (Score {score}/100)"]
    for dq in dqs:
        lines.append(f"  ✗ {dq['reason']}")
    lines.append("")
    lines.append("What would need to change:")
    for dq in dqs:
        lines.append(f"  • {dq['fix']}")
    return "\n".join(lines)


def format_fund(value, suffix=""):
    """Pretty-print a fundamental value (or 'n/a')."""
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.2f}{suffix}"
    return f"{value}{suffix}"


def fundamentals_line(ticker, f):
    """One-line readable summary of a fundamentals dict."""
    return (
        f"{ticker}: PE(t/f)={format_fund(f.get('trailingPE'))}/"
        f"{format_fund(f.get('forwardPE'))}, "
        f"revGrowth={format_fund(f.get('revenueGrowth'))}, "
        f"earnGrowth={format_fund(f.get('earningsGrowth'))}, "
        f"margin={format_fund(f.get('profitMargins'))}, "
        f"D/E={format_fund(f.get('debtToEquity'))}, "
        f"ROE={format_fund(f.get('returnOnEquity'))}, "
        f"target=${format_fund(f.get('targetMeanPrice'))}, "
        f"rec={format_fund(f.get('recommendationMean'))}"
    )


# ---------------------------------------------------------------------------
# Claude analysis — daily portfolio review (HOLD / SELL with fundamentals)
# ---------------------------------------------------------------------------
FRAMEWORK_INSTRUCTIONS = """You are a senior fundamental equity analyst at a top hedge fund with a
short term 1–6 month investment horizon. The client has a $5,000 portfolio
and invests $300–$500 per position.

For EACH ticker below, score it on this exact 5-pillar framework:

BUSINESS QUALITY (1-5):
- Competitive moat? Management credibility? Durable business model?

GROWTH TRAJECTORY (1-5):
- Are revenue/earnings growth ACCELERATING or DECELERATING quarter over
  quarter (look at the quarterly trend, not just the annual number).
- Are margins expanding or contracting?

VALUATION (1-5):
- PEG ratio below 2 (growth at reasonable price)?
- Forward P/E vs sector?
- Analyst target conviction (tight high–low spread = high conviction).

CATALYST (1-5):
- What specific event in the next 1–6 months could move this stock?
  (earnings, product launch, sector rotation, macro shift)

RISK (1-5):
- The single biggest risk to the thesis. Company specific or macro?

FINAL RECOMMENDATION:
- Total score / 25
- Signal: STRONG BUY (20-25) / BUY (15-19) / HOLD (10-14) / SELL (<10)
- Suggested position size: $500 (strong buy), $300 (buy), $0 (hold/sell)
- Price target: your own estimate (do NOT just copy analyst average)
- Stop loss: your recommended level based on key support
- Time horizon: weeks until thesis plays out
- ONE paragraph bull case, ONE paragraph bear case
"""


def _format_position_for_framework(p):
    """Render one judged position (rich fundamentals + P/L) for the prompt."""
    rich = p.get("fundamentals") or {}
    target = p.get("analyst_target")
    target_str = f"${target:.2f}" if target else "n/a"
    return (
        f"=== {p['ticker']} (POSITION) ===\n"
        f"  Holding: {p['shares']} shares @ entry ${p['entry_price']:.2f}, "
        f"current ${p['current_price']:.2f}, P/L {p['pl_pct']:+.2f}%, "
        f"stored analyst target {target_str}\n"
        f"{rich_fundamentals_block(rich)}"
    )


def analyze_portfolio_with_claude(judged_positions, news_map):
    """5-pillar framework review of every non-forced portfolio position.

    `judged_positions` already has the hard stop-loss / above-target rules
    applied. We send the rich fundamentals + sentiment-tagged news in ONE
    Claude call and ask for the framework output per ticker plus an URGENT
    ALERTS block (consumed by parse_urgent_alerts in run_analysis).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    positions_text = "\n\n".join(
        _format_position_for_framework(p) for p in judged_positions
    )
    forced_text = "\n".join(
        f"{p['ticker']}: {p['forced_signal']} — {p['forced_reason']}"
        for p in judged_positions
        if p.get("forced_signal")
    ) or "(none)"
    news_text = "\n\n".join(
        format_news_block(p["ticker"], news_map.get(p["ticker"], []))
        for p in judged_positions
    ) or "(no news available)"

    prompt = f"""{FRAMEWORK_INSTRUCTIONS}

CURRENT PORTFOLIO POSITIONS (rich fundamentals follow):
{positions_text}

POSITIONS ALREADY MARKED SELL BY HARD RULES (do not change these,
just acknowledge them in the recommendation as 'forced sell'):
{forced_text}

RECENT NEWS (last 24h, NewsAPI headlines, with sentiment):
{news_text}

Apply the 5-pillar framework above to EACH non-forced position. Update
the HOLD/SELL recommendation accordingly. For positions currently held,
"BUY" / "STRONG BUY" should be interpreted as HOLD (we already own it);
"HOLD" stays HOLD; "SELL" means SELL. Make the action explicit on the
"Signal" line.

ALSO scan the news for any CRITICAL negative event for any holding:
  - accounting scandal or fraud
  - earnings / guidance cut, missed forecast
  - CEO/CFO resignation or termination
  - regulatory action, lawsuit with material impact
  - major data breach or operational failure
List each in an URGENT ALERTS section. If none, write
"URGENT ALERTS: (none)".

Format the reply EXACTLY like this:

PORTFOLIO REVIEW:

=== TICKER ===
Business Quality: X/5 — note
Growth Trajectory: X/5 — note
Valuation: X/5 — note
Catalyst: X/5 — note
Risk: X/5 — note
Total: XX/25
Signal: HOLD or SELL (framework rating: STRONG BUY/BUY/HOLD/SELL)
Position: $X | Target: $X | Stop: $X | Horizon: N weeks
Bull case: one paragraph.
Bear case: one paragraph.

(repeat for each position; keep entries concise)

URGENT ALERTS:
TICKER — short description of the critical event (one line each)
(or "(none)")

Be direct and grounded in the numbers shown."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(
        (block.text for block in message.content if hasattr(block, "text")),
        "No analysis available",
    )


def parse_urgent_alerts(claude_text):
    """Extract URGENT ALERTS lines from Claude's reply.

    Returns a list of strings (one per alert, without the URGENT prefix).
    Returns [] if the section is absent or contains "(none)".
    """
    if not claude_text or "URGENT ALERTS" not in claude_text.upper():
        return []
    # Split on the URGENT ALERTS marker (case-insensitive)
    upper = claude_text.upper()
    idx = upper.index("URGENT ALERTS")
    block = claude_text[idx:]
    # Drop the header line
    lines = block.splitlines()[1:]
    alerts = []
    for raw in lines:
        line = raw.strip().lstrip("-").lstrip("•").strip()
        if not line:
            continue
        if line.lower().startswith("(none)") or line.lower() == "none":
            continue
        # Stop if Claude started a new section
        if line.endswith(":") and line.upper() == line:
            break
        alerts.append(line)
    return alerts


# ---------------------------------------------------------------------------
# Claude analysis — monthly buy screen
# ---------------------------------------------------------------------------
def pick_monthly_buys_with_claude(rich_candidates, news_map):
    """Run the 5-pillar framework on filtered candidates and pick the top 2.

    `rich_candidates` is a list of rich-fundamentals dicts (already passed
    the hard fundamental screen). `news_map` is {ticker: [articles]} for
    the last 7 days, with sentiment attached to each headline.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    candidates_text = "\n\n".join(
        rich_fundamentals_block(c) for c in rich_candidates
    )
    news_text = "\n\n".join(
        format_news_block(c["ticker"], news_map.get(c["ticker"], []))
        for c in rich_candidates
    ) or "(no news available)"

    # After 10+ closed recs, inject the bot's own historical performance
    # so Claude can weight the most predictive factors more heavily.
    track_record = get_track_record_for_prompt()

    prompt = f"""{FRAMEWORK_INSTRUCTIONS}{track_record}

The candidates below have ALREADY passed a hard fundamental filter
(revenue growth > 15%, earnings growth > 10%, profit margin > 15%,
ROE > 15%, forward PE < 25, recommendationMean < 2.0).

CANDIDATES (rich fundamentals follow):
{candidates_text}

RECENT NEWS (last 7 days, with sentiment):
{news_text}

Apply the 5-pillar framework to EACH candidate. Bad news (scandal,
guidance cut, executive departure, fraud) DISQUALIFIES a candidate
even if its fundamentals look great. Then PICK THE TOP 2 with the
highest framework totals (must be BUY or STRONG BUY → score >= 15).

Format the reply EXACTLY like this:

PER-CANDIDATE FRAMEWORK SCORES:

=== TICKER ===
Business Quality: X/5 — note
Growth Trajectory: X/5 — note
Valuation: X/5 — note
Catalyst: X/5 — note
Risk: X/5 — note
Total: XX/25 → SIGNAL

(repeat for each candidate; one short note per pillar)

MONTHLY BUY PICKS:

1) TICKER — STRONG BUY or BUY — Position: $500 or $300
   Target: $X | Stop: $Y | Horizon: N weeks
   Bull case: one paragraph.
   Bear case: one paragraph.

2) TICKER — STRONG BUY or BUY — Position: $500 or $300
   Target: $X | Stop: $Y | Horizon: N weeks
   Bull case: one paragraph.
   Bear case: one paragraph.

If fewer than 2 candidates score >= 15/25, output only the qualifying
one(s) and add a final line: "No second pick this month."
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(
        (block.text for block in message.content if hasattr(block, "text")),
        "No analysis available",
    )


# ---------------------------------------------------------------------------
# Claude analysis — single-stock deep dive (used by /deep TICKER)
# ---------------------------------------------------------------------------
def analyze_stock_deep(ticker, rich, position=None, news=None):
    """Run the full 5-pillar framework on a single ticker.

    `rich`     – rich-fundamentals dict from get_rich_fundamentals(ticker)
    `position` – optional dict (the user's holding) for position context
    `news`     – optional list of articles (with sentiment) for the ticker
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    pos_text = "(not currently held)"
    if position:
        entry = position.get("entry_price")
        shares = position.get("shares")
        target = _safe_target(position.get("analyst_target"))
        target_str = f"${target:.2f}" if target else "n/a"
        cur = rich.get("currentPrice")
        pl = (
            ((cur - entry) / entry) * 100
            if (cur and entry)
            else None
        )
        pl_str = f"{pl:+.2f}%" if pl is not None else "n/a"
        pos_text = (
            f"Currently HELD — {shares} shares @ entry ${entry:.2f}, "
            f"P/L {pl_str}, stored analyst target {target_str}"
        )

    news_text = format_news_block(ticker, news or [])

    prompt = f"""{FRAMEWORK_INSTRUCTIONS}

TICKER UNDER REVIEW: {ticker}
Position context: {pos_text}

RICH FUNDAMENTALS:
{rich_fundamentals_block(rich)}

RECENT NEWS (last 7 days, with sentiment):
{news_text}

Apply the 5-pillar framework to {ticker}. Be specific and grounded in
the numbers shown above (cite PEG, growth rates, conviction, sentiment,
etc.). Format your reply EXACTLY like this:

=== {ticker} — DEEP ANALYSIS ===

Business Quality: X/5 — one or two sentences
Growth Trajectory: X/5 — one or two sentences (cite the q/q EPS trend)
Valuation: X/5 — one or two sentences (cite PEG / fwd PE / conviction)
Catalyst: X/5 — one or two sentences (specific event, 1–6 month window)
Risk: X/5 — one or two sentences (single biggest risk)

Total: XX/25
Signal: STRONG BUY (20-25) / BUY (15-19) / HOLD (10-14) / SELL (<10)
Position size: $500 / $300 / $0
Price target: $X (your estimate, NOT just analyst average)
Stop loss: $X (your level based on key support)
Time horizon: N weeks

Bull case:
One paragraph, grounded in the numbers above.

Bear case:
One paragraph, grounded in the numbers above.
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(
        (block.text for block in message.content if hasattr(block, "text")),
        "No analysis available",
    )


# ---------------------------------------------------------------------------
# Momentum scoring (100-point) — primary signal engine
# ---------------------------------------------------------------------------
# Five components, all derived from price/volume/news/earnings data:
#   - Price momentum    (30 pts)  — 1w, 4w, 12w returns
#   - RS vs SPY         (20 pts)  — 4w outperformance vs the index
#   - Volume confirm    (15 pts)  — up-day vs down-day volume ratio
#   - News sentiment    (20 pts)  — keyword tally on last 10 headlines
#   - Earnings momentum (15 pts)  — last quarter beat + estimates rising
# Score thresholds → signal (see get_momentum_signal).
_POSITIVE_NEWS = (
    "beat", "surge", "growth", "record", "strong", "upgrade", "buy",
    "bullish", "profit", "raised", "exceeded", "outperform",
)
_NEGATIVE_NEWS = (
    "miss", "drop", "fall", "weak", "downgrade", "sell", "bearish",
    "loss", "cut", "below", "concern", "risk", "lawsuit",
)


def _news_keyword_score(articles):
    """Return (news_score:int, label:str) per the spec's keyword heuristic.

    Label contract:
      "No relevant news"          — no articles fetched at all
      "X positive, Y negative"    — articles were fetched; X/Y are keyword counts
                                    (both may be 0 if no signal words matched)
    This distinction lets the display layer show accurate counts vs a true
    data-absent state.
    """
    if not articles:
        return 10, "No relevant news"
    pos = neg = 0
    for a in articles:
        text = (
            ((a.get("title") or "") + " " + (a.get("description") or ""))
            .lower()
        )
        if any(w in text for w in _POSITIVE_NEWS):
            pos += 1
        if any(w in text for w in _NEGATIVE_NEWS):
            neg += 1
    total = pos + neg
    if total == 0:
        # Articles were fetched but none matched signal keywords — neutral,
        # not "no news". Show the count so the display is honest.
        return 10, f"0 positive, 0 negative ({len(articles)} articles, no signal words)"
    ratio = pos / total
    if ratio >= 0.7:
        score = 20
    elif ratio >= 0.4:
        score = 10
    else:
        score = 0
    return score, f"{pos} positive, {neg} negative"


def _finnhub_earnings_beat(ticker):
    """Query Finnhub for the most recent earnings beat.

    Returns (beat: bool | None, actual: float | None, estimate: float | None,
             period: str | None).  Returns (None, None, None, None) when the
    key is absent, the request fails, or no quarterly data is available.

    Finnhub /stock/earnings returns quarters newest-first.
    """
    if not FINNHUB_API_KEY:
        return None, None, None, None
    try:
        url = (
            f"https://finnhub.io/api/v1/stock/earnings"
            f"?symbol={ticker}&token={FINNHUB_API_KEY}"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            print(
                f"[finnhub {ticker}] earnings HTTP {resp.status_code}: "
                f"{resp.text[:120]}"
            )
            return None, None, None, None
        data = resp.json()
        if not data:
            return None, None, None, None
        latest = data[0]
        actual = latest.get("actual")
        estimate = latest.get("estimate")
        period = latest.get("period", "")
        print(
            f"[finnhub {ticker}] earnings  period={period}  "
            f"actual={actual}  estimate={estimate}  "
            f"surprise={latest.get('surprisePercent')}%"
        )
        if actual is None or estimate is None:
            return None, actual, estimate, period
        beat = float(actual) > float(estimate)
        return beat, actual, estimate, period
    except Exception as exc:
        print(f"[finnhub {ticker}] earnings fetch failed: {exc}")
        return None, None, None, None


def _finnhub_price_target(ticker):
    """Return (target_mean: float | None) from Finnhub /stock/price-target."""
    if not FINNHUB_API_KEY:
        return None
    try:
        url = (
            f"https://finnhub.io/api/v1/stock/price-target"
            f"?symbol={ticker}&token={FINNHUB_API_KEY}"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json()
        target = data.get("targetMean") or data.get("targetMedian")
        print(
            f"[finnhub {ticker}] price-target  mean={data.get('targetMean')}  "
            f"median={data.get('targetMedian')}  high={data.get('targetHigh')}  "
            f"analysts={data.get('numberOfAnalysts')}"
        )
        return float(target) if target else None
    except Exception as exc:
        print(f"[finnhub {ticker}] price-target fetch failed: {exc}")
        return None


def score_momentum(ticker, hist=None, spy_hist=None, info=None,
                   news_articles=None):
    """Compute the 100-point momentum score for one ticker.

    Returns ``(score:int|None, details:dict)``. Returns ``(None, {})`` on
    a hard failure (insufficient history, etc). All upstream inputs are
    optional — when omitted the function fetches them itself, so the
    function is usable both stand-alone (/deep, /buy) and from a bulk
    pre-fetched context (handle_portfolio, /monthly).
    """
    score = 0
    details = {}
    try:
        if hist is None:
            try:
                hist = yf.Ticker(ticker).history(period="3mo")
            except Exception as exc:
                print(f"[momentum {ticker}] history fetch failed: {exc}")
                hist = None
        if hist is None or len(hist) < 20:
            return None, {}

        if spy_hist is None:
            try:
                spy_hist = yf.Ticker("SPY").history(period="3mo")
            except Exception as exc:
                print(f"[momentum {ticker}] SPY fetch failed: {exc}")
                spy_hist = None

        current = float(hist["Close"].iloc[-1])
        week1_ago = float(
            hist["Close"].iloc[-5] if len(hist) >= 5 else hist["Close"].iloc[0]
        )
        week4_ago = float(
            hist["Close"].iloc[-20] if len(hist) >= 20 else hist["Close"].iloc[0]
        )
        week12_ago = float(hist["Close"].iloc[0])

        # ── Price momentum (30 pts) ─────────────────────────────────────
        ret_1w = (current - week1_ago) / week1_ago if week1_ago else 0
        ret_4w = (current - week4_ago) / week4_ago if week4_ago else 0
        ret_12w = (current - week12_ago) / week12_ago if week12_ago else 0
        if ret_1w > 0:
            score += 10
        if ret_4w > 0:
            score += 10
        if ret_12w > 0:
            score += 10
        details["ret_1w"] = round(ret_1w * 100, 1)
        details["ret_4w"] = round(ret_4w * 100, 1)
        details["ret_12w"] = round(ret_12w * 100, 1)

        # ── Relative strength vs SPY (20 pts) ───────────────────────────
        spy_ret_4w = 0.0
        if spy_hist is not None and not spy_hist.empty:
            spy_current = float(spy_hist["Close"].iloc[-1])
            spy_w4 = float(
                spy_hist["Close"].iloc[-20] if len(spy_hist) >= 20
                else spy_hist["Close"].iloc[0]
            )
            spy_ret_4w = (spy_current - spy_w4) / spy_w4 if spy_w4 else 0
        rs = ret_4w - spy_ret_4w
        if rs > 0.03:
            score += 20
        elif rs > 0:
            score += 10
        details["rs_vs_spy"] = round(rs * 100, 1)
        details["spy_ret_4w"] = round(spy_ret_4w * 100, 1)

        # ── Volume confirmation via RVOL (15 pts) ───────────────────────
        # RVOL = today's volume ÷ 20-day average volume. Awards points
        # only on up days (close > open) — institutional buying is what
        # we're trying to detect, not panic selling. A high RVOL on a
        # down day is bearish, so it scores 0.
        try:
            last20 = hist.tail(20)
            avg_vol_20d = float(last20["Volume"].mean()) if len(last20) > 0 else 0.0
            today_vol = float(hist["Volume"].iloc[-1])
            today_open = float(hist["Open"].iloc[-1])
            today_close = float(hist["Close"].iloc[-1])
            up_day = today_close > today_open
            if avg_vol_20d > 0:
                rvol = today_vol / avg_vol_20d
            else:
                rvol = 0.0

            vol_score = 0
            if up_day:
                if rvol > 2.0:
                    vol_score = 15
                elif rvol >= 1.5:
                    vol_score = 10
                elif rvol >= 1.0:
                    vol_score = 7
            score += vol_score

            details["rvol"] = round(rvol, 2)
            details["rvol_up_day"] = bool(up_day)
            details["rvol_score"] = vol_score
            # Backward-compat: keep the old vol_ratio key populated so
            # any callers / tests that still reference it don't break —
            # we now stuff RVOL into it. The new _vol_label inspects the
            # rvol key first and only falls back to vol_ratio.
            details["vol_ratio"] = round(rvol, 2)
        except Exception as exc:
            print(f"[momentum {ticker}] RVOL calc failed: {exc}")
            details["rvol"] = None
            details["rvol_up_day"] = None
            details["rvol_score"] = 0
            details["vol_ratio"] = None

        # ── News sentiment (20 pts) ─────────────────────────────────────
        # Primary: Finnhub /company-news (ticker-specific, 20 articles, 3 days).
        # Fallback: NewsAPI (broader query with company name alias, 10 articles).
        # Both sources normalise to {title, description} so _news_keyword_score
        # works identically on either.
        if news_articles is None:
            try:
                news_articles = get_finnhub_news(ticker, days=3)
                if not news_articles:
                    print(
                        f"[momentum {ticker}] Finnhub news empty, "
                        "falling back to NewsAPI"
                    )
                    news_articles = get_stock_news(ticker, days=3, page_size=10)
            except Exception as exc:
                print(f"[momentum {ticker}] news fetch failed: {exc}")
                news_articles = []

        news_score, news_label = _news_keyword_score(news_articles or [])

        # Detailed log so every run is auditable
        _pos = sum(
            1 for a in (news_articles or [])
            if any(
                w in ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
                for w in _POSITIVE_NEWS
            )
        )
        _neg = sum(
            1 for a in (news_articles or [])
            if any(
                w in ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
                for w in _NEGATIVE_NEWS
            )
        )
        print(
            f"[momentum {ticker}] news  articles={len(news_articles or [])}  "
            f"pos={_pos}  neg={_neg}  score={news_score}/20  label={news_label!r}"
        )
        if news_articles:
            for _a in (news_articles or [])[:5]:
                print(
                    f"  headline: {(_a.get('title') or '')[:72]}"
                )

        score += news_score
        details["news"] = news_label
        details["news_score"] = news_score
        details["_news_article_count"] = len(news_articles or [])

        # ── Earnings momentum (15 pts) ──────────────────────────────────
        # 8 pts: beat last quarter EPS estimate (Finnhub primary, yfinance fallback)
        # 7 pts: analyst price target ≥10% above current price
        # Each sub-component is wrapped so one failure never zeroes the other.
        _ts = datetime.utcnow().strftime("%H:%M:%S UTC")
        print(f"[momentum {ticker}] earnings check at {_ts}")
        earnings_score = 0
        details["earnings_beat"] = None
        details["estimates_rising"] = None

        # ── 8 pts: EPS beat ─────────────────────────────────────────────
        try:
            # PRIMARY: Finnhub (more reliable, real-time consensus data)
            fh_beat, fh_actual, fh_estimate, fh_period = _finnhub_earnings_beat(ticker)
            if fh_beat is not None:
                details["earnings_beat"] = fh_beat
                details["_earnings_source"] = "finnhub"
                details["_earnings_period"] = fh_period
                details["_earnings_actual"] = fh_actual
                details["_earnings_estimate"] = fh_estimate
                if fh_beat:
                    earnings_score += 8
            else:
                # FALLBACK: yfinance earnings_history
                # Uses epsActual / epsEstimate columns (oldest-first, iloc[-1] = newest).
                # More stable than earnings_dates whose column names have changed across
                # yfinance versions.
                print(f"[momentum {ticker}] Finnhub beat=None, falling back to yfinance")
                stock_obj = yf.Ticker(ticker)
                eh = stock_obj.earnings_history
                if (eh is not None and not eh.empty
                        and "epsActual" in eh.columns
                        and "epsEstimate" in eh.columns):
                    reported_only = eh.dropna(subset=["epsActual", "epsEstimate"])
                    if not reported_only.empty:
                        latest = reported_only.iloc[-1]   # oldest-first → last = newest
                        yf_actual = float(latest["epsActual"])
                        yf_estimate = float(latest["epsEstimate"])
                        yf_period = str(latest.name) if latest.name else ""
                        yf_beat = yf_actual > yf_estimate
                        details["earnings_beat"] = yf_beat
                        details["_earnings_source"] = "yfinance"
                        details["_earnings_period"] = yf_period
                        details["_earnings_actual"] = yf_actual
                        details["_earnings_estimate"] = yf_estimate
                        print(
                            f"[momentum {ticker}] yfinance fallback  "
                            f"period={yf_period}  actual={yf_actual}  "
                            f"estimate={yf_estimate}  beat={yf_beat}"
                        )
                        if yf_beat:
                            earnings_score += 8
        except Exception as exc:
            print(f"[momentum {ticker}] earnings_beat check failed: {exc}")

        # ── 7 pts: analyst target ≥10% upside ───────────────────────────
        try:
            # Ensure we have a usable info dict (may be None if called standalone)
            _info = info or {}
            target = None

            # PRIMARY: Finnhub price target (fresher consensus than yfinance)
            fh_target = _finnhub_price_target(ticker)
            if fh_target:
                target = fh_target
                details["_target_source"] = "finnhub"
            else:
                # FALLBACK: yfinance info dict — fetch it now if not pre-supplied
                if not _info:
                    try:
                        _yf_obj = yf.Ticker(ticker)
                        _info = _yf_obj.info or {}
                        print(
                            f"[momentum {ticker}] fetched yfinance info standalone  "
                            f"targetMeanPrice={_info.get('targetMeanPrice')}  "
                            f"currentPrice={_info.get('currentPrice')}"
                        )
                    except Exception as exc:
                        print(f"[momentum {ticker}] yfinance info fetch failed: {exc}")
                target = _info.get("targetMeanPrice") or 0
                details["_target_source"] = "yfinance"

            current_px = _info.get("currentPrice") or current
            upside_pct = ((target / current_px) - 1) * 100 if target and current_px else 0
            print(
                f"[momentum {ticker}] price-target  target={target}  "
                f"current={current_px:.2f}  upside={upside_pct:.1f}%  "
                f"qualifies={bool(target and target > current_px * 1.10)}"
            )
            if target and target > current_px * 1.10:
                earnings_score += 7
                details["estimates_rising"] = True
            else:
                details["estimates_rising"] = False
            details["_target_price"] = round(target, 2) if target else None
            details["_target_upside_pct"] = round(upside_pct, 1)
        except Exception as exc:
            print(f"[momentum {ticker}] price-target check failed: {exc}")
            details["estimates_rising"] = False

        score += earnings_score
        details["earnings_score"] = earnings_score
        print(
            f"[momentum {ticker}] earnings_score={earnings_score}  "
            f"beat={details.get('earnings_beat')}  "
            f"target_upside={details.get('_target_upside_pct')}%  "
            f"estimates_rising={details.get('estimates_rising')}"
        )

    except Exception as exc:
        print(f"[momentum {ticker}] hard failure: {exc}")
        return None, {}

    return score, details


def score_momentum_bulk(tickers, fundamentals=None, news_map=None):
    """Parallel momentum scoring for a list of tickers.

    Reuses pre-fetched ``fundamentals`` (one info dict per ticker) and
    ``news_map`` ({ticker: [articles]}) when supplied. SPY history is
    fetched once and shared across all worker threads — saves N round-trips.
    Returns ``{ticker: (score, details)}`` (excluding tickers that failed).
    """
    if not tickers:
        return {}
    try:
        spy_hist = yf.Ticker("SPY").history(period="3mo")
    except Exception as exc:
        print(f"[momentum-bulk] SPY history failed: {exc}")
        spy_hist = None

    out = {}

    def _one(t):
        info = (fundamentals or {}).get(t)
        news = (news_map or {}).get(t)
        try:
            hist = yf.Ticker(t).history(period="3mo")
        except Exception as exc:
            print(f"[momentum-bulk {t}] history fetch failed: {exc}")
            hist = None
        return t, score_momentum(
            t, hist=hist, spy_hist=spy_hist, info=info, news_articles=news,
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, res in ex.map(_one, tickers):
            if res is not None:
                out[t] = res
    return out


def get_momentum_signal(score):
    """Map a 0-100 momentum score to (signal, color_emoji).

    PURE momentum signal — no portfolio context. Used by /deep and
    /monthly where there is no entry price or stop loss to overlay.

    Thresholds (per spec):
      ≥80 STRONG BUY 🟢   ≥65 BUY 🟢   ≥50 HOLD 🟢
      ≥35 WATCH 🟡        else SELL 🔴
    A None score (insufficient data) renders as WATCH 🟡 — never trips a
    forced sell on a single bad data fetch.
    """
    if score is None:
        return "WATCH", "🟡"
    if score >= 80:
        return "STRONG BUY", "🟢"
    if score >= 65:
        return "BUY", "🟢"
    if score >= 50:
        return "HOLD", "🟢"
    if score >= 35:
        return "WATCH", "🟡"
    return "SELL", "🔴"


def apply_position_rules(score, current_price, stop_price):
    """Final SELL/HOLD/BUY rules for an OWNED position.

    Per spec — SELL fires if EITHER trigger hits, otherwise the score
    drives the bucket:

      SELL 🔴       if score < 35  OR  current ≤ stop_price
      STRONG BUY 🟢 if score ≥ 80  AND price above stop
      BUY 🟢        if score ≥ 65  AND price above stop
      HOLD 🟢       if score ≥ 50  AND price above stop
      WATCH 🟡      otherwise (score 35-50, price above stop)

    The caller computes ``stop_price`` via ``compute_trailing_stop``
    so it transparently handles trailing (peak-based), tightened
    (-3% from current in the watch zone) and ATR-adjusted (-5/-7/-10)
    behaviour. Returns ``(signal, color, stop_breached)`` so the caller
    can show WHICH trigger fired.
    """
    stop_breached = (
        current_price is not None
        and stop_price is not None
        and stop_price > 0
        and current_price <= stop_price
    )

    # Hard SELL rules — either condition trips it.
    if stop_breached:
        return "SELL", "🔴", True
    if score is not None and score < 35:
        return "SELL", "🔴", False

    # Score-based bucket for everything above the SELL line. None →
    # WATCH so a single bad data fetch never forces action.
    if score is None:
        return "WATCH", "🟡", False
    if score >= 80:
        return "STRONG BUY", "🟢", False
    if score >= 65:
        return "BUY", "🟢", False
    if score >= 50:
        return "HOLD", "🟢", False
    return "WATCH", "🟡", False


def _fallback_reason(signal, score, stop_breached=False):
    """Deterministic one-liner used when Claude is unavailable."""
    if signal == "SELL":
        if stop_breached:
            return (
                "Trailing stop loss breached — exit position to "
                "protect capital."
            )
        return "Momentum broken across price, RS, and news — exit position."
    if signal == "WATCH":
        return "Momentum weakening — tighten stop, watch for further breakdown."
    if signal == "HOLD":
        return "Momentum mixed — trend intact but RS or volume is fading."
    if signal == "BUY":
        return "Momentum confirmed across multiple components — trend healthy."
    if signal == "STRONG BUY":
        return "All five momentum components firing — strongest possible setup."
    return f"Score {score}/100 — see component breakdown above."


def _format_momentum_for_prompt(records):
    """Compact one-line-per-ticker momentum summary for the Claude prompt."""
    lines = []
    for r in records:
        d = r.get("details") or {}
        extras = []
        if r.get("stop_breached"):
            extras.append("TRAILING STOP BREACHED")
        if r.get("above_target"):
            extras.append("ABOVE ANALYST TARGET (overextended)")
        extras_str = (" | " + " | ".join(extras)) if extras else ""
        lines.append(
            f"- {r['ticker']}: signal={r['signal']}, "
            f"score={r.get('score', 'n/a')}/100, "
            f"1w={d.get('ret_1w', 'n/a')}%, 4w={d.get('ret_4w', 'n/a')}%, "
            f"12w={d.get('ret_12w', 'n/a')}%, "
            f"RS={d.get('rs_vs_spy', 'n/a')}%, "
            f"volRatio={d.get('vol_ratio', 'n/a')}, "
            f"news={d.get('news', 'n/a')}, "
            f"earnBeat={d.get('earnings_beat')}, "
            f"estRising={d.get('estimates_rising')}"
            f"{extras_str}"
        )
    return "\n".join(lines)


def _get_quick_reasons(positions_data):
    """Single batched Claude call → {ticker: one-sentence momentum reason}.

    ``positions_data`` is a list of dicts with: ticker, signal, score,
    details. On any failure (no API key, network, JSON parse error) we
    fall back to the rule-based reason from _fallback_reason() so
    /portfolio always renders.
    """
    fallback = {
        p["ticker"]: _fallback_reason(
            p["signal"], p.get("score"),
            stop_breached=p.get("stop_breached", False),
        )
        for p in positions_data
    }
    if not ANTHROPIC_API_KEY or not positions_data:
        return fallback

    prompt = (
        "You are a concise momentum-focused portfolio analyst. For each "
        "position below you have a 100-point momentum score plus a "
        "component breakdown (price returns, relative strength vs SPY, "
        "volume ratio, news sentiment, earnings momentum). Write ONE "
        "short sentence (max 16 words) per ticker explaining why the "
        "given signal is appropriate, citing the SPECIFIC weakest or "
        "strongest momentum component for that ticker.\n\n"
        "IMPORTANT — when a ticker has the flag 'TRAILING STOP BREACHED', "
        "the SELL signal is driven by the trailing stop being hit (capital "
        "protection), NOT by momentum — your sentence MUST mention the "
        "trailing stop being breached. When a ticker has the flag 'ABOVE "
        "ANALYST TARGET', mention that the stock looks overextended "
        "even if momentum is still strong.\n\n"
        "End each sentence with a period.\n\n"
        "Positions:\n" + _format_momentum_for_prompt(positions_data) + "\n\n"
        "Output STRICTLY valid JSON only (no markdown, no preamble, no "
        "trailing text). Schema:\n"
        '{"TICKER1": "reason sentence.", "TICKER2": "..."}'
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(
            (b.text for b in msg.content if hasattr(b, "text")), ""
        ).strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return fallback
        parsed = json.loads(m.group(0))
        if not isinstance(parsed, dict):
            return fallback
        out = dict(fallback)
        for k, v in parsed.items():
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
        return out
    except Exception as exc:
        print(f"[quick-reasons] Claude call failed: {exc}")
        return fallback


# ---------------------------------------------------------------------------
# Monthly buy screen — used by /monthly
# ---------------------------------------------------------------------------
def run_monthly_screen(chat_id):
    """Monthly buy screen — fundamental filter then momentum ranking.

    Pipeline:
      1. Build the screening universe: all S&P 500 + Nasdaq 100, deduplicated.
      2. Pull fundamentals for every unique ticker (one bulk parallel fetch).
      3. Apply the hard fundamental filters via ``passes_buy_screen`` —
         this only rejects bad companies, it does NOT score them.
      4. Score every survivor with ``score_momentum_bulk`` and rank.
      5. Pick the top 2 with score ≥ 65; if none qualify, send the
         "no strong momentum opportunities — hold cash" message.
      6. Log each pick to the rec log with ``momentum_score_at_recommendation``.
    The Claude 5-pillar framework call is no longer used here.
    """
    if not is_trading_day():
        send_telegram(
            f"{market_status_line()}\n\n"
            "The monthly screen uses live price momentum data — running it on a "
            "weekend or holiday would score stocks on stale Friday prices.\n"
            "Please run /monthly on a weekday when the market is open (or "
            "pre-market is fine too).",
            chat_id,
        )
        return

    send_telegram(
        "*Running monthly S&P 500 + Nasdaq 100 buy screen...*\n"
        "Building universe, applying fundamental filter + momentum ranking. "
        "This takes 3-6 minutes.",
        chat_id,
    )

    tickers, sp500_n, ndq100_n, overlap_n = get_screening_universe()
    universe_line = (
        f"Screened {len(tickers)} stocks "
        f"({sp500_n} S&P 500 + {ndq100_n} Nasdaq 100 − {overlap_n} overlap)"
    )
    print(f"[monthly] {universe_line}")

    fundamentals_map = fetch_fundamentals_bulk(tickers)
    print(
        f"[monthly] fetched fundamentals for {len(fundamentals_map)}"
        f" / {len(tickers)} tickers"
    )

    basic_candidates = [
        f for f in fundamentals_map.values() if passes_buy_screen(f)
    ]
    print(
        f"[monthly] {len(basic_candidates)} stocks pass the hard fundamental "
        f"filter"
    )
    if not basic_candidates:
        send_telegram(
            f"*Monthly Buy Screen*\n_{universe_line}_\n\n"
            "No stocks passed the fundamental filter this month.",
            chat_id,
        )
        return

    cand_tickers = [c["ticker"] for c in basic_candidates]
    cand_fundamentals = {c["ticker"]: c for c in basic_candidates}

    # Pull 7-day news in bulk so the keyword-based news component of the
    # momentum score has fresh signal without per-ticker re-fetching.
    news_map = fetch_news_bulk(cand_tickers, days=7)
    print(
        f"[monthly] fetched news for {sum(1 for v in news_map.values() if v)}"
        f" / {len(cand_tickers)} candidates"
    )

    momentum_map = score_momentum_bulk(
        cand_tickers,
        fundamentals=cand_fundamentals,
        news_map=news_map,
    )
    print(f"[monthly] scored momentum for {len(momentum_map)} candidates")

    # ── Filter: exclude tickers with a SELL signal in the last 7 days ──
    signal_log = load_signal_log()
    today_date = datetime.utcnow().date()
    def _had_recent_sell(ticker):
        entry = signal_log.get(ticker)
        if not entry:
            return False
        date_str = entry.get("last_sell_signal_date", "")
        try:
            d = datetime.strptime(date_str[:10], "%Y-%m-%d").date()
            return (today_date - d).days <= SELL_SIGNAL_BLACKOUT_DAYS
        except Exception:
            return False

    pre_filter_count = len(momentum_map)
    momentum_map = {t: v for t, v in momentum_map.items()
                    if not _had_recent_sell(t)}
    excluded = pre_filter_count - len(momentum_map)
    if excluded:
        print(f"[monthly] excluded {excluded} ticker(s) with recent SELL signal")

    # Rank by score descending, take only those ≥ 65 (BUY threshold), top 2.
    ranked = sorted(
        ((t, s, d) for t, (s, d) in momentum_map.items() if s is not None),
        key=lambda x: x[1],
        reverse=True,
    )
    qualifying = [r for r in ranked if r[1] >= 65]

    # Apply hard disqualification rules — clean picks go to BUY section,
    # disqualified ones are shown separately with reasons.
    picks = []
    disqualified_entries = []   # list of (ticker, score, details, dq_list)
    for t, s, d in qualifying:
        fund = cand_fundamentals.get(t, {}) or {}
        dqs = check_buy_disqualifiers(t, s, d, fund)
        if dqs:
            disqualified_entries.append((t, s, d, dqs))
        else:
            if len(picks) < 2:
                picks.append((t, s, d))

    if not picks and not disqualified_entries:
        top_score = ranked[0][1] if ranked else 0
        send_telegram(
            f"*Monthly Buy Screen*\n"
            f"_{universe_line}_\n"
            f"_{len(basic_candidates)} of {len(fundamentals_map)} passed the "
            f"fundamental filter._\n\n"
            f"No strong momentum opportunities this month — hold cash.\n"
            f"(Best score this month: {top_score}/100, below the 65 BUY "
            f"threshold.)",
            chat_id,
        )
        return

    dq_note = (
        f"  {len(disqualified_entries)} disqualified by hard rules."
        if disqualified_entries else ""
    )
    if picks:
        header = (
            f"*Monthly Buy Screen*\n"
            f"_{universe_line}_\n"
            f"_{len(basic_candidates)} of {len(fundamentals_map)} passed the "
            f"fundamental filter._\n"
            f"_{len(qualifying)} scored ≥ 65.  "
            f"Top {len(picks)} clean pick(s) below.{dq_note}_\n"
        )
    else:
        header = (
            f"*Monthly Buy Screen*\n"
            f"_{universe_line}_\n"
            f"_{len(basic_candidates)} of {len(fundamentals_map)} passed the "
            f"fundamental filter._\n"
            f"_{len(qualifying)} scored ≥ 65 but all were disqualified by hard rules._\n"
            f"_Hold cash — no clean BUY candidates this month._\n"
        )
    send_telegram(header, chat_id)

    # One Telegram message per clean pick with the full momentum breakdown.
    parsed_for_log = []
    for t, score, d in picks:
        signal, color = get_momentum_signal(score)
        f = cand_fundamentals.get(t, {}) or {}
        current_px = f.get("currentPrice")
        target = _safe_target(f.get("targetMeanPrice"))
        current_line = (
            f"Current price: ${current_px:.2f}" if current_px else "Current price: n/a"
        )
        target_line = (
            f"Analyst target: ${target:.2f}" if target else "Analyst target: n/a"
        )
        send_telegram(
            f"{color} {t} — {signal}\n"
            f"Momentum Score: {score}/100\n"
            f"Price: 1W {_fmt_signed_pct(d.get('ret_1w'))} | "
            f"4W {_fmt_signed_pct(d.get('ret_4w'))} | "
            f"12W {_fmt_signed_pct(d.get('ret_12w'))}\n"
            f"vs S&P 500 (4W): Stock {_fmt_signed_pct(d.get('ret_4w'))} "
            f"vs SPY {_fmt_signed_pct(d.get('spy_ret_4w'))} → "
            f"RS: {_fmt_signed_pct(d.get('rs_vs_spy'))}\n"
            f"Volume: {_vol_label(d.get('rvol'), d.get('rvol_up_day'))}\n"
            f"News: {d.get('news', 'n/a')}\n"
            f"Earnings: "
            f"{_earnings_label(d.get('earnings_beat'), d.get('estimates_rising'))}\n"
            f"{current_line} | {target_line}",
            chat_id,
            parse_mode=None,
        )
        parsed_for_log.append({
            "ticker": t,
            "signal": signal,
            "claude_target": None,
            "stop_loss": None,
            "bull_case": None,
            "bear_case": None,
        })

    # Disqualified section — show as a single grouped message.
    if disqualified_entries:
        dq_lines = ["⚠️ DISQUALIFIED — scored ≥ 65 but failed hard rules:\n"]
        for t, s, d, dqs in disqualified_entries[:5]:  # cap at 5 shown
            dq_lines.append(format_buy_disqualifier_block(t, s, dqs))
            dq_lines.append("")
        send_telegram("\n".join(dq_lines).strip(), chat_id, parse_mode=None)

    # Feedback loop — log every monthly pick so we can grade it later.
    try:
        log_recommendations(
            parsed_for_log,
            price_lookup=lambda t: (cand_fundamentals.get(t, {}) or {}).get("currentPrice"),
            analyst_lookup=lambda t: _safe_target((cand_fundamentals.get(t, {}) or {}).get("targetMeanPrice")),
            momentum_lookup=lambda t: momentum_map.get(t, (None, None))[0],
            source="monthly",
        )
    except Exception as exc:
        print(f"[recs] monthly logging failed: {exc}")

    # Record the monthly picks so /buy can grant the 5-day SELL-protection
    # window when the user actually executes the trade.
    try:
        picks_log = load_monthly_picks()
        rec_date = datetime.utcnow().strftime("%Y-%m-%d")
        for entry in parsed_for_log:
            picks_log[entry["ticker"]] = rec_date
        save_monthly_picks(picks_log)
        print(f"[monthly] recorded picks: {[e['ticker'] for e in parsed_for_log]}")
    except Exception as exc:
        print(f"[monthly] picks logging failed: {exc}")


# ===========================================================================
# Feedback loop & performance tracking
# ===========================================================================
# Every recommendation made by the bot (monthly pick, /deep, /buy, daily
# SELL signal) is appended to recommendations_log.json. A daily 07:30 UTC
# job checks open recs against their 4-week / 8-week review dates, fetches
# the realized stock return AND the SPY benchmark return for the same
# window, and marks the call CORRECT (beat SPY), INCORRECT (lagged SPY),
# or STOPPED (the stock hit its stop loss inside the window).
# ===========================================================================
_recs_lock = threading.Lock()


def load_recs():
    """Lock-guarded read so concurrent writers can't expose half-written JSON."""
    with _recs_lock:
        return load_json(RECS_LOG_FILE, [])


def save_recs(recs):
    """Atomic write via temp file + os.replace — survives crash mid-write."""
    tmp = RECS_LOG_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(recs, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, RECS_LOG_FILE)


def _today_iso():
    return datetime.utcnow().strftime("%Y-%m-%d")


def _add_days_iso(date_str, days):
    return (
        datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    ).strftime("%Y-%m-%d")


def _historical_close(ticker, date_str):
    """Close price of `ticker` on `date_str` (YYYY-MM-DD).

    Falls back to the next available trading day's close if `date_str`
    was a weekend / holiday. Returns None on any failure.
    """
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d")
        end = start + timedelta(days=7)
        hist = yf.Ticker(ticker).history(
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            return None
        return float(hist["Close"].iloc[0])
    except Exception as exc:
        print(f"[recs] historical close lookup failed for {ticker}@{date_str}: {exc}")
        return None


def _hit_stop_loss(ticker, start_date_str, end_date_str, stop_loss):
    """True if `ticker` traded at/below `stop_loss` between the two dates."""
    if not stop_loss:
        return False
    try:
        hist = yf.Ticker(ticker).history(
            start=start_date_str,
            end=_add_days_iso(end_date_str, 1),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            return False
        return float(hist["Low"].min()) <= float(stop_loss)
    except Exception:
        return False


# --- Framework output parsing -----------------------------------------------
# Claude often wraps fields in `**bold**`; the regex below tolerates that.
_FRAMEWORK_BLOCK_RE = re.compile(
    r"===\s*([A-Z][A-Z0-9.\-]{0,9})\s*(?:[—\-]\s*DEEP ANALYSIS\s*)?===\s*"
    r"(.*?)(?=^===\s*[A-Z]|^MONTHLY BUY PICKS|^URGENT ALERTS|^PER-CANDIDATE|\Z)",
    re.DOTALL | re.MULTILINE,
)
_TOTAL_RE = re.compile(r"Total:\s*\**\s*(\d+)\s*/\s*25", re.IGNORECASE)
_SIGNAL_RE = re.compile(
    r"Signal:?\s*\**\s*(STRONG\s*BUY|BUY|HOLD|SELL)",
    re.IGNORECASE,
)
_TARGET_RE = re.compile(
    r"(?:Price\s+target|Target):?\s*\**\s*\$?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_STOP_RE = re.compile(
    r"Stop(?:\s*loss)?:?\s*\**\s*\$?\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
_BULL_RE = re.compile(
    r"Bull\s*case:?\s*\**\s*\n?(.+?)(?=\n\s*\**\s*Bear\s*case|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_BEAR_RE = re.compile(
    r"Bear\s*case:?\s*\**\s*\n?(.+?)(?=^\s*===|^\s*---|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)
# Monthly picks come in "1) TICKER — SIGNAL — Position: $X" or "1." form,
# with em-dash, en-dash, or hyphen as the separator. Tolerate **bold**.
_PICK_RE = re.compile(
    r"^\s*\d+[\)\.]\s*\**\s*([A-Z][A-Z0-9.\-]{0,9})\**\s*[—–\-]+\s*\**\s*"
    r"(STRONG\s*BUY|BUY)\b(.*?)(?=^\s*\d+[\)\.]|^\s*If fewer|^\s*No second|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)


def parse_monthly_picks(text):
    """Extract the top picks section AND merge with per-candidate scores.

    Returns list of dicts with the same shape as parse_framework_blocks().
    """
    if not text:
        return []
    # Per-candidate scores live in === TICKER === blocks earlier in the text.
    score_by_ticker = {
        b["ticker"]: b for b in parse_framework_blocks(text)
    }
    out = []
    for m in _PICK_RE.finditer(text):
        ticker = m.group(1).upper()
        signal = m.group(2).upper().replace("  ", " ")
        body = m.group(3)
        target = _TARGET_RE.search(body)
        stop = _STOP_RE.search(body)
        bull = _BULL_RE.search(body)
        bear = _BEAR_RE.search(body)
        scored = score_by_ticker.get(ticker, {})
        out.append({
            "ticker": ticker,
            "total_score": scored.get("total_score"),
            "signal": signal,
            "claude_target": _parse_money(target.group(1)) if target else None,
            "stop_loss": _parse_money(stop.group(1)) if stop else None,
            "bull_case": bull.group(1).strip() if bull else None,
            "bear_case": bear.group(1).strip() if bear else None,
        })
    return out


def _parse_money(s):
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_framework_blocks(text):
    """Extract every `=== TICKER ===` framework block from Claude output.

    Returns list of dicts: {ticker, total_score, signal, claude_target,
    stop_loss, bull_case, bear_case}. Missing fields are None.
    """
    if not text:
        return []
    out = []
    for m in _FRAMEWORK_BLOCK_RE.finditer(text):
        ticker = m.group(1).upper()
        body = m.group(2)
        total = _TOTAL_RE.search(body)
        signal = _SIGNAL_RE.search(body)
        target = _TARGET_RE.search(body)
        stop = _STOP_RE.search(body)
        bull = _BULL_RE.search(body)
        bear = _BEAR_RE.search(body)
        out.append({
            "ticker": ticker,
            "total_score": int(total.group(1)) if total else None,
            "signal": signal.group(1).upper().replace("  ", " ") if signal else None,
            "claude_target": _parse_money(target.group(1)) if target else None,
            "stop_loss": _parse_money(stop.group(1)) if stop else None,
            "bull_case": bull.group(1).strip() if bull else None,
            "bear_case": bear.group(1).strip() if bear else None,
        })
    return out


def _make_rec(ticker, parsed, price, analyst_target, source,
              momentum_score=None):
    """Construct one recommendation dict.

    ``parsed`` is a flexible dict — for momentum-era recs it contains
    {signal, claude_target, stop_loss, bull_case, bear_case}. The legacy
    ``framework_score`` field is preserved for back-compat with old log
    entries but is no longer set by current code paths.
    """
    today = _today_iso()
    return {
        "id": f"{today}-{ticker}-{int(time.time())}",
        "date": today,
        "ticker": ticker,
        "source": source,
        "signal": parsed.get("signal"),
        # Kept None on new recs — no longer used as a buy/sell signal.
        "framework_score": parsed.get("total_score"),
        # NEW: primary signal source for the momentum-era system.
        "momentum_score_at_recommendation": momentum_score,
        "price_at_recommendation": price,
        "claude_target": parsed.get("claude_target"),
        "analyst_target": analyst_target,
        "stop_loss": parsed.get("stop_loss"),
        "bull_case": parsed.get("bull_case"),
        "bear_case": parsed.get("bear_case"),
        "sp500_at_recommendation": _historical_close("SPY", today),
        "status": "open",
        "review_4w_date": _add_days_iso(today, REVIEW_4W_DAYS),
        "review_8w_date": _add_days_iso(today, REVIEW_8W_DAYS),
        "review_4w_price": None, "review_4w_return": None,
        "review_4w_sp500_return": None, "review_4w_result": None,
        "review_8w_price": None, "review_8w_return": None,
        "review_8w_sp500_return": None, "review_8w_result": None,
    }


def log_recommendations(parsed_blocks, price_lookup, analyst_lookup, source,
                        signals_to_log=None, momentum_lookup=None):
    """Append rec blocks to the log, optionally filtered by signal.

    ``price_lookup(ticker)`` → price at recommendation.
    ``analyst_lookup(ticker)`` → analyst target (or None).
    ``momentum_lookup(ticker)`` → momentum score at recommendation
        (None when the rec source predates the momentum system).
    ``signals_to_log`` is a set like {"STRONG BUY", "BUY"}; None = log all.
    """
    if not parsed_blocks:
        return 0
    added = 0
    with _recs_lock:
        recs = load_recs()
        for p in parsed_blocks:
            sig = (p.get("signal") or "").upper()
            if signals_to_log is not None and sig not in signals_to_log:
                continue
            t = p["ticker"]
            momentum = (
                momentum_lookup(t) if momentum_lookup is not None else None
            )
            try:
                rec = _make_rec(
                    t, p, price_lookup(t), analyst_lookup(t), source,
                    momentum_score=momentum,
                )
            except Exception as exc:
                print(f"[recs] failed to build rec for {t}: {exc}")
                continue
            recs.append(rec)
            added += 1
        if added:
            save_recs(recs)
    print(f"[recs] logged {added} new recommendation(s) from source={source}")
    return added


# --- Daily review check (07:30 UTC) -----------------------------------------
def _classify_review_result(rec, current_price, sp500_now, window_end):
    """Return ('CORRECT'|'INCORRECT'|'STOPPED', stock_ret%, sp500_ret%).

    Returns (None, None, None) if any required input is missing — the
    review is then deferred so we don't bias win-rate with a bogus
    SPY=0% baseline.
    """
    rec_price = rec["price_at_recommendation"]
    sp_at = rec["sp500_at_recommendation"]
    stop = rec.get("stop_loss")
    if rec_price is None or current_price is None:
        return None, None, None
    if not (sp500_now and sp_at):
        # No clean benchmark → defer; we'll retry next day.
        return None, None, None
    stock_ret = ((current_price - rec_price) / rec_price) * 100
    sp_ret = ((sp500_now - sp_at) / sp_at) * 100
    # Stop-loss check supersedes the SPY comparison.
    if stop and _hit_stop_loss(rec["ticker"], rec["date"], window_end, stop):
        return "STOPPED", stock_ret, sp_ret
    if stock_ret > sp_ret:
        return "CORRECT", stock_ret, sp_ret
    return "INCORRECT", stock_ret, sp_ret


def check_recommendation_reviews(chat_id=None):
    """Daily 07:30 UTC sweep — review any recs whose 4w/8w date has arrived."""
    today = _today_iso()
    with _recs_lock:
        recs = load_recs()
        if not recs:
            return
        spy_now = _historical_close("SPY", today) or get_current_price("SPY")
        any_change = False
        for rec in recs:
            if rec.get("status") == "closed":
                continue

            # 4-week review (only fires once)
            if (
                rec.get("review_4w_result") is None
                and rec["review_4w_date"] <= today
            ):
                cur = get_current_price(rec["ticker"])
                window_end = today
                result, stock_ret, sp_ret = _classify_review_result(
                    rec, cur, spy_now, window_end,
                )
                if result is not None:
                    rec["review_4w_price"] = cur
                    rec["review_4w_return"] = round(stock_ret, 2)
                    rec["review_4w_sp500_return"] = round(sp_ret, 2)
                    rec["review_4w_result"] = result
                    any_change = True
                    if chat_id:
                        send_telegram(
                            f"📊 *4-week review — {rec['ticker']}* "
                            f"({rec['signal']} on {rec['date']})\n"
                            f"Stock: {stock_ret:+.2f}%  |  "
                            f"SPY: {sp_ret:+.2f}%  →  *{result}*",
                            chat_id,
                        )

            # 8-week review (closes the rec)
            if (
                rec.get("review_8w_result") is None
                and rec["review_8w_date"] <= today
            ):
                cur = get_current_price(rec["ticker"])
                window_end = today
                result, stock_ret, sp_ret = _classify_review_result(
                    rec, cur, spy_now, window_end,
                )
                if result is not None:
                    rec["review_8w_price"] = cur
                    rec["review_8w_return"] = round(stock_ret, 2)
                    rec["review_8w_sp500_return"] = round(sp_ret, 2)
                    rec["review_8w_result"] = result
                    rec["status"] = "closed"
                    any_change = True
                    if chat_id:
                        send_telegram(
                            f"📊 *8-week FINAL — {rec['ticker']}* "
                            f"({rec['signal']} on {rec['date']})\n"
                            f"Stock: {stock_ret:+.2f}%  |  "
                            f"SPY: {sp_ret:+.2f}%  →  *{result}* (closed)",
                            chat_id,
                        )
        if any_change:
            save_recs(recs)


# --- Performance & review commands ------------------------------------------
def _final_result(rec):
    """Use 8w result if present, otherwise 4w."""
    return rec.get("review_8w_result") or rec.get("review_4w_result")


def _final_return(rec):
    if rec.get("review_8w_return") is not None:
        return rec["review_8w_return"], rec.get("review_8w_sp500_return", 0)
    if rec.get("review_4w_return") is not None:
        return rec["review_4w_return"], rec.get("review_4w_sp500_return", 0)
    return None, None


def compute_performance_stats():
    """Return aggregated stats over CLOSED (8-week-finalized) recs only.

    Mixing 4w-interim and 8w-final returns would distort best/worst and
    avg, so the headline metrics use closed positions exclusively.
    Open + 4w-only recs still appear in `/review` and "Recent calls".
    """
    recs = load_recs()
    reviewed = [r for r in recs if r.get("status") == "closed"]
    total = len(reviewed)
    wins = sum(1 for r in reviewed if _final_result(r) == "CORRECT")
    stopped = sum(1 for r in reviewed if _final_result(r) == "STOPPED")
    avg_stock = (
        sum(_final_return(r)[0] for r in reviewed) / total if total else 0.0
    )
    avg_sp = (
        sum(_final_return(r)[1] for r in reviewed) / total if total else 0.0
    )
    best = worst = None
    for r in reviewed:
        ret, _ = _final_return(r)
        if ret is None:
            continue
        if best is None or ret > best[1]:
            best = (r["ticker"], ret, r["date"])
        if worst is None or ret < worst[1]:
            worst = (r["ticker"], ret, r["date"])

    def _winrate_for(filter_fn):
        sub = [r for r in reviewed if filter_fn(r)]
        if not sub:
            return None, 0
        w = sum(1 for r in sub if _final_result(r) == "CORRECT")
        return (w / len(sub)) * 100, len(sub)

    sb_wr = _winrate_for(lambda r: (r.get("signal") or "").upper() == "STRONG BUY")
    b_wr = _winrate_for(lambda r: (r.get("signal") or "").upper() == "BUY")
    high_wr = _winrate_for(
        lambda r: r.get("framework_score") and r["framework_score"] >= 20
    )
    mid_wr = _winrate_for(
        lambda r: r.get("framework_score")
        and 15 <= r["framework_score"] < 20
    )

    return {
        "total": total,
        "wins": wins,
        "stopped": stopped,
        "win_rate": (wins / total * 100) if total else 0.0,
        "avg_stock": avg_stock,
        "avg_sp": avg_sp,
        "best": best,
        "worst": worst,
        "strong_buy_wr": sb_wr,
        "buy_wr": b_wr,
        "high_score_wr": high_wr,
        "mid_score_wr": mid_wr,
        "all_recs": recs,
        "reviewed": reviewed,
    }


def handle_performance(chat_id):
    s = compute_performance_stats()
    if s["total"] == 0:
        send_telegram(
            "*Performance*\n_No reviewed recommendations yet._\n"
            "Recommendations are evaluated at 4 and 8 week marks.",
            chat_id,
        )
        return
    lines = [
        "*Performance — overall track record*",
        f"Reviewed: {s['total']}  |  Wins: {s['wins']}  "
        f"|  Stopped: {s['stopped']}",
        f"Win rate vs SPY: *{s['win_rate']:.1f}%*",
        f"Avg return: stock {s['avg_stock']:+.2f}%  vs  "
        f"SPY {s['avg_sp']:+.2f}%",
    ]
    if s["best"]:
        t, r, d = s["best"]
        lines.append(f"Best call: *{t}* {r:+.2f}% (rec'd {d})")
    if s["worst"]:
        t, r, d = s["worst"]
        lines.append(f"Worst call: *{t}* {r:+.2f}% (rec'd {d})")

    lines.append("")
    lines.append("*By signal type:*")

    def _fmt_wr(wr):
        if wr[0] is None:
            return "n/a"
        return f"{wr[0]:.1f}% (N={wr[1]})"
    lines.append(f"STRONG BUY: {_fmt_wr(s['strong_buy_wr'])}")
    lines.append(f"BUY: {_fmt_wr(s['buy_wr'])}")
    lines.append("")
    lines.append("*By framework score:*")
    lines.append(f"Score 20–25: {_fmt_wr(s['high_score_wr'])}")
    lines.append(f"Score 15–19: {_fmt_wr(s['mid_score_wr'])}")

    lines.append("")
    lines.append("*Recent calls (last 5):*")
    for r in s["all_recs"][-5:]:
        ret, _ = _final_return(r)
        ret_str = f"{ret:+.2f}%" if ret is not None else "open"
        result = _final_result(r) or r.get("status", "open")
        lines.append(
            f"• {r['date']} *{r['ticker']}* — {r.get('signal') or '?'} → "
            f"{result} ({ret_str})"
        )
    _send_chunked("\n".join(lines), chat_id)


def handle_review(chat_id):
    """`/review` — show every open recommendation with countdowns."""
    recs = load_recs()
    open_recs = [r for r in recs if r.get("status") != "closed"]
    if not open_recs:
        send_telegram(
            "*Open recommendations*\n_None — log is empty or all closed._",
            chat_id,
        )
        return
    today_dt = datetime.strptime(_today_iso(), "%Y-%m-%d")
    lines = [f"*Open recommendations ({len(open_recs)})*", ""]
    for r in open_recs:
        cur = get_current_price(r["ticker"])
        rec_price = r.get("price_at_recommendation")
        if cur and rec_price:
            ret = ((cur - rec_price) / rec_price) * 100
            ret_str = f"{ret:+.2f}% so far"
            cur_str = f"${cur:.2f}"
        else:
            ret_str = "n/a"
            cur_str = "n/a"
        d4 = (datetime.strptime(r["review_4w_date"], "%Y-%m-%d") - today_dt).days
        d8 = (datetime.strptime(r["review_8w_date"], "%Y-%m-%d") - today_dt).days
        d4_label = (
            r.get("review_4w_result") or
            (f"in {d4}d" if d4 > 0 else "due now" if d4 == 0 else f"{abs(d4)}d overdue")
        )
        d8_label = (
            r.get("review_8w_result") or
            (f"in {d8}d" if d8 > 0 else "due now" if d8 == 0 else f"{abs(d8)}d overdue")
        )
        lines.append(
            f"*{r['ticker']}* ({r.get('signal') or '?'}, "
            f"score {r.get('framework_score') or '?'}/25) — {r['date']}\n"
            f"  Rec ${rec_price:.2f} → now {cur_str}  |  {ret_str}\n"
            f"  4w review: {d4_label}  |  8w review: {d8_label}"
        )
    _send_chunked("\n\n".join(lines), chat_id)


# --- Weekly summary (Sunday 08:00 UTC) --------------------------------------
def weekly_performance_summary(chat_id):
    """Sunday 08:00 UTC — recap of any reviews this week + one-line Claude take."""
    recs = load_recs()
    if not recs:
        return
    today = _today_iso()
    week_ago = _add_days_iso(today, -7)
    reviewed_this_week = [
        r for r in recs
        if (r.get("review_4w_result") and r["review_4w_date"] >= week_ago and r["review_4w_date"] <= today)
        or (r.get("review_8w_result") and r["review_8w_date"] >= week_ago and r["review_8w_date"] <= today)
    ]
    s = compute_performance_stats()

    lines = ["*Weekly Performance Summary*", ""]
    if reviewed_this_week:
        lines.append("*Reviewed this week:*")
        for r in reviewed_this_week:
            result = _final_result(r) or "—"
            ret, sp = _final_return(r)
            ret_str = f"{ret:+.2f}% vs SPY {sp:+.2f}%" if ret is not None else ""
            lines.append(
                f"• *{r['ticker']}* ({r.get('signal')}, "
                f"{r['date']}) → {result}  {ret_str}"
            )
        lines.append("")
    else:
        lines.append("_No new reviews this week._")
        lines.append("")

    if s["total"] > 0:
        beating = "BEATING" if s["avg_stock"] > s["avg_sp"] else "LAGGING"
        lines.append(f"*Running win rate:* {s['win_rate']:.1f}% over {s['total']} reviews")
        lines.append(
            f"*Average return:* {s['avg_stock']:+.2f}% vs SPY "
            f"{s['avg_sp']:+.2f}% — {beating} the market"
        )

        # One-line Claude commentary
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            msg = client.messages.create(
                model="claude-opus-4-5",
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        f"In ONE sentence, comment on what these stock-picking "
                        f"results suggest about the strategy: "
                        f"{s['total']} reviewed picks, {s['win_rate']:.0f}% "
                        f"beat SPY, avg stock return {s['avg_stock']:+.2f}% vs "
                        f"SPY {s['avg_sp']:+.2f}%. STRONG BUY win rate "
                        f"{s['strong_buy_wr'][0] if s['strong_buy_wr'][0] is not None else 'n/a'}, "
                        f"BUY win rate "
                        f"{s['buy_wr'][0] if s['buy_wr'][0] is not None else 'n/a'}. "
                        f"Be direct and specific."
                    ),
                }],
            )
            commentary = next(
                (b.text for b in msg.content if hasattr(b, "text")),
                "",
            ).strip()
            if commentary:
                lines.append("")
                lines.append(f"_Claude says: {commentary}_")
        except Exception as exc:
            print(f"[weekly] commentary failed: {exc}")

    send_telegram("\n".join(lines), chat_id)


def get_track_record_for_prompt():
    """Adaptive context block for the monthly prompt — only after 10+ closes."""
    recs = load_recs()
    closed = [r for r in recs if r.get("status") == "closed"]
    if len(closed) < 10:
        return ""
    s = compute_performance_stats()
    sb = s["strong_buy_wr"][0]
    b = s["buy_wr"][0]
    high = s["high_score_wr"][0]
    mid = s["mid_score_wr"][0]

    factor = "framework score" if (
        high is not None and mid is not None and high > mid
    ) else "signal type"
    return (
        f"\n\nHISTORICAL TRACK RECORD ({len(closed)} closed recs):\n"
        f"- Overall win rate vs SPY: {s['win_rate']:.1f}%\n"
        f"- Avg return: stock {s['avg_stock']:+.2f}% vs SPY {s['avg_sp']:+.2f}%\n"
        f"- STRONG BUY win rate: {sb if sb is not None else 'n/a'}%  "
        f"|  BUY win rate: {b if b is not None else 'n/a'}%\n"
        f"- Score 20-25 win rate: {high if high is not None else 'n/a'}%  "
        f"|  Score 15-19 win rate: {mid if mid is not None else 'n/a'}%\n"
        f"Most predictive factor so far: {factor}. Weight it heavily.\n"
    )


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
def handle_buy(args, chat_id):
    # Expect: /buy AAPL 10 189.50
    try:
        ticker = args[0].upper()
        shares = float(args[1])
        price = float(args[2])
    except (IndexError, ValueError):
        send_telegram(
            "Usage: /buy TICKER SHARES PRICE\nExample: /buy AAPL 10 189.50",
            chat_id,
        )
        return

    # Fetch the analyst consensus target + sector right at buy time.
    fund = get_fundamentals(ticker)
    target = _safe_target((fund or {}).get("targetMeanPrice"))
    sector_raw = (fund or {}).get("sector")
    sector = sector_raw.strip() if isinstance(sector_raw, str) else None

    # ── Sector cap (max 3 per sector) ────────────────────────────────
    portfolio = load_portfolio()
    if sector:
        same_sector = [
            t for t, p in portfolio.items()
            if t != ticker and isinstance(p.get("sector"), str)
            and p["sector"].strip().lower() == sector.lower()
        ]
        if len(same_sector) >= MAX_POSITIONS_PER_SECTOR:
            send_telegram(
                f"⚠️ Sector cap reached — already have "
                f"{len(same_sector)} positions in *{sector}* "
                f"({', '.join(same_sector)}). "
                f"Diversify into a different sector.",
                chat_id,
            )
            return

    # ── ATR + ATR-adjusted stop ──────────────────────────────────────
    atr_pct, _ = calculate_atr(ticker)
    atr_stop_pct = get_atr_stop_pct(atr_pct)

    # ── Monthly-screen buy protection ────────────────────────────────
    # If this ticker was recommended by the most recent monthly screen,
    # stamp the position with monthly_buy_date so the daily SELL logic
    # grants it a ≈5-trading-day suppression window.
    today_iso_buy = datetime.utcnow().strftime("%Y-%m-%d")
    monthly_picks = load_monthly_picks()
    monthly_buy_date = None
    if ticker in monthly_picks:
        rec_date = monthly_picks[ticker]
        days_since = _cal_days_since(rec_date)
        if days_since <= 30:   # only within last 30 days counts as "recent"
            monthly_buy_date = today_iso_buy

    portfolio[ticker] = {
        "shares": shares,
        "entry_price": price,
        "added": datetime.utcnow().isoformat(timespec="seconds"),
        "analyst_target": target,
        # alert state for the new target-based system
        "approach_alerted": False,
        "above_alerted": False,
        # Trailing stop / sector / ATR fields.
        "peak_price": price,
        "sector": sector,
        "atr_pct": atr_pct,
        "atr_stop_pct": atr_stop_pct,
        "atr_last_calc": today_iso_buy,
        "tightened_stop": False,
        # Monthly-screen protection window (None if not a monthly pick).
        "monthly_buy_date": monthly_buy_date,
    }
    save_portfolio(portfolio)

    # ── Cash deduction ───────────────────────────────────────────────
    cost = round(shares * price, 2)
    new_cash = adjust_cash(-cost)

    # ── Confirmation message ─────────────────────────────────────────
    lines = [f"Added *{ticker}* — {shares} shares @ ${price:.2f}"]
    if target:
        upside = ((target - price) / price) * 100
        lines.append(
            f"Analyst target: ${target:.2f} ({upside:+.1f}% upside)"
        )
    else:
        lines.append("_Analyst target unavailable._")
    if sector:
        lines.append(f"Sector: {sector}")
    if atr_pct is not None:
        stop_dollar = price * (1.0 + atr_stop_pct / 100.0)
        lines.append(
            f"ATR: {atr_pct:.2f}% → trailing stop {atr_stop_pct:.0f}% "
            f"(${stop_dollar:.2f})"
        )
        if atr_pct > ATR_HIGH_VOL_THRESHOLD:
            lines.append(
                "⚠️ High volatility stock — using wider -10% stop "
                "to avoid whipsawing"
            )
        elif atr_pct < ATR_LOW_VOL_THRESHOLD:
            lines.append(
                "Low volatility stock — using tighter -5% stop"
            )
    lines.append(
        f"Cost: ${cost:,.2f} | Cash remaining: ${new_cash:,.2f}"
    )
    if monthly_buy_date:
        lines.append(
            f"🛡 Monthly-screen protection active — SELL signals suppressed "
            f"for the first {MONTHLY_BUY_PROTECTION_CAL_DAYS} calendar days "
            f"(unless trailing stop is breached or score stays below 35 for "
            f"2 consecutive days)."
        )
    if new_cash < 0:
        lines.append(
            "_⚠️ Cash balance is negative — you've over-allocated. "
            "Consider tracking actual brokerage cash separately._"
        )
    send_telegram("\n".join(lines), chat_id)

    # Feedback loop — kick off a momentum analysis on the new position so
    # the /buy decision gets logged like any other recommendation. Runs in
    # the background so the user gets the confirmation immediately. The
    # 25-point fundamental framework is no longer used here.
    def _buy_momentum_log():
        try:
            try:
                info = yf.Ticker(ticker).info
            except Exception:
                info = fund
            news = get_stock_news(ticker, days=1, page_size=10)
            score, details = score_momentum(
                ticker, info=info, news_articles=news,
            )
            if score is None:
                print(f"[/buy {ticker}] could not compute momentum — skip log")
                return
            signal, _ = get_momentum_signal(score)

            # Persist the just-computed score on the new position so the
            # daily monitor's "drop > 20pts" comparison has a baseline.
            # Race-window vs a concurrent /sell is acceptable here — the
            # next /portfolio run rewrites the score anyway.
            pf = load_portfolio()
            if ticker in pf:
                pf[ticker]["momentum_score"] = score
                save_portfolio(pf)

            send_telegram(
                f"Momentum logged for *{ticker}*: {score}/100 → {signal}",
                chat_id,
            )
            parsed = [{
                "ticker": ticker,
                "signal": signal,
                "claude_target": None,
                "stop_loss": None,
                "bull_case": None,
                "bear_case": None,
            }]
            log_recommendations(
                parsed,
                # Use the user's actual fill price as the rec baseline.
                price_lookup=lambda _t: price,
                analyst_lookup=lambda _t: target,
                momentum_lookup=lambda _t: score,
                source="buy",
            )
        except Exception as exc:
            print(f"[/buy {ticker}] momentum-log failed: {exc}")

    Thread(target=_buy_momentum_log, daemon=True).start()


def handle_sell(args, chat_id):
    # Two forms:
    #   /sell AAPL        -> sell entire position
    #   /sell AAPL 5      -> sell only 5 shares (partial)
    if not args:
        send_telegram(
            "Usage: /sell TICKER [SHARES]\n"
            "Examples:\n"
            "  /sell AAPL       (sell entire position)\n"
            "  /sell AAPL 5     (sell only 5 shares)",
            chat_id,
        )
        return

    ticker = args[0].upper()
    portfolio = load_portfolio()

    if ticker not in portfolio:
        send_telegram(f"*{ticker}* is not in your portfolio.", chat_id)
        return

    pos = portfolio[ticker]
    held = pos["shares"]
    entry = pos["entry_price"]

    # Optional second arg = shares to sell (partial)
    if len(args) >= 2:
        try:
            sell_qty = float(args[1])
        except ValueError:
            send_telegram("Shares must be a number, e.g. /sell AAPL 5", chat_id)
            return
        if sell_qty <= 0:
            send_telegram("Shares to sell must be greater than 0.", chat_id)
            return
        if sell_qty > held:
            send_telegram(
                f"You only hold {held} shares of *{ticker}* — "
                f"cannot sell {sell_qty}.",
                chat_id,
            )
            return
    else:
        sell_qty = held  # full close

    remaining = round(held - sell_qty, 6)

    # ── Cash bookkeeping — credit proceeds at the LATEST price (not
    # entry) so the cash balance reflects the actual exit value. Falls
    # back to entry price if a live quote isn't available.
    exit_price = get_current_price(ticker) or entry
    proceeds = round(sell_qty * exit_price, 2)
    new_cash = adjust_cash(proceeds)

    if remaining <= 0:
        del portfolio[ticker]
        save_portfolio(portfolio)
        send_telegram(
            f"Sold {sell_qty} *{ticker}* shares @ ~${exit_price:.2f}. "
            f"Position closed.\n"
            f"Proceeds: ${proceeds:,.2f} | Cash: ${new_cash:,.2f}",
            chat_id,
        )

        # ── Immediate rescan after exit (per spec): wait 5 minutes,
        # then look for a replacement among the top 100 S&P 500 names
        # that the user doesn't already hold. Background thread so the
        # confirmation message returns instantly.
        def _post_sell_rescan():
            try:
                time.sleep(RESCAN_DELAY_SECONDS)
                find_replacement_after_exit(chat_id, exclude={ticker})
            except Exception as exc:
                print(f"[/sell {ticker}] rescan failed: {exc}")

        Thread(target=_post_sell_rescan, daemon=True).start()
    else:
        pos["shares"] = remaining
        save_portfolio(portfolio)
        send_telegram(
            f"Sold {sell_qty} *{ticker}* shares @ ~${exit_price:.2f}. "
            f"Remaining position: {remaining} shares @ ${entry:.2f}\n"
            f"Proceeds: ${proceeds:,.2f} | Cash: ${new_cash:,.2f}",
            chat_id,
        )


def handle_trim(args, chat_id):
    # /trim AAPL  -> sells 50% of the AAPL position (quick partial exit)
    if not args:
        send_telegram(
            "Usage: /trim TICKER\n"
            "Sells 50% of your position (quick partial exit).",
            chat_id,
        )
        return

    ticker = args[0].upper()
    portfolio = load_portfolio()

    if ticker not in portfolio:
        send_telegram(f"*{ticker}* is not in your portfolio.", chat_id)
        return

    pos = portfolio[ticker]
    held = pos["shares"]
    entry = pos["entry_price"]

    # Sell half. Round to 4 decimals so the message is readable.
    sell_qty = round(held / 2, 4)
    remaining = round(held - sell_qty, 6)

    if sell_qty <= 0:
        send_telegram(
            f"*{ticker}* position is too small to trim ({held} shares).",
            chat_id,
        )
        return

    # Credit proceeds at current price BEFORE removing the position
    # (parity with /sell so cash.json stays in sync — fix per
    # architect review).
    exit_price = get_current_price(ticker) or entry
    proceeds = round(sell_qty * exit_price, 2)
    new_cash = adjust_cash(proceeds)

    if remaining <= 0:
        del portfolio[ticker]
        save_portfolio(portfolio)
        send_telegram(
            f"Trimmed *{ticker}* — sold {sell_qty} shares (full position) "
            f"@ ${exit_price:.2f} → +${proceeds:.2f}.\n"
            f"Cash balance: ${new_cash:.2f}",
            chat_id,
        )
    else:
        pos["shares"] = remaining
        save_portfolio(portfolio)
        send_telegram(
            f"Trimmed *{ticker}* — sold {sell_qty} shares (50%) "
            f"@ ${exit_price:.2f} → +${proceeds:.2f}.\n"
            f"Remaining position: {remaining} shares @ ${entry:.2f}\n"
            f"Cash balance: ${new_cash:.2f}",
            chat_id,
        )


def _fmt_signed_pct(value):
    """`+5.2%` / `-3.1%` / `n/a` — handles None for missing momentum data."""
    return "n/a" if value is None else f"{value:+.1f}%"


def _vol_label(rvol, up_day=None):
    """Render the relative-volume (RVOL = today / 20-day avg) line.

    `up_day` (optional) lets us call out institutional buying on an
    up day vs. distribution on a down day. When omitted (legacy
    callers) we still render the ratio without the directional flag.
    """
    if rvol is None:
        return "n/a"
    if rvol >= 2.0 and up_day:
        flag = "institutional buying confirmed ✅"
    elif rvol >= 1.5 and up_day:
        flag = "above average ↑"
    elif rvol >= 1.0 and up_day:
        flag = "above average"
    elif up_day is False and rvol >= 1.5:
        flag = "distribution ⚠️ (down day on heavy volume)"
    elif up_day is False:
        flag = "down day"
    else:
        flag = "below average"
    return f"RVOL {rvol:.2f}x ({flag})"


def _earnings_label(beat, est_rising):
    """Render the earnings momentum line text."""
    beat_txt = (
        "Beat" if beat is True else "Missed" if beat is False else "n/a"
    )
    est_txt = (
        "rising" if est_rising is True
        else "falling" if est_rising is False
        else "n/a"
    )
    return f"{beat_txt} last Q | Estimates {est_txt}"


def handle_portfolio(chat_id, scheduled=False):
    """Unified daily view — one Telegram message per position with full
    momentum breakdown (🔴 SELL → 🟡 WATCH/HOLD → 🟢 BUY/STRONG BUY order)
    followed by a single summary message bucketed by signal.

    When ``scheduled=True`` (called from the 09:00 UTC cron) extra alerts
    are sent BEFORE the main report:
      - immediate SELL alert for any position whose score dropped below 35
      - WARNING alert for any position whose score fell more than 20 points
        vs yesterday
    Yesterday's score is read from ``portfolio[ticker]['momentum_score']``
    and the new score is written back at the end of every run.
    """
    try:
        _handle_portfolio_inner(chat_id, scheduled=scheduled)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[/portfolio] UNCAUGHT CRASH:\n{tb}")
        try:
            send_telegram(
                f"❌ /portfolio crashed internally — please try again.\n"
                f"Error: {exc}",
                chat_id,
                parse_mode=None,
            )
        except Exception:
            pass


def _handle_portfolio_inner(chat_id, scheduled=False):
    # Show market status at the top of every portfolio view.
    msl = market_status_line()
    if not is_trading_day():
        send_telegram(
            f"{msl}\n_Scores are based on the last available trading session._",
            chat_id,
        )
    else:
        send_telegram(msl, chat_id)

    portfolio = load_portfolio()
    print(f"[/portfolio] loaded portfolio tickers: {list(portfolio.keys())}")
    if not portfolio:
        send_telegram(
            "Your portfolio is empty. Use /buy TICKER SHARES PRICE to add one.",
            chat_id,
        )
        return

    tickers = list(portfolio.keys())
    fundamentals = fetch_fundamentals_bulk(tickers)
    portfolio_dirty = False
    today_iso = datetime.utcnow().strftime("%Y-%m-%d")
    transition_alerts = []  # tightened/restored alerts to fire after the report

    # Pull 1-day news once for every holding so the momentum scorer can
    # share the result rather than each thread hitting NewsAPI separately.
    try:
        news_map = fetch_news_bulk(tickers, days=1)
    except Exception as exc:
        print(f"[/portfolio] bulk news fetch failed: {exc}")
        news_map = {}

    # The heavy lifting — momentum scores for every holding in parallel.
    momentum_map = score_momentum_bulk(
        tickers, fundamentals=fundamentals, news_map=news_map,
    )

    # ── Build per-position record ───────────────────────────────────────
    records = []
    for ticker, pos in portfolio.items():
        # Wrap the whole per-position block so a single bad fetch can't
        # crash the entire /portfolio command (per reliability spec).
        try:
            shares = pos["shares"]
            entry = pos["entry_price"]
            f = fundamentals.get(ticker)
            current = (f or {}).get("currentPrice") or get_current_price(ticker)

            target = _safe_target(pos.get("analyst_target"))
            if target is None and f:
                live_target = _safe_target(f.get("targetMeanPrice"))
                if live_target is not None:
                    target = live_target
                    pos["analyst_target"] = target
                    portfolio_dirty = True

            # ── Backfill new schema fields for legacy positions ─────────
            # peak_price / sector / atr_pct / atr_stop_pct / tightened_stop.
            if pos.get("peak_price") is None:
                pos["peak_price"] = entry
                portfolio_dirty = True
            if pos.get("sector") is None and f and f.get("sector"):
                pos["sector"] = str(f["sector"]).strip()
                portfolio_dirty = True
            if pos.get("tightened_stop") is None:
                pos["tightened_stop"] = False
                portfolio_dirty = True

            # ── Weekly ATR refresh (or first-time backfill) ─────────────
            atr_age_days = None
            if pos.get("atr_last_calc"):
                try:
                    last = datetime.strptime(
                        pos["atr_last_calc"], "%Y-%m-%d"
                    )
                    atr_age_days = (datetime.utcnow() - last).days
                except Exception:
                    atr_age_days = None
            if pos.get("atr_pct") is None or (
                atr_age_days is not None and atr_age_days >= ATR_RECALC_DAYS
            ):
                new_atr_pct, _ = calculate_atr(ticker)
                if new_atr_pct is not None:
                    pos["atr_pct"] = new_atr_pct
                    pos["atr_stop_pct"] = get_atr_stop_pct(new_atr_pct)
                    pos["atr_last_calc"] = today_iso
                    portfolio_dirty = True
                elif pos.get("atr_stop_pct") is None:
                    # First-time backfill failed → use the default -7%.
                    pos["atr_stop_pct"] = STOP_LOSS_PCT
                    pos["atr_last_calc"] = today_iso
                    portfolio_dirty = True

            prev_score = pos.get("momentum_score")  # may be None
            score_details = momentum_map.get(ticker)
            score = score_details[0] if score_details else None
            details = score_details[1] if score_details else {}

            # ── Update peak_price (only when NOT in tightened mode) ─────
            # In tightened mode we keep the trailing stop frozen so
            # bouncing prices don't ratchet the stop up.
            if (current is not None and not pos.get("tightened_stop")
                    and current > float(pos.get("peak_price", entry) or entry)):
                pos["peak_price"] = float(current)
                portfolio_dirty = True

            # ── Watch-zone tightened-stop transitions ───────────────────
            # Score in [35, 50) → tighten stop to -3% from current,
            #                     but NEVER lower than the existing
            #                     peak-based trailing stop (otherwise
            #                     entering watch zone could clear an
            #                     already-breached SELL — bug fix per
            #                     architect review).
            # Score >= 50      → restore trailing -X% from new peak.
            if score is not None and current is not None:
                in_watch_zone = 35 <= score < 50
                is_tightened = bool(pos.get("tightened_stop"))
                if in_watch_zone and not is_tightened:
                    # Compute both candidate stops and take the higher
                    # (more protective) — the watch-zone tighten can
                    # only ever raise the stop, never lower it.
                    peak_for_calc = float(
                        pos.get("peak_price", entry) or entry
                    )
                    atr_pct = pos.get("atr_stop_pct", STOP_LOSS_PCT)
                    peak_based_stop = round(
                        peak_for_calc * (1.0 + atr_pct / 100.0), 2,
                    )
                    candidate_tight = round(
                        current * (1.0 + WATCH_TIGHTENED_STOP_PCT / 100.0),
                        2,
                    )
                    pos["tightened_stop"] = True
                    pos["tightened_stop_price"] = max(
                        candidate_tight, peak_based_stop,
                    )
                    portfolio_dirty = True
                    transition_alerts.append((
                        f"⚠️ {ticker} momentum weakening (score {score}). "
                        f"Stop tightened to ${pos['tightened_stop_price']:.2f} "
                        f"(higher of -3% from current ${current:.2f} "
                        f"or existing trailing stop ${peak_based_stop:.2f}). "
                        f"Last chance for recovery."
                    ))
                elif score >= 50 and is_tightened:
                    pos["tightened_stop"] = False
                    pos.pop("tightened_stop_price", None)
                    pos["peak_price"] = max(
                        float(pos.get("peak_price", current) or current),
                        float(current),
                    )
                    portfolio_dirty = True
                    atr_pct_for_msg = pos.get("atr_stop_pct", STOP_LOSS_PCT)
                    transition_alerts.append((
                        f"✅ {ticker} momentum recovering (score {score}). "
                        f"Stop restored to {atr_pct_for_msg:.0f}% trailing."
                    ))

            # ── Compute effective stop AFTER all transition logic ───────
            stop_price, stop_mode = compute_trailing_stop(pos, current)

            signal, color, stop_breached = apply_position_rules(
                score, current, stop_price,
            )

            # BONUS context flag (display-only, not a trigger): price
            # has already exceeded the analyst target.
            above_target = (
                current is not None
                and target is not None
                and current > target
            )

            # ── Signal confirmation — prevent daily momentum flipping ───
            # SELL via score only fires if confirmed (prevents day-to-day
            # flipping on a single noisy reading):
            #   (a) dramatic collapse: score dropped > SELL_CONFIRM_DROP pts
            #   (b) two consecutive days below 35 threshold
            # Trailing-stop breach is always immediate — no confirmation.
            # Monthly-screen-buy protection (≈5 trading days) adds a second
            # gate: during the window, only (b) OR stop_breached can fire
            # a SELL — dramatic-only drops are suppressed to WATCH.
            in_protection = _in_monthly_buy_protection(pos)

            prev_for_check = pos.get("momentum_score")
            consecutive_low = (
                score is not None and score < 35
                and prev_for_check is not None and prev_for_check < 35
            )
            dramatic_drop = (
                score is not None and score < 35
                and prev_for_check is not None
                and (prev_for_check - score) > SELL_CONFIRM_DROP
            )
            confirmed_sell = stop_breached or consecutive_low or dramatic_drop

            if signal == "SELL" and not stop_breached:
                if not confirmed_sell:
                    # First day below 35 — unconfirmed, downgrade to WATCH.
                    signal = "WATCH"
                    color = "🟡"
                elif in_protection and not consecutive_low:
                    # In protection window and only dramatic-drop triggered —
                    # spec requires consecutive_low or stop to break protection.
                    signal = "WATCH"
                    color = "🟡"

            # ── Persist score + score_history + last_signal ─────────────
            # prev_score was captured before the new score was computed.
            prev_signal = pos.get("last_signal")
            if score is not None and pos.get("momentum_score") != score:
                pos["momentum_score"] = score
                portfolio_dirty = True
            if score is not None:
                _update_score_history(pos, score, today_iso)
                portfolio_dirty = True
            if pos.get("last_signal") != signal:
                pos["last_signal"] = signal
                portfolio_dirty = True

            common = {
                "ticker": ticker, "shares": shares, "entry": entry,
                "target": target,
                "stop": stop_price, "stop_mode": stop_mode,
                "peak": float(pos.get("peak_price", entry) or entry),
                "atr_pct": pos.get("atr_pct"),
                "atr_stop_pct": pos.get("atr_stop_pct", STOP_LOSS_PCT),
                "sector": pos.get("sector"),
                "signal": signal, "color": color,
                "score": score, "prev_score": prev_score,
                "prev_signal": prev_signal,
                "in_protection": in_protection,
                "confirmed_sell": confirmed_sell,
                "details": details, "reason": None,
                "stop_breached": stop_breached,
                "above_target": above_target,
            }

            if current is None:
                common.update({
                    "current": None, "pl_dollar": None, "pl_pct": None,
                })
                records.append(common)
                continue

            pl_dollar = (current - entry) * shares
            pl_pct = ((current - entry) / entry) * 100
            common.update({
                "current": current, "pl_dollar": pl_dollar, "pl_pct": pl_pct,
            })
            records.append(common)
        except Exception as exc:
            print(f"[/portfolio {ticker}] per-position build failed: {exc}")
            continue

    if portfolio_dirty:
        save_portfolio(portfolio)

    # Fire any tightened/restored stop transition alerts before the
    # main report so they surface to the top of the user's feed.
    for msg in transition_alerts:
        try:
            send_telegram(msg, chat_id, parse_mode=None)
        except Exception as exc:
            print(f"[/portfolio] transition alert failed: {exc}")

    # ── Single batched Claude call → one-sentence momentum reason ──────
    print(f"[/portfolio] {len(records)} records built, calling _get_quick_reasons")
    judged = [r for r in records if r["score"] is not None]
    reasons = _get_quick_reasons(judged)
    print(f"[/portfolio] reasons returned: {list(reasons.keys())}")
    for r in records:
        r["reason"] = reasons.get(
            r["ticker"],
            _fallback_reason(
                r["signal"], r.get("score"),
                stop_breached=r.get("stop_breached", False),
            ),
        )

    print(f"[/portfolio] reasons assigned, scheduled={scheduled}")
    # ── Scheduled-only urgent alerts (sent BEFORE the main report) ─────
    if scheduled:
        for r in records:
            # SELL alert fires when the confirmed signal is SELL.
            # Unconfirmed first-day sub-35 scores have already been
            # downgraded to WATCH above — those don't reach here as SELL.
            if r["signal"] == "SELL":
                # Record every real SELL signal so the monthly screen can
                # filter out recently-sold stocks.
                record_sell_signal(r["ticker"], today_iso)

                if r.get("stop_breached"):
                    pl_pct_txt = (
                        f"{r['pl_pct']:+.1f}%" if r.get("pl_pct") is not None
                        else "n/a"
                    )
                    mode_txt = (
                        " (tightened -3% from current)"
                        if r.get("stop_mode") == "tightened" else ""
                    )
                    trigger = (
                        f"Trailing stop breached{mode_txt} — current "
                        f"${r['current']:.2f} ≤ stop ${r['stop']:.2f} "
                        f"(P&L {pl_pct_txt} from entry ${r['entry']:.2f})."
                    )
                else:
                    trigger = (
                        f"Momentum score {r['score']}/100 below 35 "
                        f"for 2 consecutive days — SELL confirmed."
                    )
                send_telegram(
                    f"🚨 SELL ALERT — {r['ticker']}\n"
                    f"{trigger}\n"
                    f"Reason: {r['reason']}",
                    chat_id,
                    parse_mode=None,
                )
            elif (r["score"] is not None and r.get("in_protection")
                    and r["score"] < 35):
                # In monthly-buy protection — SELL suppressed, but warn.
                send_telegram(
                    f"⚠️ WATCH (protected) — {r['ticker']}\n"
                    f"Score {r['score']}/100 — below SELL threshold but "
                    f"within {MONTHLY_BUY_PROTECTION_CAL_DAYS}-day monthly-"
                    f"screen protection window. No SELL unless stop hit or "
                    f"score stays below 35 tomorrow.",
                    chat_id,
                    parse_mode=None,
                )

            if (r["score"] is not None and r["prev_score"] is not None
                    and (r["prev_score"] - r["score"]) > 20):
                send_telegram(
                    f"⚠️ WARNING — {r['ticker']}\n"
                    f"Momentum dropped {r['prev_score']} → {r['score']} "
                    f"({r['prev_score'] - r['score']} pts) since yesterday.\n"
                    f"Reason: {r['reason']}",
                    chat_id,
                    parse_mode=None,
                )

    # ── Sort: 🔴 first, 🟡 second, 🟢 last ────────────────────────────
    color_order = {"🔴": 0, "🟡": 1, "🟢": 2}
    records.sort(key=lambda r: (color_order.get(r["color"], 9), r["ticker"]))
    print(f"[/portfolio] sorted {len(records)} records, entering message-send loop")

    # ── Send ONE Telegram message per position with full momentum block ─
    for r in records:
        print(f"[/portfolio] sending message for {r['ticker']} signal={r['signal']}")
        d = r.get("details") or {}
        score_str = f"{r['score']}/100" if r["score"] is not None else "n/a"

        if r["current"] is None:
            send_telegram(
                f"{r['color']} {r['ticker']} — {r['signal']}\n"
                f"Momentum Score: {score_str}\n"
                f"{r['shares']} shares | Entry ${r['entry']:.2f} | "
                f"Current: price unavailable\n"
                f"Reason: {r['reason']}",
                chat_id,
                parse_mode=None,
            )
            continue

        pl_sign = "+" if r["pl_dollar"] >= 0 else "-"
        pl_line = (
            f"Entry ${r['entry']:.2f} → Current ${r['current']:.2f} | "
            f"P&L: {pl_sign}${abs(r['pl_dollar']):.2f} "
            f"({r['pl_pct']:+.1f}%)"
        )

        if r["target"] is not None:
            upside_pct = ((r["target"] - r["current"]) / r["current"]) * 100
            if r.get("above_target"):
                # Bonus context flag (display-only — not a sell trigger):
                # the position is trading ABOVE the analyst consensus
                # target, which historically means it's overextended.
                target_line = (
                    f"Target: ${r['target']:.0f} "
                    f"({upside_pct:+.0f}% upside, "
                    f"⚠️ above target — may be overextended)"
                )
            else:
                target_line = (
                    f"Target: ${r['target']:.0f} "
                    f"({upside_pct:+.0f}% upside)"
                )
        else:
            target_line = "Target: n/a"

        # ── Trailing-stop line — shows the current stop, mode, peak, ATR ──
        atr_pct = r.get("atr_pct")
        atr_stop_pct = r.get("atr_stop_pct", STOP_LOSS_PCT)
        peak = r.get("peak", r["entry"])
        if r.get("stop_mode") == "tightened":
            stop_line = (
                f"Trailing Stop: ${r['stop']:.2f} "
                f"(⚠️ tightened to -3% from current — watch zone)"
            )
        else:
            atr_label = (
                f", ATR {atr_pct:.2f}%" if atr_pct is not None else ""
            )
            stop_line = (
                f"Trailing Stop: ${r['stop']:.2f} "
                f"({abs(atr_stop_pct):.0f}% below peak ${peak:.2f}{atr_label})"
            )

        # Sector line (only when we have one)
        sector_line = (
            f"Sector: {r['sector']}\n" if r.get("sector") else ""
        )

        # If SELL was triggered by the trailing stop, surface the trigger
        # as a dedicated line so it can't be missed.
        trigger_line = ""
        if r.get("stop_breached"):
            mode_txt = (
                " (tightened to -3% from current — watch zone)"
                if r.get("stop_mode") == "tightened"
                else f" ({abs(atr_stop_pct):.0f}% below peak ${peak:.2f})"
            )
            trigger_line = (
                f"⛔ TRAILING STOP BREACHED — current ${r['current']:.2f} "
                f"≤ stop ${r['stop']:.2f}{mode_txt}\n"
            )

        # ── Signal stability indicator ───────────────────────────────────
        # "Stable" if signal unchanged from yesterday; "Changing" if it
        # flipped — show yesterday and today score side by side.
        prev_sig = r.get("prev_signal")
        cur_sig = r.get("signal")
        if prev_sig is None:
            stability_line = "Signal stability: — (first reading)\n"
        elif prev_sig == cur_sig:
            stability_line = f"Signal stability: Stable ({cur_sig})\n"
        else:
            prev_sc = r.get("prev_score")
            cur_sc = r.get("score")
            prev_sc_txt = f"{prev_sc}/100" if prev_sc is not None else "n/a"
            cur_sc_txt = f"{cur_sc}/100" if cur_sc is not None else "n/a"
            stability_line = (
                f"Signal stability: Changing ⚠️  "
                f"{prev_sig} ({prev_sc_txt}) → {cur_sig} ({cur_sc_txt})\n"
            )

        # Monthly-buy protection note (shown when SELL would have fired
        # but is being suppressed inside the 5-day protection window).
        protection_line = ""
        if r.get("in_protection") and r.get("score") is not None and r["score"] < 35:
            protection_line = (
                f"🛡 Monthly-screen protection active — SELL suppressed "
                f"(score {r['score']}/100 < 35 but within "
                f"{MONTHLY_BUY_PROTECTION_CAL_DAYS}-day window)\n"
            )

        # Spec format: full momentum breakdown then position math then reason.
        try:
            _send_chunked(
                f"{r['color']} {r['ticker']} — {r['signal']}\n"
                f"{sector_line}"
                f"Momentum Score: {score_str}\n"
                f"{stability_line}"
                f"{protection_line}"
                f"Price: 1W {_fmt_signed_pct(d.get('ret_1w'))} | "
                f"4W {_fmt_signed_pct(d.get('ret_4w'))} | "
                f"12W {_fmt_signed_pct(d.get('ret_12w'))}\n"
                f"vs S&P 500 (4W): Stock {_fmt_signed_pct(d.get('ret_4w'))} "
                f"vs SPY {_fmt_signed_pct(d.get('spy_ret_4w'))} → "
                f"RS: {_fmt_signed_pct(d.get('rs_vs_spy'))}\n"
                f"Volume: {_vol_label(d.get('rvol'), d.get('rvol_up_day'))}\n"
                f"News: {d.get('news', 'n/a')}\n"
                f"Earnings: "
                f"{_earnings_label(d.get('earnings_beat'), d.get('estimates_rising'))}\n"
                f"{trigger_line}"
                f"{pl_line}\n"
                f"{target_line}\n"
                f"{stop_line}\n"
                f"{r['reason']}",
                chat_id,
                parse_mode=None,
            )
            print(f"[/portfolio] sent message for {r['ticker']}")
        except Exception as exc:
            import traceback
            print(f"[/portfolio] message build/send FAILED for {r['ticker']}: {exc}\n{traceback.format_exc()}")

    # ── Final summary message — bucketed by signal per spec ────────────
    priced = [r for r in records if r["current"] is not None]
    total_cost = sum(r["entry"] * r["shares"] for r in priced)
    total_value = sum(r["current"] * r["shares"] for r in priced)
    total_pl = total_value - total_cost
    total_pct = (total_pl / total_cost * 100) if total_cost > 0 else 0
    pl_sign = "+" if total_pl >= 0 else "-"

    health_line = ""
    try:
        health = calculate_health_score(portfolio, fundamentals)
        if health is not None:
            health_line = (
                f"Health: {health['score']}/10 {health['rating_emoji']}"
            )
    except Exception as exc:
        print(f"[health] portfolio summary failed: {exc}")

    # Three signal buckets per spec.
    sell_tickers = [r["ticker"] for r in records if r["signal"] == "SELL"]
    watch_tickers = [
        r["ticker"] for r in records if r["signal"] == "WATCH"
    ]
    hold_buy_tickers = [
        r["ticker"] for r in records
        if r["signal"] in ("HOLD", "BUY", "STRONG BUY")
    ]

    summary_lines = [
        "📊 PORTFOLIO SUMMARY",
        f"Total: ${total_value:,.0f} | "
        f"P&L: {pl_sign}${abs(total_pl):,.0f} ({total_pct:+.1f}%)",
    ]
    if health_line:
        summary_lines.append(health_line)
    summary_lines.append("")
    summary_lines.append(
        "🔴 SELL signals: "
        + (", ".join(sell_tickers) if sell_tickers else "none")
        + " — momentum broken"
    )
    summary_lines.append(
        "🟡 WATCH signals: "
        + (", ".join(watch_tickers) if watch_tickers else "none")
        + " — momentum weakening"
    )
    summary_lines.append(
        "🟢 HOLD/BUY signals: "
        + (", ".join(hold_buy_tickers) if hold_buy_tickers else "none")
        + " — momentum intact"
    )
    send_telegram("\n".join(summary_lines), chat_id, parse_mode=None)


def handle_deep(args, chat_id):
    """`/deep TICKER` — full momentum analysis on any stock."""
    if not args:
        send_telegram(
            "Usage: /deep TICKER\nExample: /deep NVDA",
            chat_id,
        )
        return
    ticker = args[0].upper()
    msl = market_status_line()
    market_note = "" if is_trading_day() else f"\n_{msl} — using last session data._"
    send_telegram(
        f"*Running momentum analysis on {ticker}...*\n"
        f"Pulling 12-week price history, news & earnings. ~10s.{market_note}",
        chat_id,
    )

    def _run():
        try:
            # Fetch the inputs we'll share with the scorer once each.
            try:
                info = yf.Ticker(ticker).info
            except Exception:
                info = None
            # Finnhub company-news is the primary source (ticker-specific,
            # 20 articles, 3-day window). NewsAPI is the fallback.
            news = get_finnhub_news(ticker, days=3)
            if not news:
                print(f"[/deep {ticker}] Finnhub news empty, falling back to NewsAPI")
                news = get_stock_news(ticker, days=3, page_size=10)

            score, details = score_momentum(
                ticker, info=info, news_articles=news,
            )
            if score is None:
                send_telegram(
                    f"Could not compute momentum for *{ticker}* — "
                    f"insufficient price history. Check the ticker spelling.",
                    chat_id,
                )
                return

            signal, color = get_momentum_signal(score)
            current_px = (info or {}).get("currentPrice")
            target = _safe_target((info or {}).get("targetMeanPrice"))

            d = details
            target_line = (
                f"Analyst target: ${target:.2f}" if target else "Analyst target: n/a"
            )
            current_line = (
                f"Current price: ${current_px:.2f}" if current_px else "Current price: n/a"
            )

            # Build the base stats block (shown regardless of DQ status).
            stats_block = (
                f"Momentum Score: {score}/100\n"
                f"Price: 1W {_fmt_signed_pct(d.get('ret_1w'))} | "
                f"4W {_fmt_signed_pct(d.get('ret_4w'))} | "
                f"12W {_fmt_signed_pct(d.get('ret_12w'))}\n"
                f"vs S&P 500 (4W): Stock {_fmt_signed_pct(d.get('ret_4w'))} "
                f"vs SPY {_fmt_signed_pct(d.get('spy_ret_4w'))} → "
                f"RS: {_fmt_signed_pct(d.get('rs_vs_spy'))}\n"
                f"Volume: {_vol_label(d.get('rvol'), d.get('rvol_up_day'))}\n"
                f"News: {d.get('news', 'n/a')}\n"
                f"Earnings: "
                f"{_earnings_label(d.get('earnings_beat'), d.get('estimates_rising'))}\n"
                f"{current_line} | {target_line}"
            )

            # Hard disqualification check — overrides BUY signal.
            dqs = check_buy_disqualifiers(ticker, score, details, info or {})

            if dqs and signal in ("BUY", "STRONG BUY"):
                # Show stats first, then the disqualification block.
                send_telegram(
                    f"📊 {ticker} — Deep Analysis\n{stats_block}",
                    chat_id,
                    parse_mode=None,
                )
                dq_msg = (
                    f"\n{format_buy_disqualifier_block(ticker, score, dqs)}\n\n"
                    f"Despite a {score}/100 momentum score, {ticker} does NOT "
                    f"qualify as a BUY due to the rule(s) above."
                )
                send_telegram(dq_msg, chat_id, parse_mode=None)
            else:
                # Clean — show normal signal with reasoning.
                reasons = _get_quick_reasons([{
                    "ticker": ticker, "signal": signal, "color": color,
                    "score": score, "details": details,
                }])
                reason = reasons.get(ticker, _fallback_reason(signal, score))
                send_telegram(
                    f"{color} {ticker} — {signal}\n"
                    f"{stats_block}\n"
                    f"{reason}",
                    chat_id,
                    parse_mode=None,
                )

            # Feedback loop — log this on-demand recommendation with the
            # momentum score (no claude_target / stop_loss available from
            # the momentum-only flow).
            try:
                parsed = [{
                    "ticker": ticker,
                    "signal": signal,
                    "claude_target": None,
                    "stop_loss": None,
                    "bull_case": None,
                    "bear_case": None,
                }]
                log_recommendations(
                    parsed,
                    price_lookup=lambda _t: current_px,
                    analyst_lookup=lambda _t: target,
                    momentum_lookup=lambda _t: score,
                    source="deep",
                )
            except Exception as exc:
                print(f"[recs] /deep logging failed: {exc}")
        except Exception as exc:
            print(f"[/deep {ticker}] error: {exc}")
            send_telegram(
                f"Momentum analysis for *{ticker}* failed: {exc}",
                chat_id,
            )

    Thread(target=_run, daemon=True).start()


def handle_start(chat_id):
    send_telegram(
        "Welcome to *Portfolio Bot*\n\n"
        "Commands:\n"
        "`/buy TICKER SHARES PRICE` — add a position (sector cap: 3/sector)\n"
        "`/sell TICKER [SHARES]` — sell whole position or N shares\n"
        "`/trim TICKER` — sell 50% of a position (quick partial exit)\n"
        "`/portfolio` — momentum-scored daily view (one msg per position + summary)\n"
        "`/cash` — total value, invested, idle cash + treasury suggestion\n"
        "`/health` — portfolio health score (0–10) + sector breakdown\n"
        "`/earnings` — upcoming earnings dates (next 30 days)\n"
        "`/deep TICKER` — full momentum analysis on any stock\n"
        "`/monthly` — monthly S&P 500 + Nasdaq 100 buy screen (top 2 picks)\n"
        "`/review` — open recommendations + 4w/8w countdowns\n"
        "`/performance` — track record vs S&P 500\n"
        "`/market` — market status (open/closed, ET time, holiday check)\n"
        "`/settings` — view/change delivery time and on/off toggle\n\n"
        "_Hard SELL rules:_\n"
        "_• Trailing stop loss from peak (-5% / -7% / -10% by ATR volatility)_\n"
        "_• Tightened to -3% from current when momentum score 35–49_\n"
        "_• Momentum score < 35 (capital protection)_\n"
        "_• Price ≥ analyst target (above fair value — strong sell)_\n"
        "_Soft alert:_\n"
        "_• Price ≥ 90% of analyst target (approaching fair value)_\n"
        "_• 08:00 UTC pre-market gap-down check vs trailing stop_\n"
        "_Targets are fetched at /buy and refreshed on the 1st of each month._",
        chat_id,
    )


# ---------------------------------------------------------------------------
# Telegram long-polling loop (runs in a background thread)
# ---------------------------------------------------------------------------
def poll_telegram():
    """Long-poll Telegram forever. Any error is logged and the loop restarts."""
    last_update_id = 0
    print("Telegram polling loop started.")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            res = requests.get(url, params=params, timeout=40).json()

            for update in res.get("result", []):
                last_update_id = update["update_id"]
                try:
                    msg = update.get("message")
                    if not msg or "text" not in msg:
                        print(f"[update {last_update_id}] no text, skipping")
                        continue

                    text = msg["text"].strip()
                    chat_id = msg["chat"]["id"]
                    user = msg.get("from", {}).get("username", "unknown")
                    print(f"[incoming] chat={chat_id} user=@{user} text={text!r}")
                    remember_chat(chat_id)

                    if not text.startswith("/"):
                        continue

                    parts = text.split()
                    cmd = parts[0].split("@")[0]   # strip @BotName if present
                    args = parts[1:]

                    if cmd == "/start" or cmd == "/help":
                        handle_start(chat_id)
                    elif cmd == "/buy":
                        handle_buy(args, chat_id)
                    elif cmd == "/sell":
                        handle_sell(args, chat_id)
                    elif cmd == "/trim":
                        handle_trim(args, chat_id)
                    elif cmd == "/portfolio":
                        # The unified daily view does live yfinance fetches
                        # plus a Claude reasoning call — push it off the
                        # polling thread so /portfolio stays responsive.
                        send_telegram(
                            "Building your daily portfolio view...", chat_id
                        )
                        Thread(
                            target=handle_portfolio,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/earnings":
                        # Earnings calls AlphaVantage with a 13-second
                        # throttle per ticker — push to a background
                        # thread so the polling loop doesn't block for
                        # >1 minute on a 6-position portfolio.
                        send_telegram(
                            "Analyzing… please wait", chat_id,
                        )
                        Thread(
                            target=handle_earnings,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/health":
                        send_telegram(
                            "Analyzing… please wait", chat_id,
                        )
                        Thread(
                            target=handle_health,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/cash":
                        Thread(
                            target=handle_cash,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/market":
                        now_et = datetime.now(_ET)
                        day_str = now_et.strftime("%A, %b %d %Y • %I:%M %p ET")
                        trading = "Yes ✅" if is_trading_day() else "No ❌"
                        open_now = "Yes 🟢" if is_market_open() else "No ⏸"
                        send_telegram(
                            f"*Market Status*\n"
                            f"{market_status_line()}\n\n"
                            f"Date/Time (ET): {day_str}\n"
                            f"Trading day: {trading}\n"
                            f"Market open now: {open_now}\n"
                            f"NYSE hours: 9:30 AM – 4:00 PM ET",
                            chat_id,
                        )
                    elif cmd == "/settings":
                        handle_settings(args, chat_id)
                    elif cmd == "/performance":
                        send_telegram(
                            "Analyzing… please wait", chat_id,
                        )
                        Thread(
                            target=handle_performance,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/review":
                        send_telegram(
                            "Analyzing… please wait", chat_id,
                        )
                        Thread(
                            target=handle_review,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    elif cmd == "/deep":
                        handle_deep(args, chat_id)
                    elif cmd == "/monthly":
                        send_telegram(
                            "Monthly buy screen started, please wait...",
                            chat_id,
                        )
                        Thread(
                            target=run_monthly_screen,
                            args=(chat_id,),
                            daemon=True,
                        ).start()
                    else:
                        send_telegram(
                            "Unknown command. Send /help for the list.", chat_id
                        )
                except Exception as inner:
                    # Never let one bad update kill the loop.
                    print(f"Error handling update {last_update_id}: {inner}")
        except Exception as e:
            print(f"Polling error (will retry in 5s): {e}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Monthly analyst-target refresh
# ---------------------------------------------------------------------------
def refresh_analyst_targets(chat_id=None, notify=True):
    """Re-fetch the analyst consensus target for every holding.

    Stores the new target on each position and resets the per-position
    alert flags so the new target gets a fresh evaluation. Optionally
    sends a Telegram summary of which targets changed.
    """
    portfolio = load_portfolio()
    if not portfolio:
        return

    tickers = list(portfolio.keys())
    fundamentals = fetch_fundamentals_bulk(tickers)
    changes = []
    unchanged = []
    missing = []

    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker)
        new_target = _safe_target((f or {}).get("targetMeanPrice"))
        old_target = _safe_target(pos.get("analyst_target"))

        if new_target is None:
            missing.append(ticker)
            continue

        # Reset alert flags so the refreshed target re-evaluates from scratch.
        pos["analyst_target"] = new_target
        pos["approach_alerted"] = False
        pos["above_alerted"] = False

        if old_target is None:
            changes.append(f"{ticker}: set ${new_target:.2f}")
        elif abs(new_target - old_target) >= 0.01:
            arrow = "↑" if new_target > old_target else "↓"
            changes.append(
                f"{ticker}: ${old_target:.2f} → ${new_target:.2f} {arrow}"
            )
        else:
            unchanged.append(ticker)

    save_portfolio(portfolio)

    if notify and chat_id is not None:
        body = ["*Monthly analyst-target refresh*", ""]
        if changes:
            body.append("*Updated:*")
            body.extend(changes)
        else:
            body.append("_No target changes._")
        if unchanged:
            body.append("")
            body.append(f"_Unchanged:_ {', '.join(unchanged)}")
        if missing:
            body.append("")
            body.append(f"_No target available:_ {', '.join(missing)}")
        send_telegram("\n".join(body), chat_id)


# ---------------------------------------------------------------------------
# Portfolio Health Score (0–10) — diversification + stop / upside / momentum
# ---------------------------------------------------------------------------
def calculate_health_score(portfolio=None, fundamentals=None):
    """Compute a four-component health score for the portfolio.

    Components (each 0–10):
      1. Diversification — distinct sectors across holdings
      2. Stop-loss health — how many positions are below stop
      3. Upside remaining — average % gap to analyst target
      4. Momentum — share of positions with positive P&L

    Final score = average of the four, rounded to 1 dp.

    Returns a dict {score, rating_emoji, rating_label, components: [(name,
    score, detail), ...]} or None if the portfolio is empty.

    `fundamentals` may be passed in to reuse a bulk fetch (e.g. /portfolio).
    """
    if portfolio is None:
        portfolio = load_portfolio()
    if not portfolio:
        return None
    if fundamentals is None:
        fundamentals = fetch_fundamentals_bulk(list(portfolio.keys()))

    rows = []
    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker) or {}
        current = _safe_float(f.get("currentPrice"))
        if current is None:
            current = _safe_float(get_current_price(ticker))
        target = _safe_target(pos.get("analyst_target"))
        if target is None:
            target = _safe_target(f.get("targetMeanPrice"))
        # Prefer the persisted sector (set by /buy or backfilled by
        # /portfolio); fall back to the live yfinance sector.
        sector = pos.get("sector")
        if not sector:
            sector_raw = f.get("sector")
            sector = sector_raw.strip() if isinstance(sector_raw, str) else None
        # Use the actual trailing stop (peak-based + ATR-adjusted, or
        # tightened in the watch zone) — not the legacy entry × -7%.
        stop_price, _stop_mode = compute_trailing_stop(pos, current)
        rows.append(
            {
                "ticker": ticker,
                "entry": pos["entry_price"],
                "shares": pos["shares"],
                "current": current,
                "target": target,
                "sector": sector or None,
                "stop_price": stop_price,
            }
        )

    # 1. Diversification
    sectors = {r["sector"] for r in rows if r["sector"]}
    n_sec = len(sectors)
    if n_sec >= 4:
        div_score = 10.0
    elif n_sec == 3:
        div_score = 7.0
    elif n_sec == 2:
        div_score = 4.0
    else:
        # 0 or 1 sector → 1/10
        div_score = 1.0
    if sectors:
        div_detail = (
            f"{n_sec} sector{'s' if n_sec != 1 else ''}: "
            f"{', '.join(sorted(sectors))}"
        )
    else:
        div_detail = "sector data unavailable"

    # 2. Stop-loss health
    below_stop = [
        r["ticker"] for r in rows
        if r["current"] is not None and r["current"] <= r["stop_price"]
    ]
    stop_score = float(max(0, 10 - 2 * len(below_stop)))
    if below_stop:
        stop_detail = (
            f"{len(below_stop)} below stop: {', '.join(below_stop)}"
        )
    else:
        stop_detail = "all positions above stop loss"

    # 3. Upside remaining
    upsides = []
    for r in rows:
        if r["current"] is None or r["target"] is None or r["current"] == 0:
            continue
        upsides.append(((r["target"] - r["current"]) / r["current"]) * 100)
    if not upsides:
        # Floor at 1.0 (the spec's worst published bucket) rather than 0
        # so a missing-data signal doesn't unfairly bottom out the average.
        ups_score = 1.0
        ups_detail = "no analyst-target / price data (insufficient data)"
    else:
        avg_up = sum(upsides) / len(upsides)
        if avg_up > 30:
            ups_score = 10.0
        elif avg_up >= 20:
            ups_score = 7.0
        elif avg_up >= 10:
            ups_score = 4.0
        else:
            ups_score = 1.0
        ups_detail = (
            f"avg upside {avg_up:+.1f}% across {len(upsides)} "
            f"position{'s' if len(upsides) != 1 else ''}"
        )

    # 4. Momentum
    pos_with_price = [r for r in rows if r["current"] is not None]
    if not pos_with_price:
        # Floor at 1.0 — same rationale as Upside above.
        mom_score = 1.0
        mom_detail = "no live prices available (insufficient data)"
    else:
        positive = [r for r in pos_with_price if r["current"] >= r["entry"]]
        mom_score = (len(positive) / len(pos_with_price)) * 10.0
        mom_detail = (
            f"{len(positive)}/{len(pos_with_price)} positions with "
            f"positive P&L"
        )

    components = [
        ("Diversification", div_score, div_detail),
        ("Stop-loss health", stop_score, stop_detail),
        ("Upside remaining", ups_score, ups_detail),
        ("Momentum", mom_score, mom_detail),
    ]

    total = round(sum(c[1] for c in components) / len(components), 1)

    if total >= 8:
        emoji, label = "💪", "Strong"
    elif total >= 6:
        emoji, label = "👍", "Healthy"
    elif total >= 4:
        emoji, label = "⚠️", "Needs attention"
    else:
        emoji, label = "🚨", "Critical"

    # Sector breakdown — count holdings per sector + flag the
    # max-3-per-sector cap so /health can render a concentration table.
    sector_counts = {}
    for r in rows:
        sec = r.get("sector") or "Unknown"
        sector_counts[sec] = sector_counts.get(sec, 0) + 1

    return {
        "score": total,
        "rating_emoji": emoji,
        "rating_label": label,
        "components": components,
        "sector_counts": sector_counts,
        "total_positions": len(rows),
    }


def format_health_summary(report):
    """One-line summary suitable for embedding at the top of /portfolio."""
    if report is None:
        return None
    return (
        f"*Health: {report['score']}/10* "
        f"{report['rating_emoji']} {report['rating_label']}"
    )


def format_health_breakdown(report):
    """Full multi-line breakdown for /health and the 08:30 push."""
    if report is None:
        return "Your portfolio is empty."
    lines = [
        f"*Portfolio Health Score: {report['score']}/10* "
        f"{report['rating_emoji']} {report['rating_label']}",
        "",
    ]
    for name, score, detail in report["components"]:
        # `detail` contains dynamic ticker/sector strings; we deliberately
        # render it without Markdown emphasis so unexpected `_` or `*`
        # characters can't break Telegram's Markdown parser.
        lines.append(f"• *{name}:* {score:.1f}/10 — {detail}")

    # ── Sector concentration table (max 3 per sector cap) ──────────────
    sector_counts = report.get("sector_counts") or {}
    if sector_counts:
        lines.append("")
        lines.append("*Sector breakdown* (cap: 3 per sector)")
        for sec, n in sorted(
            sector_counts.items(), key=lambda kv: (-kv[1], kv[0])
        ):
            flag = " ⚠️ at cap" if n >= MAX_POSITIONS_PER_SECTOR else ""
            lines.append(f"• {sec}: {n}{flag}")
    return "\n".join(lines)


def handle_health(chat_id):
    """`/health` — show the score and the four-component breakdown."""
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram("Your portfolio is empty.", chat_id)
        return
    report = calculate_health_score(portfolio)
    _send_chunked(format_health_breakdown(report), chat_id)


def handle_settings(args, chat_id):
    """`/settings` — view or change bot delivery settings.

    Sub-commands:
      /settings                  — show current settings
      /settings delivery on|off  — toggle automatic daily delivery
      /settings time HH:MM       — set delivery time (UTC, 24-hour)
    """
    settings = load_settings()

    # ── /settings delivery on|off ────────────────────────────────────────────
    if args and args[0].lower() == "delivery":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            send_telegram(
                "Usage: /settings delivery on   or   /settings delivery off",
                chat_id,
            )
            return
        enabled = args[1].lower() == "on"
        settings["daily_delivery"] = enabled
        save_settings(settings)
        state = "✅ enabled" if enabled else "❌ disabled"
        delivery_time = settings.get("delivery_time_utc", "09:00")
        msg = (
            f"*Daily delivery {state}*\n"
            + (
                f"Automatic /portfolio will be sent at *{delivery_time} UTC* "
                "every weekday."
                if enabled
                else "No automatic reports will be sent until you turn it back on."
            )
        )
        send_telegram(msg, chat_id)
        return

    # ── /settings time HH:MM ─────────────────────────────────────────────────
    if args and args[0].lower() == "time":
        if len(args) < 2:
            send_telegram(
                "Usage: /settings time HH:MM\nExample: /settings time 08:30",
                chat_id,
            )
            return
        time_str = args[1].strip()
        # Validate format
        if not re.match(r"^\d{2}:\d{2}$", time_str):
            send_telegram(
                f"Invalid time '{time_str}' — use 24-hour HH:MM format, e.g. 08:30",
                chat_id,
            )
            return
        hh, mm = int(time_str[:2]), int(time_str[3:])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            send_telegram(
                f"Invalid time '{time_str}' — hours must be 00–23, minutes 00–59.",
                chat_id,
            )
            return
        settings["delivery_time_utc"] = time_str
        save_settings(settings)
        now_utc = datetime.utcnow()
        current_hhmm = now_utc.strftime("%H:%M")
        already_sent_today = (
            _last_delivery_date == now_utc.date() if _last_delivery_date else False
        )
        note = (
            " (already sent today — next delivery is tomorrow)"
            if already_sent_today
            else ""
        )
        send_telegram(
            f"*Delivery time updated to {time_str} UTC*{note}\n"
            "Change takes effect immediately — no restart needed.",
            chat_id,
        )
        return

    # ── /settings — show current config ──────────────────────────────────────
    enabled = settings.get("daily_delivery", True)
    delivery_time = settings.get("delivery_time_utc", "09:00")
    tz_label = settings.get("timezone_display", "ET")
    state_icon = "✅ On" if enabled else "❌ Off"

    # Approximate ET equivalent (UTC-4 in summer, UTC-5 in winter).
    try:
        hh, mm = int(delivery_time[:2]), int(delivery_time[3:])
        now_et = datetime.now(_ET)
        utc_offset = int(now_et.utcoffset().total_seconds() // 3600)
        et_hh = (hh + utc_offset) % 24
        et_str = f"{et_hh:02d}:{mm:02d} {tz_label}"
    except Exception:
        et_str = "n/a"

    send_telegram(
        f"*Bot Settings*\n\n"
        f"Daily delivery: {state_icon}\n"
        f"Delivery time: *{delivery_time} UTC* ({et_str})\n"
        f"Delivered: every weekday (Mon–Fri), skips holidays\n\n"
        f"_To change:_\n"
        f"`/settings delivery on` or `/settings delivery off`\n"
        f"`/settings time HH:MM`  (24-hr UTC, e.g. 08:30)\n",
        chat_id,
    )


def handle_cash(chat_id):
    """`/cash` — total value, invested, idle cash + treasury suggestion.

    Reads cash.json (bootstrapping it on first call from
    INITIAL_CASH minus the cost of every existing position) and combines
    it with the live market value of the portfolio to show:
      • Total portfolio value (positions + cash)
      • Invested amount (entry × shares)
      • Current market value of positions
      • Available cash
      • A nudge to park idle cash > $500 in BIL/SGOV (≈5% yield)
    """
    cash_data = load_cash()
    cash = float(cash_data.get("cash", 0.0))

    portfolio = load_portfolio()
    invested = 0.0
    market_value = 0.0
    if portfolio:
        try:
            fundamentals = fetch_fundamentals_bulk(list(portfolio.keys()))
        except Exception as exc:
            print(f"[/cash] bulk fundamentals failed: {exc}")
            fundamentals = {}
        for ticker, pos in portfolio.items():
            try:
                shares = float(pos.get("shares", 0))
                entry = float(pos.get("entry_price", 0))
                invested += shares * entry
                f = fundamentals.get(ticker) or {}
                current = f.get("currentPrice")
                if current is None:
                    current = get_current_price(ticker)
                if current is not None:
                    market_value += shares * float(current)
                else:
                    # Price fetch failed → fall back to entry to avoid
                    # underreporting the user's net worth.
                    market_value += shares * entry
            except Exception as exc:
                print(f"[/cash {ticker}] valuation failed: {exc}")
                continue

    total_value = market_value + cash
    pl = market_value - invested
    pl_sign = "+" if pl >= 0 else "-"
    pl_pct = (pl / invested * 100.0) if invested > 0 else 0.0
    cash_pct = (cash / total_value * 100.0) if total_value > 0 else 100.0

    lines = [
        "💰 *CASH & ALLOCATION*",
        f"Total value: ${total_value:,.2f}",
        f"  • Positions: ${market_value:,.2f} "
        f"(invested ${invested:,.2f}, "
        f"P&L {pl_sign}${abs(pl):,.2f} / {pl_pct:+.1f}%)",
        f"  • Cash: ${cash:,.2f} ({cash_pct:.0f}% of total)",
    ]

    # Idle-cash nudge: > $500 sitting idle → suggest BIL or SGOV which
    # currently yield ≈5% on T-bills with daily liquidity.
    if cash > IDLE_CASH_THRESHOLD:
        annual_yield = cash * 0.05
        lines.append("")
        lines.append(
            f"💡 *${cash:,.2f} idle* — consider parking in *BIL* or *SGOV* "
            f"(≈5% yield on short Treasuries). Daily liquidity, no lockup. "
            f"Estimated income: ~${annual_yield:,.0f}/yr."
        )
    elif cash < 0:
        lines.append("")
        lines.append(
            "⚠️ Cash balance is negative — you've over-allocated relative "
            "to the seed cash. Add a buy/sell to reconcile or top up the "
            "balance manually in cash.json."
        )

    _send_chunked("\n".join(lines), chat_id)


def scheduled_health_check():
    """Daily 08:30 UTC morning health check (between earnings and analysis)."""
    if _skip_if_not_trading_day("health-check"):
        return
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping health check — no chat has interacted yet.")
        return
    print("[scheduled] running portfolio health check")
    try:
        portfolio = load_portfolio()
        if not portfolio:
            return
        report = calculate_health_score(portfolio)
        send_telegram(
            "☀️ *Morning Health Check*\n\n" + format_health_breakdown(report),
            chat_id,
        )
    except Exception as exc:
        print(f"[scheduled] health check failed: {exc}")


# ---------------------------------------------------------------------------
# Earnings calendar — Alpha Vantage + yfinance
# ---------------------------------------------------------------------------
_AV_LOCK = threading.Lock()
_AV_LAST_CALL_TS = 0.0


def _av_throttle():
    """Block until at least ALPHA_VANTAGE_MIN_INTERVAL_SEC has passed since the
    previous call. Keeps us under the 5 req/min free-tier limit even when a
    sweep iterates over every holding back-to-back.
    """
    global _AV_LAST_CALL_TS
    with _AV_LOCK:
        wait = ALPHA_VANTAGE_MIN_INTERVAL_SEC - (time.time() - _AV_LAST_CALL_TS)
        if wait > 0:
            time.sleep(wait)
        _AV_LAST_CALL_TS = time.time()


def _safe_float(value):
    """Return float(value) or None for NaN / non-numeric / missing."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def fetch_earnings_calendar(ticker, horizon="3month"):
    """Fetch upcoming earnings for `ticker` from Alpha Vantage.

    Alpha Vantage's EARNINGS_CALENDAR endpoint returns CSV. Returns a list
    of dicts: {report_date (date), fiscal_date (str), estimate (float|None),
    currency (str)}. Returns [] on any error or if the key isn't configured.
    """
    if not ALPHA_VANTAGE_KEY:
        return []
    _av_throttle()
    try:
        resp = requests.get(
            ALPHA_VANTAGE_URL,
            params={
                "function": "EARNINGS_CALENDAR",
                "symbol": ticker,
                "horizon": horizon,
                "apikey": ALPHA_VANTAGE_KEY,
            },
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.text.strip()
        # Alpha Vantage returns JSON for errors / rate limits, CSV for data.
        if not text or text.startswith("{") or text.lower().startswith("note") \
                or text.lower().startswith("information"):
            print(f"[earnings] {ticker}: AV non-CSV response: {text[:120]}")
            return []
        reader = csv.DictReader(io.StringIO(text))
        out = []
        for row in reader:
            try:
                report_date = datetime.strptime(
                    row["reportDate"], "%Y-%m-%d"
                ).date()
            except (KeyError, ValueError):
                continue
            est_raw = (row.get("estimate") or "").strip()
            try:
                est_val = float(est_raw) if est_raw else None
            except ValueError:
                est_val = None
            out.append(
                {
                    "report_date": report_date,
                    "fiscal_date": row.get("fiscalDateEnding", ""),
                    "estimate": est_val,
                    "currency": row.get("currency", ""),
                }
            )
        return out
    except Exception as exc:
        print(f"[earnings] {ticker}: AV fetch failed: {exc}")
        return []


def get_next_earnings(ticker, horizon="3month"):
    """Return next upcoming earnings dict (with `days_until`) or None."""
    today = datetime.utcnow().date()
    cal = fetch_earnings_calendar(ticker, horizon)
    upcoming = sorted(
        (e for e in cal if e["report_date"] >= today),
        key=lambda x: x["report_date"],
    )
    if not upcoming:
        return None
    nxt = dict(upcoming[0])
    nxt["days_until"] = (nxt["report_date"] - today).days
    return nxt


def get_last_quarter_eps(ticker):
    """Return a beat/miss summary for the most recent reported quarter, or None.

    Uses yfinance's earnings_dates frame which contains both estimates and
    actuals. We pick the most recent row that has a Reported EPS value.
    """
    try:
        t = yf.Ticker(ticker)
        ed = t.get_earnings_dates(limit=12)
        if ed is None or ed.empty:
            return None
        if "Reported EPS" not in ed.columns:
            return None
        reported = ed.dropna(subset=["Reported EPS"])
        if reported.empty:
            return None
        latest = reported.sort_index(ascending=False).iloc[0]
        actual = _safe_float(latest.get("Reported EPS"))
        est = _safe_float(latest.get("EPS Estimate"))
        if actual is None:
            return None
        if est is None:
            return f"reported ${actual:.2f}"
        verdict = "beat" if actual > est else (
            "missed" if actual < est else "met"
        )
        return f"{verdict} (${actual:.2f} vs ${est:.2f} est)"
    except Exception as exc:
        print(f"[earnings] {ticker}: history fetch failed: {exc}")
        return None


def check_earnings_calendar(chat_id, threshold_days=EARNINGS_ALERT_DAYS):
    """Scan all holdings; send a Telegram alert for each one with earnings
    inside the threshold window.

    Returns the list of alerted tickers.
    """
    portfolio = load_portfolio()
    if not portfolio:
        return []
    if not ALPHA_VANTAGE_KEY:
        send_telegram(
            "_Earnings check skipped — ALPHA_VANTAGE_KEY not configured._",
            chat_id,
        )
        return []

    alerted = []
    for ticker, pos in portfolio.items():
        nxt = get_next_earnings(ticker)
        if not nxt:
            continue
        days = nxt["days_until"]
        if days > threshold_days:
            continue

        # Position size + P&L
        shares = pos["shares"]
        entry = pos["entry_price"]
        current = get_current_price(ticker)
        if current is not None:
            pl_dollar = (current - entry) * shares
            pl_pct = ((current - entry) / entry) * 100
            sign = "+" if pl_dollar >= 0 else "-"
            pl_line = (
                f"P&L: {sign}${abs(pl_dollar):.2f} "
                f"({sign}{abs(pl_pct):.1f}%)"
            )
        else:
            pl_line = "P&L: _price unavailable_"

        # Analyst EPS estimate — prefer Alpha Vantage's, fall back to yfinance.
        # Both values pass through _safe_float to reject NaN / non-numeric.
        eps_est = _safe_float(nxt.get("estimate"))
        forward_eps = None
        if eps_est is None:
            try:
                forward_eps = _safe_float(
                    yf.Ticker(ticker).info.get("forwardEps")
                )
            except Exception:
                forward_eps = None
        if eps_est is not None:
            eps_line = f"Analyst EPS estimate: ${eps_est:.2f}"
        elif forward_eps is not None:
            eps_line = f"Forward EPS (yfinance): ${forward_eps:.2f}"
        else:
            eps_line = "Analyst EPS estimate: _n/a_"

        # Beat / miss last quarter — wrap in try so one bad ticker can't
        # abort the whole sweep.
        try:
            last_q = get_last_quarter_eps(ticker)
        except Exception as exc:
            print(f"[earnings] {ticker}: last-quarter lookup failed: {exc}")
            last_q = None
        last_line = (
            f"Last quarter: {last_q}" if last_q else "Last quarter: _n/a_"
        )

        when = (
            "today" if days == 0
            else "tomorrow" if days == 1
            else f"in {days} days"
        )

        # 🚨 URGENT only when earnings is imminent (≤ URGENT_EARNINGS_DAYS).
        # Beyond that, this is a heads-up, not an alarm.
        header = (
            "🚨 *URGENT EARNINGS ALERT*"
            if days <= URGENT_EARNINGS_DAYS
            else "⚠️ *EARNINGS ALERT*"
        )
        msg = (
            f"{header}\n\n"
            f"*{ticker}* — earnings {when} ({nxt['report_date'].isoformat()})\n"
            f"Position: {shares} shares @ ${entry:.2f}\n"
            f"{pl_line}\n"
            f"{eps_line}\n"
            f"{last_line}\n\n"
            f"_Three options to consider:_\n"
            f"• *Hold* through earnings — full upside, full downside risk\n"
            f"• *Trim 50%* to lock partial gains — `/trim {ticker}`\n"
            f"• *Tighten stop / exit* if you want zero earnings risk — "
            f"`/sell {ticker}`"
        )
        try:
            send_telegram(msg, chat_id)
            alerted.append(ticker)
        except Exception as exc:
            print(f"[earnings] {ticker}: send failed: {exc}")

    return alerted


def handle_earnings(chat_id):
    """Manual /earnings — split holdings into three clear buckets:
    upcoming within 30 days, scheduled beyond 30 days, and data unavailable.
    """
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram("Your portfolio is empty.", chat_id)
        return
    if not ALPHA_VANTAGE_KEY:
        send_telegram(
            "_Earnings lookup unavailable — "
            "ALPHA_VANTAGE_KEY not configured._",
            chat_id,
        )
        return

    today = datetime.utcnow().date()
    within = []      # (days_until, formatted_line)
    beyond = []      # (days_until, formatted_line)
    unavailable = []

    for ticker in portfolio.keys():
        try:
            cal = fetch_earnings_calendar(ticker)
        except Exception as exc:
            print(f"[earnings] {ticker}: lookup failed: {exc}")
            cal = []
        upcoming = sorted(
            (e for e in cal if e["report_date"] >= today),
            key=lambda x: x["report_date"],
        )
        if not upcoming:
            # Either AV returned no rows, errored, or rate-limited us.
            unavailable.append(ticker)
            continue

        nxt = upcoming[0]
        days = (nxt["report_date"] - today).days
        eps = _safe_float(nxt.get("estimate"))
        eps_str = f"est ${eps:.2f}" if eps is not None else "est n/a"
        when = (
            "today" if days == 0
            else "tomorrow" if days == 1
            else f"in {days}d"
        )
        line = (
            f"*{ticker}* — {nxt['report_date'].isoformat()} "
            f"({when}), {eps_str}"
        )
        if days <= EARNINGS_LOOKAHEAD_DAYS:
            within.append((days, line))
        else:
            beyond.append((days, line))

    within.sort(key=lambda x: x[0])
    beyond.sort(key=lambda x: x[0])

    body = ["*Upcoming Earnings (next 30 days)*", ""]
    if within:
        body.extend(line for _, line in within)
    else:
        body.append("_No upcoming earnings in the next 30 days._")

    if beyond:
        body.append("")
        body.append("*Scheduled beyond 30 days:*")
        body.extend(line for _, line in beyond)

    if unavailable:
        body.append("")
        body.append(
            f"_No data (Alpha Vantage didn't return a date — possibly "
            f"rate-limited or no scheduled earnings in 3-month horizon):_ "
            f"{', '.join(unavailable)}"
        )

    _send_chunked("\n".join(body), chat_id)


def scheduled_earnings_check():
    """Daily 08:00 UTC pre-market earnings sweep."""
    if _skip_if_not_trading_day("earnings-check"):
        return
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping earnings check — no chat has interacted yet.")
        return
    print("[scheduled] running earnings calendar check")
    try:
        check_earnings_calendar(chat_id)
    except Exception as exc:
        print(f"[scheduled] earnings check failed: {exc}")


def scheduled_recommendation_review():
    """Daily 07:30 UTC — grade any recs hitting their 4w / 8w window."""
    if _skip_if_not_trading_day("rec-review"):
        return
    chat_id = recall_chat()
    print("[scheduled] running recommendation review check")
    try:
        check_recommendation_reviews(chat_id)
    except Exception as exc:
        print(f"[scheduled] recommendation review failed: {exc}")


def scheduled_weekly_summary():
    """Sunday 08:00 UTC — push a weekly performance recap."""
    if datetime.utcnow().weekday() != 6:   # 6 == Sunday
        return
    chat_id = recall_chat()
    if chat_id is None:
        return
    print("[scheduled] running weekly performance summary")
    try:
        weekly_performance_summary(chat_id)
    except Exception as exc:
        print(f"[scheduled] weekly summary failed: {exc}")


# ---------------------------------------------------------------------------
# Scheduled daily run — time configurable via /settings
# ---------------------------------------------------------------------------
_last_delivery_date = None   # tracks last date portfolio was auto-sent


def scheduled_run():
    """Core daily portfolio delivery — called by the flexible dispatcher below."""
    if _skip_if_not_trading_day("portfolio-run"):
        return
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping scheduled run — no chat has interacted with the bot yet.")
        return
    # Same unified momentum view as /portfolio — one message per position
    # in 🔴 SELL → 🟡 WATCH/HOLD → 🟢 BUY/STRONG BUY order then a summary.
    # ``scheduled=True`` enables the per-position SELL-on-<35 alerts and
    # WARNING alerts when the score dropped >20 pts vs yesterday.
    handle_portfolio(chat_id, scheduled=True)


def _flexible_daily_delivery():
    """Runs every minute — fires ``scheduled_run`` exactly once per trading
    day when the clock matches the user-configured delivery_time_utc.

    Using a per-minute poller lets the user change the delivery time via
    /settings without restarting the bot.
    """
    global _last_delivery_date
    settings = load_settings()
    if not settings.get("daily_delivery", True):
        return
    delivery_time = settings.get("delivery_time_utc", "09:00")
    now_utc = datetime.utcnow()
    current_hhmm = now_utc.strftime("%H:%M")
    today = now_utc.date()
    if current_hhmm == delivery_time and _last_delivery_date != today:
        _last_delivery_date = today
        print(f"[daily-delivery] firing at {delivery_time} UTC")
        scheduled_run()


# ---------------------------------------------------------------------------
# Gap-down pre-market check (08:00 UTC) — catches overnight crashes that
# would push a position below its trailing stop before the user wakes up.
# ---------------------------------------------------------------------------
def _premarket_price(ticker):
    """Best-effort pre-market price for ``ticker``.

    Tries yfinance ``info['preMarketPrice']`` first (liquid names have
    this in extended hours), falls back to ``get_current_price`` (last
    regular-session close) so we can still compare against the trailing
    stop even when no pre-market trade has printed yet.
    """
    try:
        info = yf.Ticker(ticker).info or {}
        for key in ("preMarketPrice", "regularMarketPrice", "currentPrice"):
            v = info.get(key)
            if v is not None and float(v) > 0:
                return float(v)
    except Exception as exc:
        print(f"[gap-down {ticker}] info fetch failed: {exc}")
    return get_current_price(ticker)


def check_gap_down(chat_id=None):
    """Run at 08:00 UTC daily — alert if any position has gapped below
    its trailing stop in the pre-market session. Sends one alert per
    breached position and a single "all clear" line if nothing tripped.
    """
    if _skip_if_not_trading_day("gap-down"):
        return
    if chat_id is None:
        chat_id = recall_chat()
    if chat_id is None:
        print("[gap-down] no chat has interacted yet — skipping")
        return

    portfolio = load_portfolio()
    if not portfolio:
        return

    print(f"[gap-down] scanning {len(portfolio)} positions")
    breached = []
    for ticker, pos in portfolio.items():
        try:
            current = _premarket_price(ticker)
            if current is None:
                print(f"[gap-down {ticker}] no price available")
                continue
            stop_price, stop_mode = compute_trailing_stop(pos, current)
            if stop_price <= 0:
                continue
            if current <= stop_price:
                entry = float(pos.get("entry_price", 0) or 0)
                pl_pct = (
                    ((current - entry) / entry) * 100.0
                    if entry > 0 else 0.0
                )
                mode_txt = (
                    "tightened (-3% from current — watch zone)"
                    if stop_mode == "tightened"
                    else (
                        f"{abs(pos.get('atr_stop_pct', STOP_LOSS_PCT)):.0f}% "
                        f"below peak ${pos.get('peak_price', entry):.2f}"
                    )
                )
                breached.append((ticker, current, stop_price, pl_pct, mode_txt))
        except Exception as exc:
            print(f"[gap-down {ticker}] failed: {exc}")
            continue

    if not breached:
        print("[gap-down] all clear")
        return

    for ticker, current, stop_price, pl_pct, mode_txt in breached:
        send_telegram(
            f"🚨 GAP DOWN ALERT — {ticker}\n"
            f"Pre-market price ${current:.2f} ≤ trailing stop "
            f"${stop_price:.2f} ({mode_txt}).\n"
            f"P&L from entry: {pl_pct:+.1f}%.\n"
            f"Consider exiting at the open to protect capital.",
            chat_id,
            parse_mode=None,
        )


# ---------------------------------------------------------------------------
# Immediate rescan after exit — find a replacement among the top 100
# S&P 500 names that the user doesn't already hold.
# ---------------------------------------------------------------------------
def find_replacement_after_exit(chat_id, exclude=None):
    """Scan the top S&P 500 names for a replacement after a /sell.

    1. Pull fundamentals for the first 100 S&P 500 tickers (a proxy for
       the most liquid mega-cap names).
    2. Filter through ``passes_buy_screen``.
    3. Score momentum on the survivors.
    4. Pick the highest-scoring name with score ≥ 65 that isn't already
       in the user's portfolio and isn't in ``exclude`` (the just-sold
       ticker — and any sector that would now be at the cap).
    5. Send a "💡 Replacement opportunity" message OR a "no replacement
       — holding cash" message + idle-cash BIL/SGOV nudge.

    Runs in a background thread (spawned from /sell), so it can take
    a few minutes without blocking the bot.
    """
    if exclude is None:
        exclude = set()
    else:
        exclude = set(exclude)

    portfolio = load_portfolio()
    held = set(portfolio.keys())
    excluded = held | exclude

    # Sectors already at the cap — skip any candidate in those sectors so
    # the rescan never recommends something the user couldn't /buy anyway.
    sector_counts = {}
    for p in portfolio.values():
        sec = p.get("sector")
        if sec:
            sector_counts[sec] = sector_counts.get(sec, 0) + 1
    capped_sectors = {
        s for s, n in sector_counts.items() if n >= MAX_POSITIONS_PER_SECTOR
    }

    send_telegram(
        "🔍 Scanning top 100 S&P 500 names for a replacement... "
        "(takes ~2 minutes)",
        chat_id,
    )

    try:
        all_tickers = get_sp500_tickers()
    except Exception as exc:
        print(f"[rescan] SP500 fetch failed: {exc}")
        send_telegram(
            "⚠️ Couldn't fetch S&P 500 list — replacement scan skipped.",
            chat_id,
        )
        return

    candidates = [t for t in all_tickers[:100] if t not in excluded]
    if not candidates:
        send_telegram(
            "No candidates left to scan after filtering out current holdings.",
            chat_id,
        )
        return

    try:
        fundamentals = fetch_fundamentals_bulk(candidates)
    except Exception as exc:
        print(f"[rescan] fundamentals failed: {exc}")
        fundamentals = {}

    survivors = [
        f for f in fundamentals.values()
        if passes_buy_screen(f)
        and f.get("ticker") not in excluded
        and (f.get("sector") or "").strip() not in capped_sectors
    ]
    if not survivors:
        _send_no_replacement(chat_id)
        return

    surv_tickers = [f["ticker"] for f in survivors]
    surv_funds = {f["ticker"]: f for f in survivors}

    try:
        news_map = fetch_news_bulk(surv_tickers, days=7)
    except Exception as exc:
        print(f"[rescan] news fetch failed: {exc}")
        news_map = {}

    momentum_map = score_momentum_bulk(
        surv_tickers, fundamentals=surv_funds, news_map=news_map,
    )

    ranked = sorted(
        ((t, s, d) for t, (s, d) in momentum_map.items() if s is not None),
        key=lambda x: x[1],
        reverse=True,
    )
    qualifying = [r for r in ranked if r[1] >= 65]
    if not qualifying:
        _send_no_replacement(chat_id, top_score=ranked[0][1] if ranked else 0)
        return

    ticker, score, d = qualifying[0]
    f = surv_funds.get(ticker, {}) or {}
    current_px = f.get("currentPrice")
    target = _safe_target(f.get("targetMeanPrice"))
    sector = (f.get("sector") or "").strip() or "Unknown"
    upside_line = ""
    if current_px and target:
        upside = ((target - current_px) / current_px) * 100
        upside_line = (
            f"\nAnalyst target: ${target:.2f} "
            f"({upside:+.1f}% upside)"
        )
    price_line = (
        f"Current: ${current_px:.2f}" if current_px else "Current: n/a"
    )
    _send_chunked(
        f"💡 *REPLACEMENT OPPORTUNITY*\n"
        f"After your exit, the top S&P 500 momentum candidate is:\n\n"
        f"🟢 *{ticker}* — Score {score}/100 (BUY)\n"
        f"Sector: {sector}\n"
        f"{price_line}{upside_line}\n"
        f"Price: 1W {_fmt_signed_pct(d.get('ret_1w'))} | "
        f"4W {_fmt_signed_pct(d.get('ret_4w'))} | "
        f"12W {_fmt_signed_pct(d.get('ret_12w'))}\n"
        f"RS vs SPY: {_fmt_signed_pct(d.get('rs_vs_spy'))}\n"
        f"Volume: {_vol_label(d.get('rvol'), d.get('rvol_up_day'))}\n\n"
        f"To buy: `/buy {ticker} SHARES PRICE`",
        chat_id,
    )


def _send_no_replacement(chat_id, top_score=None):
    """Send the 'no replacement found — hold cash' message + BIL/SGOV nudge."""
    try:
        cash = float(load_cash().get("cash", 0.0))
    except Exception:
        cash = 0.0

    parts = [
        "🛑 *NO REPLACEMENT FOUND*",
        "No S&P 500 candidate scored ≥ 65 (BUY threshold) — holding cash "
        "is the right call.",
    ]
    if top_score is not None and top_score > 0:
        parts.append(f"_Top score this scan: {top_score}/100._")
    if cash > IDLE_CASH_THRESHOLD:
        annual_yield = cash * 0.05
        parts.append("")
        parts.append(
            f"💡 *${cash:,.2f} idle* — park in *BIL* or *SGOV* "
            f"(≈5% yield, daily liquidity). "
            f"Estimated income: ~${annual_yield:,.0f}/yr."
        )
    _send_chunked("\n".join(parts), chat_id)


schedule.every().day.at("07:30", "UTC").do(scheduled_recommendation_review)
schedule.every().day.at("08:00", "UTC").do(scheduled_earnings_check)
schedule.every().day.at("08:00", "UTC").do(scheduled_weekly_summary)  # Sundays only
# Pre-market gap-down sweep — runs alongside the earnings/weekly sweep.
schedule.every().day.at("08:00", "UTC").do(check_gap_down)
schedule.every().day.at("08:30", "UTC").do(scheduled_health_check)
# Daily portfolio delivery — time is user-configurable via /settings.
# A per-minute poller fires scheduled_run() when the clock matches the
# configured delivery_time_utc, so time changes take effect immediately.
schedule.every().minute.do(_flexible_daily_delivery)


# ---------------------------------------------------------------------------
# Boot everything (only when run as a script, NOT when imported)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # ── PID-file guard: prevent two instances running simultaneously ──────
    # Replit restarts can leave a zombie process still holding the port and
    # the Telegram polling connection. Writing our PID to a lockfile and
    # killing any previous owner ensures only one instance runs at a time.
    _PID_FILE = "/tmp/portfolio_bot.pid"
    try:
        if os.path.exists(_PID_FILE):
            _old_pid = int(open(_PID_FILE).read().strip())
            if _old_pid != os.getpid():
                try:
                    os.kill(_old_pid, 9)   # SIGKILL — no grace period needed
                    print(f"[boot] killed stale instance PID {_old_pid}")
                except ProcessLookupError:
                    pass  # already gone
        with open(_PID_FILE, "w") as _f:
            _f.write(str(os.getpid()))
    except Exception as _e:
        print(f"[boot] PID guard warning: {_e}")

    # ── Thread 1: Flask web server (portfolio summary + /ping) on port 8080
    t_web = Thread(target=keep_alive, daemon=True, name="web-server")
    t_web.start()

    # ── Thread 2: Telegram long-poll loop
    t_bot = Thread(target=poll_telegram, daemon=True, name="telegram-poll")
    t_bot.start()

    # ── Thread 3 (optional): extended web dashboard on port 8082.
    # Imported here so dashboard.py's lazy `from main import …` resolves
    # to the already-running module without double-executing this script.
    try:
        from dashboard import start_dashboard
        t_dash = Thread(target=start_dashboard, daemon=True, name="dashboard")
        t_dash.start()
        dash_status = "starting on port 8082"
    except Exception as _e:
        print(f"[boot] dashboard skipped: {_e}")
        dash_status = "skipped"

    _web_port = int(os.environ.get("PORT", 5000))
    print(
        "\n"
        "┌─────────────────────────────────────────────────┐\n"
        f"│     Portfolio Bot v{BOT_VERSION} — Momentum System       │\n"
        "│                    STARTED                      │\n"
        "├─────────────────────────────────────────────────┤\n"
        f"│  Web server  : http://0.0.0.0:{_web_port}               │\n"
        f"│  /ping       : http://0.0.0.0:{_web_port}/ping          │\n"
        f"│  Telegram    : polling …                        │\n"
        f"│  Dashboard   : {dash_status:<33} │\n"
        "│  Scheduler   : 08:00/08:30/09:00 UTC jobs       │\n"
        "│  Score 0-100 : price+RS+RVOL+news+earnings      │\n"
        "└─────────────────────────────────────────────────┘\n"
    )

    # ── Main thread: run the schedule loop indefinitely
    while True:
        schedule.run_pending()
        time.sleep(30)
