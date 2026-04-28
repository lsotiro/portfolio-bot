"""Portfolio Bot — a Telegram-controlled portfolio assistant.

Commands you can send the bot:
  /buy TICKER SHARES PRICE   add a position (e.g. /buy AAPL 10 189.50)
  /sell TICKER [SHARES]      sell entire position, or only N shares
  /trim TICKER               sell 50% of a position (quick partial exit)
  /portfolio                 show current holdings with live P&L
  /analyze                   run the daily 5-pillar review on holdings
  /deep TICKER               full 5-pillar deep dive on any single stock
  /monthly                   run the monthly S&P 500 buy screen
  /earnings                  list upcoming earnings (next 30 days)
  /health                    portfolio health score (0–10) with breakdown

The 5-pillar Claude framework scores each stock on Business Quality,
Growth Trajectory, Valuation, Catalyst, and Risk (1-5 each, total /25).
Signals: STRONG BUY 20-25 / BUY 15-19 / HOLD 10-14 / SELL <10. News
headlines also carry +1 / 0 / -1 sentiment via a keyword heuristic.

Daily schedule (UTC):
  08:00 — earnings calendar sweep (alerts for earnings ≤ 3 days away)
  08:30 — morning portfolio health score push
  09:00 — full /analyze portfolio review

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

import csv
import io
import json
import math
import os
import re
import threading
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

# How many days before earnings we send a heads-up alert.
EARNINGS_ALERT_DAYS = 3
# Maximum lookahead window for the manual /earnings command.
EARNINGS_LOOKAHEAD_DAYS = 30
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
# Free-tier AV is 5 requests/minute. Space calls 13s apart to stay safe.
ALPHA_VANTAGE_MIN_INTERVAL_SEC = 13.0

PORTFOLIO_FILE = "portfolio.json"   # where positions are stored
LAST_CHAT_FILE = "last_chat.json"   # remembers chat for scheduled reports
RECS_LOG_FILE = "recommendations_log.json"   # feedback-loop history

# Review windows for recommendation tracking (calendar days).
REVIEW_4W_DAYS = 28
REVIEW_8W_DAYS = 56

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


def get_stock_news(ticker, days=1, page_size=5):
    """Fetch the most recent NewsAPI headlines for a ticker.

    Each article dict carries a `sentiment` field (-1 / 0 / +1) computed
    from the headline via the keyword heuristic above.

    Returns a list of dicts: [{title, source, publishedAt, url, sentiment}].
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
        title = (art.get("title") or "").strip()
        out.append(
            {
                "title": title,
                "source": (art.get("source") or {}).get("name", ""),
                "publishedAt": art.get("publishedAt", "")[:10],
                "url": art.get("url", ""),
                "sentiment": headline_sentiment(title),
            }
        )
    return out


def aggregate_sentiment(articles):
    """Average sentiment across a list of articles, in [-1, +1]."""
    if not articles:
        return 0.0
    return sum(a.get("sentiment", 0) for a in articles) / len(articles)


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
# Daily review — used by /analyze and the scheduler
# ---------------------------------------------------------------------------
def _trigger_deep_for_mover(ticker, move_pct, position, chat_id):
    """Background helper — runs full deep analysis for a >3% single-day mover."""
    try:
        rich = get_rich_fundamentals(ticker)
        if not rich:
            return
        news = get_stock_news(ticker, days=7, page_size=5)
        result = analyze_stock_deep(ticker, rich, position, news)
        send_telegram(
            f"📈 *{ticker}* moved {move_pct:+.1f}% today — auto deep analysis:\n\n"
            f"{result}",
            chat_id,
        )
        # Log to feedback loop so big-mover deep-dives count toward track record.
        try:
            parsed = parse_framework_blocks(result)
            log_recommendations(
                parsed,
                price_lookup=lambda _t: rich.get("currentPrice"),
                analyst_lookup=lambda _t: _safe_target(rich.get("targetMeanPrice")),
                source="daily-mover",
            )
        except Exception as exc:
            print(f"[recs] mover deep-log failed for {ticker}: {exc}")
    except Exception as exc:
        print(f"[mover {ticker}] deep analysis failed: {exc}")


def run_analysis(chat_id):
    """Lightweight daily monitor.

    Workflow:
      1. Bulk-fetch fundamentals (refreshes analyst target for every holding).
      2. For each position: refresh target (alerting on changes for big movers),
         compute today's % move from previousClose, and apply hard SELL rules.
      3. Send a one-line-per-position summary (no Claude framework call).
      4. Spawn background deep analyses for any position moving > 3% today.
      5. Send target-change alerts for any position moving > 5% today whose
         analyst consensus also shifted significantly.
      6. Log forced SELL signals to the feedback-loop log.
    """
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram(
            "Your portfolio is empty. Use /buy TICKER SHARES PRICE first.",
            chat_id,
        )
        return

    send_telegram("*Running daily portfolio monitor…*", chat_id)

    tickers = list(portfolio.keys())
    # Basic bulk fetch is sufficient for the lightweight monitor — gives us
    # currentPrice, regularMarketPreviousClose, and the fresh analyst target.
    fundamentals = fetch_fundamentals_bulk(tickers)

    judged_positions = []
    target_changes = []   # (ticker, old, new) — for any position
    big_movers_3 = []     # >= 3% single-day move → trigger deep analysis
    big_movers_5 = []     # >= 5% single-day move → target-change alert
    portfolio_dirty = False

    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker) or {}
        current = f.get("currentPrice") or get_current_price(ticker)
        prev_close = f.get("regularMarketPreviousClose")

        if current is None:
            judged_positions.append({
                "ticker": ticker, "shares": pos["shares"],
                "entry_price": pos["entry_price"], "current_price": None,
                "analyst_target": _safe_target(pos.get("analyst_target")),
                "pl_pct": None, "move_pct": None,
                "forced_signal": None,
                "forced_reason": "could not fetch price",
            })
            continue

        pl_pct = round(
            ((current - pos["entry_price"]) / pos["entry_price"]) * 100, 2
        )

        # Single-day % move — used to trigger 3%/5% behaviors below.
        move_pct = None
        if prev_close and prev_close > 0:
            move_pct = ((current - prev_close) / prev_close) * 100

        # Refresh the analyst target every morning (replaces the old monthly
        # refresh). Any meaningful change resets the per-position alert flags
        # so the new target gets a fresh evaluation.
        old_target = _safe_target(pos.get("analyst_target"))
        new_target = _safe_target(f.get("targetMeanPrice"))
        if new_target is not None and (
            old_target is None or abs(new_target - old_target) >= 0.01
        ):
            pos["analyst_target"] = new_target
            pos["approach_alerted"] = False
            pos["above_alerted"] = False
            portfolio_dirty = True
            if old_target is not None:
                target_changes.append((ticker, old_target, new_target))
        target = new_target if new_target is not None else old_target

        forced_signal = None
        forced_reason = None

        # --- Hard stop loss ---
        if pl_pct <= STOP_LOSS_PCT:
            forced_signal = "SELL"
            forced_reason = (
                f"hard stop loss hit (P/L {pl_pct}% ≤ {STOP_LOSS_PCT}%)"
            )

        # --- Above analyst fair value ---
        elif target is not None and current >= ABOVE_TARGET_FRACTION * target:
            pct_of_target = (current / target) * 100
            forced_signal = "SELL"
            forced_reason = (
                f"ABOVE FAIR VALUE — ${current:.2f} ≥ ${target:.2f} "
                f"({pct_of_target:.0f}% of target)"
            )
            if not pos.get("above_alerted"):
                send_telegram(
                    f"🚨 *{ticker}* ABOVE FAIR VALUE — strong sell signal. "
                    f"Price ${current:.2f} is {pct_of_target:.0f}% of analyst "
                    f"target ${target:.2f}.",
                    chat_id,
                )
                pos["above_alerted"] = True
                pos["approach_alerted"] = True
                portfolio_dirty = True

        # --- Approaching fair value (soft alert, not a forced SELL) ---
        elif target is not None and current >= APPROACH_TARGET_FRACTION * target:
            pct_of_target = (current / target) * 100
            if not pos.get("approach_alerted"):
                send_telegram(
                    f"⚠️ *{ticker}* APPROACHING FAIR VALUE — consider selling. "
                    f"Price ${current:.2f} is {pct_of_target:.0f}% of "
                    f"analyst target ${target:.2f}.",
                    chat_id,
                )
                pos["approach_alerted"] = True
                portfolio_dirty = True

        judged_positions.append({
            "ticker": ticker,
            "shares": pos["shares"],
            "entry_price": pos["entry_price"],
            "current_price": current,
            "analyst_target": target,
            "pl_pct": pl_pct,
            "move_pct": move_pct,
            "forced_signal": forced_signal,
            "forced_reason": forced_reason,
        })

        if move_pct is not None and abs(move_pct) >= 3.0:
            big_movers_3.append((ticker, move_pct, dict(pos)))
        if (
            move_pct is not None and abs(move_pct) >= 5.0
            and old_target is not None and new_target is not None
        ):
            big_movers_5.append((ticker, move_pct, old_target, new_target))

    if portfolio_dirty:
        save_portfolio(portfolio)

    if not any(p["current_price"] for p in judged_positions):
        send_telegram(
            "Could not fetch current prices for any of your holdings.",
            chat_id,
        )
        return

    # --- Lightweight one-line-per-position summary ---
    lines = ["*Daily portfolio monitor*", ""]
    for p in judged_positions:
        if p["current_price"] is None:
            lines.append(f"⚪ *{p['ticker']}* — price unavailable")
            continue
        signal = p["forced_signal"] or "HOLD"
        if signal == "SELL":
            emoji = "🔴"
        elif p["pl_pct"] >= 0:
            emoji = "🟢"
        else:
            emoji = "🟡"
        move = (
            f" ({p['move_pct']:+.1f}% today)"
            if p["move_pct"] is not None else ""
        )
        tail = f" — {p['forced_reason']}" if p["forced_reason"] else ""
        lines.append(
            f"{emoji} *{p['ticker']}* — ${p['current_price']:.2f}{move} — "
            f"*{signal}* — entry ${p['entry_price']:.2f} "
            f"({p['pl_pct']:+.1f}%){tail}"
        )

    if target_changes:
        lines.append("")
        lines.append("_Analyst targets refreshed:_")
        for t, old, new in target_changes:
            arrow = "↑" if new > old else "↓"
            lines.append(f"  • {t}: ${old:.2f} → ${new:.2f} {arrow}")

    send_telegram("\n".join(lines), chat_id)

    # --- Target-change alerts for big movers (>= 5%) ---
    for ticker, move_pct, old_t, new_t in big_movers_5:
        target_change_pct = ((new_t - old_t) / old_t) * 100
        if abs(target_change_pct) >= 5.0:
            send_telegram(
                f"⚡ *{ticker}* moved *{move_pct:+.1f}% today* AND analyst "
                f"target was revised: ${old_t:.2f} → ${new_t:.2f} "
                f"({target_change_pct:+.1f}%) — re-evaluate the thesis.",
                chat_id,
            )

    # --- Auto-trigger deep analysis on >3% movers (background threads) ---
    for ticker, move_pct, position in big_movers_3:
        Thread(
            target=_trigger_deep_for_mover,
            args=(ticker, move_pct, position, chat_id),
            daemon=True,
        ).start()

    # --- Feedback loop — log forced SELL signals from the daily monitor ---
    try:
        sells = [
            {
                "ticker": p["ticker"],
                "total_score": None,
                "signal": "SELL",
                "claude_target": None,
                "stop_loss": None,
                "bull_case": None,
                "bear_case": p["forced_reason"],
            }
            for p in judged_positions if p["forced_signal"] == "SELL"
        ]
        price_by_t = {p["ticker"]: p["current_price"] for p in judged_positions}
        target_by_t = {p["ticker"]: p["analyst_target"] for p in judged_positions}
        log_recommendations(
            sells,
            price_lookup=lambda t: price_by_t.get(t),
            analyst_lookup=lambda t: target_by_t.get(t),
            source="daily",
        )
    except Exception as exc:
        print(f"[recs] daily SELL logging failed: {exc}")


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

    basic_candidates = [
        f for f in fundamentals_map.values() if passes_buy_screen(f)
    ]
    print(
        f"[monthly] {len(basic_candidates)} stocks pass the hard fundamental "
        f"screen"
    )

    if not basic_candidates:
        send_telegram(
            "No S&P 500 stocks passed the fundamental screen this month.",
            chat_id,
        )
        return

    # Per the spec, run the FULL deep framework only on stocks that passed
    # the hard filter. Fetch rich fundamentals (analyst conviction, EPS
    # trend, etc.) just for those candidates.
    cand_tickers = [c["ticker"] for c in basic_candidates]
    rich_candidates_map = fetch_rich_fundamentals_bulk(cand_tickers)
    rich_candidates = [
        rich_candidates_map[t] for t in cand_tickers if t in rich_candidates_map
    ]
    print(
        f"[monthly] fetched rich fundamentals for {len(rich_candidates)}"
        f" / {len(cand_tickers)} candidates"
    )

    # Fetch 7-day news for each candidate so Claude can weigh recent catalysts.
    news_map = fetch_news_bulk(cand_tickers, days=7)
    print(
        f"[monthly] fetched news for {sum(1 for v in news_map.values() if v)}"
        f" / {len(cand_tickers)} candidates"
    )

    picks = pick_monthly_buys_with_claude(rich_candidates, news_map)
    header = (
        f"*Monthly Buy Screen*\n"
        f"_{len(basic_candidates)} of {len(fundamentals_map)} stocks passed "
        f"the hard filter._\n\n"
    )
    send_telegram(header + picks, chat_id)

    # Feedback loop — log every monthly pick so we can grade it later.
    try:
        rich_by_ticker = {c["ticker"]: c for c in rich_candidates}
        parsed_picks = parse_monthly_picks(picks)
        log_recommendations(
            parsed_picks,
            price_lookup=lambda t: (rich_by_ticker.get(t, {}) or {}).get("currentPrice"),
            analyst_lookup=lambda t: _safe_target((rich_by_ticker.get(t, {}) or {}).get("targetMeanPrice")),
            source="monthly",
        )
    except Exception as exc:
        print(f"[recs] monthly logging failed: {exc}")


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


def _make_rec(ticker, parsed, price, analyst_target, source):
    """Construct one recommendation dict from a parsed framework block."""
    today = _today_iso()
    return {
        "id": f"{today}-{ticker}-{int(time.time())}",
        "date": today,
        "ticker": ticker,
        "source": source,
        "signal": parsed.get("signal"),
        "framework_score": parsed.get("total_score"),
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
                        signals_to_log=None):
    """Append framework blocks to the rec log, optionally filtered by signal.

    `price_lookup(ticker)` returns the price-at-recommendation.
    `analyst_lookup(ticker)` returns the analyst target (or None).
    `signals_to_log` is a set like {"STRONG BUY", "BUY"}; None = log all.
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
            try:
                rec = _make_rec(
                    t, p, price_lookup(t), analyst_lookup(t), source,
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
    send_telegram("\n".join(lines), chat_id)


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
    send_telegram("\n\n".join(lines), chat_id)


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

    # Feedback loop — kick off a deep analysis on the new position so the
    # /buy decision gets logged like any other recommendation. Runs in the
    # background so the user gets the confirmation immediately.
    def _buy_deep_log():
        try:
            rich = get_rich_fundamentals(ticker)
            if not rich:
                return
            news = get_stock_news(ticker, days=7, page_size=5)
            position = load_portfolio().get(ticker)
            result = analyze_stock_deep(ticker, rich, position, news)
            send_telegram(
                f"*Deep analysis logged for {ticker}:*\n\n{result}",
                chat_id,
            )
            parsed = parse_framework_blocks(result)
            log_recommendations(
                parsed,
                # Use the user's actual fill price as the rec baseline.
                price_lookup=lambda _t: price,
                analyst_lookup=lambda _t: target,
                source="buy",
            )
        except Exception as exc:
            print(f"[/buy {ticker}] deep-log failed: {exc}")

    Thread(target=_buy_deep_log, daemon=True).start()


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

    # Top-of-message health summary — reuses the bulk fundamentals we just
    # fetched so this adds zero extra API calls.
    try:
        health = calculate_health_score(portfolio, fundamentals)
        summary = format_health_summary(health)
        if summary:
            lines.insert(1, summary)
    except Exception as exc:
        print(f"[health] portfolio summary failed: {exc}")

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
        # Tri-state emoji: green above entry, yellow between entry & stop,
        # red at or below the stop loss.
        pl_emoji = _status_emoji(pl_pct)
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


def handle_deep(args, chat_id):
    """`/deep TICKER` — run the full 5-pillar framework on any stock."""
    if not args:
        send_telegram(
            "Usage: /deep TICKER\nExample: /deep NVDA",
            chat_id,
        )
        return
    ticker = args[0].upper()
    send_telegram(
        f"*Running deep analysis on {ticker}...*\n"
        "Pulling rich fundamentals, earnings trend & news. ~20s.",
        chat_id,
    )

    def _run():
        try:
            rich = get_rich_fundamentals(ticker)
            if not rich or not rich.get("currentPrice"):
                send_telegram(
                    f"Could not fetch fundamentals for *{ticker}*. "
                    f"Check the ticker spelling.",
                    chat_id,
                )
                return
            portfolio = load_portfolio()
            position = portfolio.get(ticker)
            news = get_stock_news(ticker, days=7, page_size=5)
            result = analyze_stock_deep(ticker, rich, position, news)
            news_summary = (
                f"News sentiment: {sentiment_label(aggregate_sentiment(news))} "
                f"({len(news)} headlines, last 7d)"
                if news else "News: (no headlines available)"
            )
            send_telegram(f"{result}\n\n_{news_summary}_", chat_id)
            # Feedback loop — log this on-demand recommendation.
            try:
                parsed = parse_framework_blocks(result)
                log_recommendations(
                    parsed,
                    price_lookup=lambda _t: rich.get("currentPrice"),
                    analyst_lookup=lambda _t: _safe_target(rich.get("targetMeanPrice")),
                    source="deep",
                )
            except Exception as exc:
                print(f"[recs] /deep logging failed: {exc}")
        except Exception as exc:
            print(f"[/deep {ticker}] error: {exc}")
            send_telegram(
                f"Deep analysis for *{ticker}* failed: {exc}",
                chat_id,
            )

    Thread(target=_run, daemon=True).start()


def handle_start(chat_id):
    send_telegram(
        "Welcome to *Portfolio Bot*\n\n"
        "Commands:\n"
        "`/buy TICKER SHARES PRICE` — add a position\n"
        "`/sell TICKER [SHARES]` — sell whole position or N shares\n"
        "`/trim TICKER` — sell 50% of a position (quick partial exit)\n"
        "`/portfolio` — holdings with live P&L, analyst target & upside\n"
        "`/health` — portfolio health score (0–10) with breakdown\n"
        "`/earnings` — upcoming earnings dates (next 30 days)\n"
        "`/analyze` — daily 5-pillar review on holdings\n"
        "`/deep TICKER` — full 5-pillar deep dive on any stock\n"
        "`/monthly` — monthly S&P 500 buy screen (top 2 picks)\n"
        "`/review` — open recommendations + 4w/8w countdowns\n"
        "`/performance` — track record vs S&P 500\n\n"
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
                    elif cmd == "/earnings":
                        handle_earnings(chat_id)
                    elif cmd == "/health":
                        handle_health(chat_id)
                    elif cmd == "/performance":
                        handle_performance(chat_id)
                    elif cmd == "/review":
                        handle_review(chat_id)
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

    stop_factor = 1.0 + (STOP_LOSS_PCT / 100.0)

    rows = []
    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker) or {}
        current = _safe_float(f.get("currentPrice"))
        if current is None:
            current = _safe_float(get_current_price(ticker))
        target = _safe_target(pos.get("analyst_target"))
        if target is None:
            target = _safe_target(f.get("targetMeanPrice"))
        sector_raw = f.get("sector")
        sector = sector_raw.strip() if isinstance(sector_raw, str) else None
        rows.append(
            {
                "ticker": ticker,
                "entry": pos["entry_price"],
                "shares": pos["shares"],
                "current": current,
                "target": target,
                "sector": sector or None,
                "stop_price": pos["entry_price"] * stop_factor,
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

    return {
        "score": total,
        "rating_emoji": emoji,
        "rating_label": label,
        "components": components,
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
    return "\n".join(lines)


def handle_health(chat_id):
    """`/health` — show the score and the four-component breakdown."""
    portfolio = load_portfolio()
    if not portfolio:
        send_telegram("Your portfolio is empty.", chat_id)
        return
    report = calculate_health_score(portfolio)
    send_telegram(format_health_breakdown(report), chat_id)


def scheduled_health_check():
    """Daily 08:30 UTC morning health check (between earnings and analysis)."""
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

        msg = (
            f"⚠️ *EARNINGS ALERT*\n\n"
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

    send_telegram("\n".join(body), chat_id)


def scheduled_earnings_check():
    """Daily 08:00 UTC pre-market earnings sweep."""
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
# Scheduled daily run at 09:00 UTC
# ---------------------------------------------------------------------------
def scheduled_run():
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping scheduled run — no chat has interacted with the bot yet.")
        return
    # The daily monitor itself now refreshes analyst targets for every
    # holding on every run, so the old day-1-of-month bulk refresh is
    # no longer needed.
    run_analysis(chat_id)


schedule.every().day.at("07:30", "UTC").do(scheduled_recommendation_review)
schedule.every().day.at("08:00", "UTC").do(scheduled_earnings_check)
schedule.every().day.at("08:00", "UTC").do(scheduled_weekly_summary)  # Sundays only
schedule.every().day.at("08:30", "UTC").do(scheduled_health_check)
# Daily monitor runs at 21:30 UTC — 30 min after US market close
# (16:00 ET = 20:00/21:00 UTC depending on DST). This is the earliest
# point at which currentPrice and regularMarketPreviousClose differ in
# a way that lets the >3% / >5% mover logic actually fire — running it
# pre-open would compare yesterday's close to itself.
schedule.every().day.at("21:30", "UTC").do(scheduled_run)


# ---------------------------------------------------------------------------
# Boot everything (only when run as a script, NOT when imported)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    Thread(target=keep_alive, daemon=True).start()
    Thread(target=poll_telegram, daemon=True).start()

    print("Portfolio Bot started. Daily analysis scheduled at 21:30 UTC "
          "(post US market close).")
    while True:
        schedule.run_pending()
        time.sleep(30)
