"""Portfolio Bot — a Telegram-controlled portfolio assistant.

Commands you can send the bot:
  /buy TICKER SHARES PRICE   add a position (e.g. /buy AAPL 10 189.50)
  /sell TICKER [SHARES]      sell entire position, or only N shares
  /trim TICKER               sell 50% of a position (quick partial exit)
  /portfolio                 show current holdings with live P&L
  /analyze                   run the daily HOLD/SELL review on holdings
  /monthly                   run the monthly S&P 500 buy screen

Once a day at 09:00 UTC the bot also runs /analyze automatically.

Hard rules applied to every holding before Claude is consulted:
  - Stop loss        : forced SELL if down 7% from entry (capital protection)
  - Above fair value : forced SELL when price ≥ analyst consensus target
                       (strong sell signal)
  - Approaching FV   : soft alert when price ≥ 90% of analyst target
                       (consider selling — limited remaining upside)
  - Big upside       : info line when price ≤ 70% of analyst target
                       (significant remaining upside)
Take-profit is now driven entirely by the analyst consensus target stored
on each position — there is no flat % target. Targets are fetched when a
position is added and refreshed on the 1st of every month.
"""

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from threading import Thread

import anthropic
import requests
import schedule
import yfinance as yf
from flask import Flask

# Hard trade rules (always override fundamentals)
STOP_LOSS_PCT = -7.0              # forced SELL at -7% (capital protection)
ABOVE_TARGET_FRACTION = 1.00      # forced SELL when price ≥ analyst target
APPROACH_TARGET_FRACTION = 0.90   # soft alert when price ≥ 90% of target
LOW_TARGET_FRACTION = 0.70        # info: price ≤ 70% of target → big upside


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

PORTFOLIO_FILE = "portfolio.json"   # where positions are stored
LAST_CHAT_FILE = "last_chat.json"   # remembers chat for scheduled reports

NEWS_ENDPOINT = "https://newsapi.org/v2/everything"


# ---------------------------------------------------------------------------
# Flask keep-alive web server
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is running"


def keep_alive():
    """Run the Flask server. Started in a background thread."""
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


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


def load_portfolio():
    return load_json(PORTFOLIO_FILE, {})


def save_portfolio(portfolio):
    save_json(PORTFOLIO_FILE, portfolio)


def remember_chat(chat_id):
    """Persist the most recent chat ID so the scheduled job knows where to send."""
    save_json(LAST_CHAT_FILE, {"chat_id": chat_id})


def recall_chat():
    return load_json(LAST_CHAT_FILE, {}).get("chat_id")


# ---------------------------------------------------------------------------
# Telegram messaging
# ---------------------------------------------------------------------------
def send_telegram(message, chat_id):
    """Send a Markdown message to a Telegram chat."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
        timeout=30,
    )


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
    """Return the first 100 S&P 500 tickers from a public dataset."""
    url = (
        "https://raw.githubusercontent.com/datasets/"
        "s-and-p-500-companies/main/data/constituents.csv"
    )
    response = urllib.request.urlopen(url)
    lines = response.read().decode().split("\n")[1:]
    tickers = [line.split(",")[0] for line in lines if line.strip()]
    return tickers[:100]


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


def get_stock_news(ticker, days=1, page_size=5):
    """Fetch the most recent NewsAPI headlines for a ticker.

    Returns a list of dicts: [{title, source, publishedAt, url}, ...]
    Empty list on any error or if NEWS_API_KEY is missing.
    """
    if not NEWS_API_KEY:
        return []
    # NewsAPI accepts ISO-8601 'from' timestamps. Use UTC `days` ago.
    from_dt = datetime.utcnow() - timedelta(days=days)
    params = {
        "q": ticker,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "apiKey": NEWS_API_KEY,
    }
    try:
        r = requests.get(NEWS_ENDPOINT, params=params, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    out = []
    for art in (data.get("articles") or [])[:page_size]:
        out.append(
            {
                "title": (art.get("title") or "").strip(),
                "source": (art.get("source") or {}).get("name", ""),
                "publishedAt": art.get("publishedAt", "")[:10],
                "url": art.get("url", ""),
            }
        )
    return out


def fetch_news_bulk(tickers, days=1, max_workers=10):
    """Fetch news for many tickers in parallel. Returns {ticker: [articles]}."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(get_stock_news, t, days): t for t in tickers
        }
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results[t] = fut.result() or []
            except Exception:
                results[t] = []
    return results


def format_news_block(ticker, articles):
    """One-block summary of news for a ticker, suitable for prompts."""
    if not articles:
        return f"{ticker}: (no recent news)"
    lines = [f"{ticker}:"]
    for a in articles:
        date = a.get("publishedAt") or "?"
        src = a.get("source") or "?"
        title = a.get("title") or "(no title)"
        lines.append(f"  - [{date}] {src}: {title}")
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
def analyze_portfolio_with_claude(judged_positions, news_map):
    """Ask Claude for HOLD/SELL on each non-hard-rule position.

    `judged_positions` is a list of dicts that already had the hard
    stop-loss / take-profit rule applied. `news_map` is {ticker: [articles]}
    for the last 24 hours.

    Claude is also asked to flag CRITICAL negative news (scandal, guidance
    cut, CEO resignation, fraud) in a separate URGENT ALERTS block which the
    caller parses and forwards as immediate Telegram alerts.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    rows = []
    for p in judged_positions:
        f = p["fundamentals"] or {}
        rows.append(
            f"{p['ticker']}: {p['shares']} shares, entry ${p['entry_price']}, "
            f"current ${p['current_price']}, P/L {p['pl_pct']}% | "
            f"PE(t/f)={format_fund(f.get('trailingPE'))}/"
            f"{format_fund(f.get('forwardPE'))}, "
            f"revGrowth={format_fund(f.get('revenueGrowth'))}, "
            f"earnGrowth={format_fund(f.get('earningsGrowth'))}, "
            f"margin={format_fund(f.get('profitMargins'))}, "
            f"D/E={format_fund(f.get('debtToEquity'))}, "
            f"ROE={format_fund(f.get('returnOnEquity'))}, "
            f"analystTarget=${format_fund(f.get('targetMeanPrice'))}, "
            f"rec={format_fund(f.get('recommendationMean'))}"
        )

    portfolio_text = "\n".join(rows)
    forced_text = "\n".join(
        f"{p['ticker']}: {p['forced_signal']} — {p['forced_reason']}"
        for p in judged_positions
        if p.get("forced_signal")
    ) or "(none)"

    news_text = "\n\n".join(
        format_news_block(p["ticker"], news_map.get(p["ticker"], []))
        for p in judged_positions
    ) or "(no news available)"

    prompt = f"""You are a senior equity analyst doing a daily portfolio review.

CURRENT POSITIONS (live price, P/L, fundamentals):
{portfolio_text}

POSITIONS ALREADY MARKED SELL BY HARD RULES (do not change these):
{forced_text}

RECENT NEWS (last 24h, NewsAPI headlines):
{news_text}

For every position NOT already in the hard-rule list above, output a clear
HOLD or SELL signal with ONE sentence of reasoning. Use price action,
fundamentals AND news together. Consider:
  - revenue growth, earnings growth, profit margins, ROE trend
  - valuation (forward PE, analyst target vs current price)
  - leverage (debt/equity)
  - analyst consensus (recommendationMean — lower is more bullish)
  - news flow / sentiment shifts
  - momentum reversal or clearly stronger opportunities elsewhere

Default to HOLD when the stock is still meaningfully below its analyst
consensus target (significant remaining upside) and fundamentals/news are
not deteriorating. Lean SELL when the upside to target has largely been
captured even if no hard rule has triggered yet.

ADDITIONALLY, scan the news for any CRITICAL negative event for any holding:
  - accounting scandal or fraud
  - earnings / guidance cut, missed forecast
  - CEO/CFO resignation or termination
  - regulatory action, lawsuit with material impact
  - major data breach or operational failure
List each one in an URGENT ALERTS section. If there are none, write
"URGENT ALERTS: (none)".

Format the reply EXACTLY like this:

PORTFOLIO REVIEW:
TICKER — HOLD or SELL — reason
TICKER — HOLD or SELL — reason
...

URGENT ALERTS:
TICKER — short description of the critical event (one line each)
(or "(none)")

Be direct and concise."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
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
def pick_monthly_buys_with_claude(candidates, news_map):
    """Ask Claude to pick the top 2 BUY opportunities from screened candidates.

    `news_map` is {ticker: [articles]} for the last 7 days.
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    rows = []
    for c in candidates:
        rows.append(
            f"{c['ticker']}: price=${format_fund(c.get('currentPrice'))}, "
            f"PE(t/f)={format_fund(c.get('trailingPE'))}/"
            f"{format_fund(c.get('forwardPE'))}, "
            f"revGrowth={format_fund(c.get('revenueGrowth'))}, "
            f"earnGrowth={format_fund(c.get('earningsGrowth'))}, "
            f"margin={format_fund(c.get('profitMargins'))}, "
            f"D/E={format_fund(c.get('debtToEquity'))}, "
            f"ROE={format_fund(c.get('returnOnEquity'))}, "
            f"analystTarget=${format_fund(c.get('targetMeanPrice'))}, "
            f"rec={format_fund(c.get('recommendationMean'))}"
        )
    candidates_text = "\n".join(rows)

    news_text = "\n\n".join(
        format_news_block(c["ticker"], news_map.get(c["ticker"], []))
        for c in candidates
    ) or "(no news available)"

    prompt = f"""You are a senior equity analyst running a monthly buy screen.

The candidates below have ALREADY passed a strict fundamentals filter:
revenue growth > 15%, earnings growth > 10%, profit margin > 15%,
ROE > 15%, forward PE < 25, analyst recommendationMean < 2.0.

CANDIDATES:
{candidates_text}

RECENT NEWS (last 7 days, NewsAPI headlines):
{news_text}

Pick the TOP 2 BUY opportunities. Use both fundamentals AND news flow —
strong recent news / catalyst is a tailwind; bad news (scandal, guidance
cut, executive departure, fraud) DISQUALIFIES a candidate even if the
fundamentals look good. For each, decide a conviction rating:
  - STRONG → suggested position size $500
  - MEDIUM → suggested position size $300

For each pick, also produce:
  - Target price (concrete dollar amount, not a percentage)
  - Stop loss price (concrete dollar amount)
  - Expected holding period in weeks
  - Exactly 3 bullet point reasons grounded in the fundamentals shown

Format your reply EXACTLY like this (no extra prose before or after):

MONTHLY BUY PICKS:

1) TICKER — STRONG or MEDIUM — Position: $500 or $300
   Target: $X | Stop: $Y | Hold: N weeks
   - reason 1
   - reason 2
   - reason 3

2) TICKER — STRONG or MEDIUM — Position: $500 or $300
   Target: $X | Stop: $Y | Hold: N weeks
   - reason 1
   - reason 2
   - reason 3

If fewer than 2 candidates are truly compelling, output only the strong
one(s) and add a final line: "No second pick this month."
"""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return next(
        (block.text for block in message.content if hasattr(block, "text")),
        "No analysis available",
    )


# ---------------------------------------------------------------------------
# Daily review — used by /analyze and the scheduler
# ---------------------------------------------------------------------------
def run_analysis(chat_id):
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram(
            "Your portfolio is empty. Use /buy TICKER SHARES PRICE first.",
            chat_id,
        )
        return

    send_telegram(
        "*Running daily portfolio review...*\n"
        "Fetching live prices and fundamentals.",
        chat_id,
    )

    tickers = list(portfolio.keys())
    fundamentals = fetch_fundamentals_bulk(tickers)

    judged_positions = []
    forced_lines = []
    alert_lines = []  # soft alerts (approaching fair value)
    info_lines = []   # informational (big upside remaining, etc.)
    portfolio_dirty = False  # set True if state changes & needs saving

    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker)
        # Prefer the live price embedded in fundamentals; fall back otherwise.
        current = (f or {}).get("currentPrice") or get_current_price(ticker)
        if current is None:
            forced_lines.append(f"{ticker} — UNKNOWN — could not fetch price")
            continue
        pl_pct = round(
            ((current - pos["entry_price"]) / pos["entry_price"]) * 100, 2
        )

        # Resolve the analyst target. Prefer the value stored at /buy time.
        # If the position pre-dates the new system, lazily fetch and persist.
        # _safe_target normalises 0/NaN/None to None so the math stays safe.
        target = _safe_target(pos.get("analyst_target"))
        if target is None and f:
            live_target = _safe_target(f.get("targetMeanPrice"))
            if live_target is not None:
                target = live_target
                pos["analyst_target"] = target
                portfolio_dirty = True

        # Per-position alert state for the target-based system.
        approach_alerted = pos.get("approach_alerted", False)
        above_alerted = pos.get("above_alerted", False)

        forced_signal = None
        forced_reason = None

        # --- Hard stop loss (capital protection — always wins) ---
        if pl_pct <= STOP_LOSS_PCT:
            forced_signal = "SELL"
            forced_reason = (
                f"hard stop loss hit (P/L {pl_pct}% ≤ {STOP_LOSS_PCT}%)"
            )

        # --- Above analyst fair value (forced SELL + strong alert) ---
        elif target is not None and current >= ABOVE_TARGET_FRACTION * target:
            pct_of_target = (current / target) * 100
            forced_signal = "SELL"
            forced_reason = (
                f"ABOVE FAIR VALUE — strong sell signal "
                f"(${current:.2f} ≥ ${target:.2f} target — "
                f"{pct_of_target:.0f}% of target)"
            )
            if not above_alerted:
                send_telegram(
                    f"🚨 *{ticker}* ABOVE FAIR VALUE — strong sell signal. "
                    f"Price ${current:.2f} is now "
                    f"{pct_of_target:.0f}% of analyst target ${target:.2f}.",
                    chat_id,
                )
                pos["above_alerted"] = True
                # If we're above fair value we're also past the approach line.
                pos["approach_alerted"] = True
                portfolio_dirty = True

        # --- Approaching fair value (soft alert, not a forced SELL) ---
        elif target is not None and current >= APPROACH_TARGET_FRACTION * target:
            pct_of_target = (current / target) * 100
            if not approach_alerted:
                send_telegram(
                    f"⚠️ *{ticker}* APPROACHING FAIR VALUE — consider selling. "
                    f"Price ${current:.2f} is {pct_of_target:.0f}% of "
                    f"analyst target ${target:.2f}.",
                    chat_id,
                )
                pos["approach_alerted"] = True
                portfolio_dirty = True
            alert_lines.append(
                f"{ticker} — APPROACHING FAIR VALUE — ${current:.2f} is "
                f"{pct_of_target:.0f}% of ${target:.2f} target "
                f"(consider selling)"
            )

        # --- Big upside remaining (info only, no alert) ---
        elif target is not None and current <= LOW_TARGET_FRACTION * target:
            upside_dollar = target - current
            upside_pct = ((target - current) / current) * 100
            info_lines.append(
                f"{ticker} — UPSIDE — +${upside_dollar:.2f}/share "
                f"({upside_pct:+.0f}%) remaining to analyst target "
                f"${target:.2f} (current ${current:.2f})"
            )

        judged_positions.append(
            {
                "ticker": ticker,
                "shares": pos["shares"],
                "entry_price": pos["entry_price"],
                "current_price": current,
                "analyst_target": target,
                "pl_pct": pl_pct,
                "fundamentals": f,
                "forced_signal": forced_signal,
                "forced_reason": forced_reason,
            }
        )
        if forced_signal:
            forced_lines.append(
                f"{ticker} — {forced_signal} — {forced_reason}"
            )

    # Persist any newly-fired alert flags or lazy-fetched targets.
    if portfolio_dirty:
        save_portfolio(portfolio)

    if not judged_positions:
        send_telegram(
            "Could not fetch current prices for any of your holdings.",
            chat_id,
        )
        return

    # Fetch last-24h news for every holding (in parallel) so Claude has it.
    held_tickers = [p["ticker"] for p in judged_positions]
    news_map = fetch_news_bulk(held_tickers, days=1)
    print(
        f"[daily] fetched news for {sum(1 for v in news_map.values() if v)}"
        f" / {len(held_tickers)} tickers"
    )

    # Ask Claude to judge the remaining (non-forced) positions, with news.
    needs_claude = [p for p in judged_positions if not p["forced_signal"]]
    if needs_claude:
        claude_output = analyze_portfolio_with_claude(judged_positions, news_map)
    else:
        claude_output = (
            "PORTFOLIO REVIEW:\n(all positions decided by hard rules)\n\n"
            "URGENT ALERTS: (none)"
        )

    # Forward each URGENT ALERT as its own immediate Telegram message.
    urgent = parse_urgent_alerts(claude_output)
    for line in urgent:
        send_telegram(f"🚨 *URGENT* — {line}", chat_id)

    sections = ["*Daily Portfolio Report*", ""]
    if forced_lines:
        sections.append("*Hard-rule SELLs (override fundamentals):*")
        sections.extend(forced_lines)
        sections.append("")
    if alert_lines:
        sections.append("*Approaching fair value:*")
        sections.extend(alert_lines)
        sections.append("")
    if info_lines:
        sections.append("*Upside remaining (well below fair value):*")
        sections.extend(info_lines)
        sections.append("")
    sections.append(claude_output)
    send_telegram("\n".join(sections), chat_id)


# ---------------------------------------------------------------------------
# Monthly buy screen — used by /monthly
# ---------------------------------------------------------------------------
def run_monthly_screen(chat_id):
    send_telegram(
        "*Running monthly S&P 500 buy screen...*\n"
        "Fetching fundamentals for ~100 stocks. This takes 1-2 minutes.",
        chat_id,
    )

    tickers = get_sp500_tickers()
    fundamentals_map = fetch_fundamentals_bulk(tickers)
    print(
        f"[monthly] fetched fundamentals for {len(fundamentals_map)}"
        f" / {len(tickers)} tickers"
    )

    candidates = [
        f for f in fundamentals_map.values() if passes_buy_screen(f)
    ]
    print(
        f"[monthly] {len(candidates)} stocks pass the hard fundamental screen"
    )

    if not candidates:
        send_telegram(
            "No S&P 500 stocks passed the fundamental screen this month.",
            chat_id,
        )
        return

    # Fetch 7-day news for each candidate so Claude can weigh recent catalysts.
    cand_tickers = [c["ticker"] for c in candidates]
    news_map = fetch_news_bulk(cand_tickers, days=7)
    print(
        f"[monthly] fetched news for {sum(1 for v in news_map.values() if v)}"
        f" / {len(cand_tickers)} candidates"
    )

    picks = pick_monthly_buys_with_claude(candidates, news_map)
    header = (
        f"*Monthly Buy Screen*\n"
        f"_{len(candidates)} of {len(fundamentals_map)} stocks passed "
        f"the hard filter._\n\n"
    )
    send_telegram(header + picks, chat_id)


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

    # Fetch the analyst consensus target right at buy time and store it.
    fund = get_fundamentals(ticker)
    target = _safe_target((fund or {}).get("targetMeanPrice"))

    portfolio = load_portfolio()
    portfolio[ticker] = {
        "shares": shares,
        "entry_price": price,
        "added": datetime.utcnow().isoformat(timespec="seconds"),
        "analyst_target": target,
        # alert state for the new target-based system
        "approach_alerted": False,
        "above_alerted": False,
    }
    save_portfolio(portfolio)

    if target:
        upside = ((target - price) / price) * 100
        send_telegram(
            f"Added *{ticker}* — {shares} shares @ ${price:.2f}\n"
            f"Analyst target: ${target:.2f} ({upside:+.1f}% upside)",
            chat_id,
        )
    else:
        send_telegram(
            f"Added *{ticker}* — {shares} shares @ ${price:.2f}\n"
            f"_Analyst target unavailable._",
            chat_id,
        )


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

    if remaining <= 0:
        del portfolio[ticker]
        save_portfolio(portfolio)
        send_telegram(
            f"Sold {sell_qty} *{ticker}* shares. Position closed.",
            chat_id,
        )
    else:
        pos["shares"] = remaining
        save_portfolio(portfolio)
        send_telegram(
            f"Sold {sell_qty} *{ticker}* shares. "
            f"Remaining position: {remaining} shares @ ${entry:.2f}",
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

    if remaining <= 0:
        del portfolio[ticker]
        save_portfolio(portfolio)
        send_telegram(
            f"Trimmed *{ticker}* — sold {sell_qty} shares (full position).",
            chat_id,
        )
    else:
        pos["shares"] = remaining
        save_portfolio(portfolio)
        send_telegram(
            f"Trimmed *{ticker}* — sold {sell_qty} shares (50%). "
            f"Remaining position: {remaining} shares @ ${entry:.2f}",
            chat_id,
        )


def handle_portfolio(chat_id):
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram(
            "Your portfolio is empty. Use /buy TICKER SHARES PRICE to add one.",
            chat_id,
        )
        return

    lines = ["*Your Portfolio*", ""]
    total_cost = 0.0
    total_value = 0.0

    # Stop loss is negative (e.g. -7) — convert to a price multiplier.
    stop_factor = 1.0 + (STOP_LOSS_PCT / 100.0)  # e.g. 0.93

    # Bulk-fetch current prices + analyst targets in one shot. Lazily fill
    # any missing analyst_target on existing positions while we're at it.
    tickers = list(portfolio.keys())
    fundamentals = fetch_fundamentals_bulk(tickers)
    portfolio_dirty = False

    for ticker, pos in portfolio.items():
        shares = pos["shares"]
        entry = pos["entry_price"]
        f = fundamentals.get(ticker)
        current = (f or {}).get("currentPrice") or get_current_price(ticker)

        # Resolve analyst target: stored value wins, else live fetch & persist.
        # _safe_target normalises 0/NaN/None so display & math stay consistent.
        target = _safe_target(pos.get("analyst_target"))
        if target is None and f:
            live_target = _safe_target(f.get("targetMeanPrice"))
            if live_target is not None:
                target = live_target
                pos["analyst_target"] = target
                portfolio_dirty = True

        stop_price = entry * stop_factor

        # First line: ticker, shares, entry price
        lines.append(f"*{ticker}* — {shares} shares @ ${entry:.2f}")

        if current is None:
            lines.append("Current: _price unavailable_")
            lines.append(
                f"Stop @ ${stop_price:.2f} ({STOP_LOSS_PCT:+.0f}%) | "
                f"Target: "
                + (f"${target:.2f}" if target else "_unavailable_")
            )
            lines.append("")
            continue

        pl_dollar = (current - entry) * shares
        pl_pct = ((current - entry) / entry) * 100
        pl_emoji = "🟢" if pl_dollar >= 0 else "🔴"
        pl_sign = "+" if pl_dollar >= 0 else "-"

        # Line 2: Current vs Entry, P&L
        lines.append(
            f"Entry ${entry:.2f} → Current ${current:.2f} | "
            f"P&L: {pl_sign}${abs(pl_dollar):.2f} "
            f"({pl_sign}{abs(pl_pct):.1f}%) {pl_emoji}"
        )

        # Line 3: Analyst target + % upside remaining (or "above target")
        if target:
            upside_pct = ((target - current) / current) * 100
            if upside_pct >= 0:
                target_label = (
                    f"Target ${target:.2f} | "
                    f"Upside: +{upside_pct:.1f}% "
                    f"(${target - current:+.2f}/share)"
                )
            else:
                # Above the analyst target — show how far past
                pct_of_target = (current / target) * 100
                target_label = (
                    f"Target ${target:.2f} | "
                    f"*Above target* — {pct_of_target:.0f}% of target "
                    f"(${current - target:+.2f}/share over)"
                )
        else:
            target_label = "Target _unavailable_"

        lines.append(target_label)

        # Line 4: stop loss price + distance
        dist_to_stop = current - stop_price
        if dist_to_stop >= 0:
            stop_label = (
                f"Stop @ ${stop_price:.2f} ({STOP_LOSS_PCT:+.0f}%) → "
                f"${dist_to_stop:.2f}/share above stop"
            )
        else:
            stop_label = (
                f"Stop @ ${stop_price:.2f} ({STOP_LOSS_PCT:+.0f}%) → "
                f"*${abs(dist_to_stop):.2f}/share BELOW stop*"
            )
        lines.append(stop_label)

        total_cost += entry * shares
        total_value += current * shares
        lines.append("")  # blank line between positions

    if portfolio_dirty:
        save_portfolio(portfolio)

    if total_cost > 0:
        total_pl = total_value - total_cost
        total_pct = (total_pl / total_cost) * 100
        emoji = "🟢" if total_pl >= 0 else "🔴"
        sign = "+" if total_pl >= 0 else "-"
        lines.append(
            f"*Total* — Cost: ${total_cost:.2f} | "
            f"Value: ${total_value:.2f} | "
            f"P&L: {sign}${abs(total_pl):.2f} "
            f"({sign}{abs(total_pct):.1f}%) {emoji}"
        )

    send_telegram("\n".join(lines), chat_id)


def handle_start(chat_id):
    send_telegram(
        "Welcome to *Portfolio Bot*\n\n"
        "Commands:\n"
        "`/buy TICKER SHARES PRICE` — add a position\n"
        "`/sell TICKER [SHARES]` — sell whole position or N shares\n"
        "`/trim TICKER` — sell 50% of a position (quick partial exit)\n"
        "`/portfolio` — holdings with live P&L, analyst target & upside\n"
        "`/analyze` — daily HOLD/SELL review on holdings\n"
        "`/monthly` — monthly S&P 500 buy screen (top 2 picks)\n\n"
        "_Hard SELL rules:_\n"
        "_• -7% stop loss (capital protection)_\n"
        "_• price ≥ analyst target (above fair value — strong sell)_\n"
        "_Soft alert:_\n"
        "_• price ≥ 90% of analyst target (approaching fair value)_\n"
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
                        handle_portfolio(chat_id)
                    elif cmd == "/analyze":
                        # Acknowledge immediately, then run analysis in a
                        # background thread so polling stays responsive.
                        send_telegram("Analysis started, please wait...", chat_id)
                        Thread(
                            target=run_analysis, args=(chat_id,), daemon=True
                        ).start()
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
# Scheduled daily run at 09:00 UTC
# ---------------------------------------------------------------------------
def scheduled_run():
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping scheduled run — no chat has interacted with the bot yet.")
        return
    # On the 1st of the month, refresh analyst targets BEFORE running
    # the analysis so the day's review uses the freshest numbers.
    if datetime.utcnow().day == 1:
        print("[monthly] refreshing analyst targets")
        try:
            refresh_analyst_targets(chat_id=chat_id)
        except Exception as exc:
            print(f"[monthly] target refresh failed: {exc}")
    run_analysis(chat_id)


schedule.every().day.at("09:00", "UTC").do(scheduled_run)


# ---------------------------------------------------------------------------
# Boot everything (only when run as a script, NOT when imported)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    Thread(target=keep_alive, daemon=True).start()
    Thread(target=poll_telegram, daemon=True).start()

    print("Portfolio Bot started. Daily analysis scheduled at 09:00 UTC.")
    while True:
        schedule.run_pending()
        time.sleep(30)
