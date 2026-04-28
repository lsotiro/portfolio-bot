"""Portfolio Bot — a Telegram-controlled portfolio assistant.

Commands you can send the bot:
  /buy TICKER SHARES PRICE   add a position (e.g. /buy AAPL 10 189.50)
  /sell TICKER               remove a position from the portfolio
  /portfolio                 show current holdings with live P&L
  /analyze                   run the daily HOLD/SELL review on holdings
  /monthly                   run the monthly S&P 500 buy screen

Once a day at 09:00 UTC the bot also runs /analyze automatically.

Hard rules applied to every holding before Claude is consulted:
  - Stop loss   : forced SELL if down 7% or more from entry
  - Partial TP  : alert at +15% (consider selling 50%)
  - Full TP     : forced SELL alert at +25% (consider exiting fully)
"""

import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Thread

import anthropic
import requests
import schedule
import yfinance as yf
from flask import Flask

# Hard trade rules (always override fundamentals)
STOP_LOSS_PCT = -7.0       # forced SELL at -7%
PARTIAL_PROFIT_PCT = 15.0  # alert only — suggest selling 50%
TAKE_PROFIT_PCT = 25.0     # forced SELL at +25%

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

PORTFOLIO_FILE = "portfolio.json"   # where positions are stored
LAST_CHAT_FILE = "last_chat.json"   # remembers chat for scheduled reports


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
def analyze_portfolio_with_claude(judged_positions):
    """Ask Claude for HOLD/SELL on each non-hard-rule position.

    `judged_positions` is a list of dicts that already had the hard
    stop-loss / take-profit rule applied. Items with `forced_signal` set
    are passed in as context but Claude is only asked to decide the
    'undecided' ones.
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

    prompt = f"""You are a senior equity analyst doing a daily portfolio review.

CURRENT POSITIONS (with live price, P/L, and fundamentals):
{portfolio_text}

POSITIONS ALREADY MARKED SELL BY HARD RULES (do not change these):
{forced_text}

For every position NOT already in the hard-rule list above, output a clear
HOLD or SELL signal with ONE sentence of reasoning. Use BOTH price action
AND fundamentals. Consider:
  - revenue growth, earnings growth, profit margins, ROE trend
  - valuation (forward PE, analyst target vs current price)
  - leverage (debt/equity)
  - analyst consensus (recommendationMean — lower is more bullish)
  - momentum reversal or clearly stronger opportunities elsewhere

Format the reply EXACTLY like this:

PORTFOLIO REVIEW:
TICKER — HOLD or SELL — reason
TICKER — HOLD or SELL — reason
...

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


# ---------------------------------------------------------------------------
# Claude analysis — monthly buy screen
# ---------------------------------------------------------------------------
def pick_monthly_buys_with_claude(candidates):
    """Ask Claude to pick the top 2 BUY opportunities from screened candidates."""
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

    prompt = f"""You are a senior equity analyst running a monthly buy screen.

The candidates below have ALREADY passed a strict fundamentals filter:
revenue growth > 15%, earnings growth > 10%, profit margin > 15%,
ROE > 15%, forward PE < 25, analyst recommendationMean < 2.0.

CANDIDATES:
{candidates_text}

Pick the TOP 2 BUY opportunities. For each, decide a conviction rating:
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
    alert_lines = []  # soft alerts (e.g. partial take profit)
    portfolio_dirty = False  # set True if alert state changes & needs saving

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

        # Per-position alert state (default to not-yet-fired)
        partial_alerted = pos.get("partial_alerted", False)
        full_alerted = pos.get("full_alerted", False)

        forced_signal = None
        forced_reason = None

        # --- Hard stop loss ---
        if pl_pct <= STOP_LOSS_PCT:
            forced_signal = "SELL"
            forced_reason = (
                f"hard stop loss hit (P/L {pl_pct}% ≤ {STOP_LOSS_PCT}%)"
            )

        # --- Full take profit at +25% (forced SELL + alert once) ---
        elif pl_pct >= TAKE_PROFIT_PCT:
            forced_signal = "SELL"
            forced_reason = (
                f"FULL SELL — take profit target hit "
                f"(P/L {pl_pct}% ≥ {TAKE_PROFIT_PCT}%)"
            )
            if not full_alerted:
                alert_msg = (
                    f"🚨 *{ticker}* FULL SELL — take profit target hit "
                    f"({pl_pct}% ≥ {TAKE_PROFIT_PCT}%). "
                    f"Consider exiting full position."
                )
                send_telegram(alert_msg, chat_id)
                pos["full_alerted"] = True
                # Treat partial as also resolved so we don't re-fire it
                pos["partial_alerted"] = True
                portfolio_dirty = True

        # --- Partial take profit at +15% (alert once, no forced SELL) ---
        elif pl_pct >= PARTIAL_PROFIT_PCT:
            if not partial_alerted:
                alert_msg = (
                    f"⚠️ *{ticker}* PARTIAL SELL — consider selling 50% "
                    f"of position to lock gains "
                    f"({pl_pct}% ≥ {PARTIAL_PROFIT_PCT}%)."
                )
                send_telegram(alert_msg, chat_id)
                pos["partial_alerted"] = True
                portfolio_dirty = True
            alert_lines.append(
                f"{ticker} — PARTIAL SELL alert — P/L {pl_pct}% "
                f"crossed +{PARTIAL_PROFIT_PCT}% (consider trimming 50%)"
            )

        judged_positions.append(
            {
                "ticker": ticker,
                "shares": pos["shares"],
                "entry_price": pos["entry_price"],
                "current_price": current,
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

    # Persist any newly-fired alert flags so we don't double-alert next time.
    if portfolio_dirty:
        save_portfolio(portfolio)

    if not judged_positions:
        send_telegram(
            "Could not fetch current prices for any of your holdings.",
            chat_id,
        )
        return

    # Ask Claude to judge the remaining (non-forced) positions
    needs_claude = [p for p in judged_positions if not p["forced_signal"]]
    if needs_claude:
        claude_output = analyze_portfolio_with_claude(judged_positions)
    else:
        claude_output = "PORTFOLIO REVIEW:\n(all positions decided by hard rules)"

    sections = ["*Daily Portfolio Report*", ""]
    if forced_lines:
        sections.append("*Hard-rule SELLs (override fundamentals):*")
        sections.extend(forced_lines)
        sections.append("")
    if alert_lines:
        sections.append("*Partial-take-profit alerts:*")
        sections.extend(alert_lines)
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

    picks = pick_monthly_buys_with_claude(candidates)
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

    portfolio = load_portfolio()
    portfolio[ticker] = {
        "shares": shares,
        "entry_price": price,
        "added": datetime.utcnow().isoformat(timespec="seconds"),
    }
    save_portfolio(portfolio)
    send_telegram(f"Added *{ticker}* — {shares} shares @ ${price}", chat_id)


def handle_sell(args, chat_id):
    # Expect: /sell AAPL
    try:
        ticker = args[0].upper()
    except IndexError:
        send_telegram("Usage: /sell TICKER", chat_id)
        return

    portfolio = load_portfolio()
    if ticker in portfolio:
        del portfolio[ticker]
        save_portfolio(portfolio)
        send_telegram(f"Removed *{ticker}* from portfolio.", chat_id)
    else:
        send_telegram(f"*{ticker}* is not in your portfolio.", chat_id)


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

    for ticker, pos in portfolio.items():
        shares = pos["shares"]
        entry = pos["entry_price"]
        current = get_current_price(ticker)

        # First line: ticker, shares, entry price
        lines.append(f"*{ticker}* — {shares} shares @ ${entry:.2f}")

        if current is None:
            lines.append("Current: _price unavailable_")
        else:
            pl_dollar = (current - entry) * shares
            pl_pct = ((current - entry) / entry) * 100
            emoji = "🟢" if pl_dollar >= 0 else "🔴"
            sign = "+" if pl_dollar >= 0 else "-"
            lines.append(
                f"Current: ${current:.2f} | "
                f"P&L: {sign}${abs(pl_dollar):.2f} "
                f"({sign}{abs(pl_pct):.1f}%) {emoji}"
            )
            total_cost += entry * shares
            total_value += current * shares

        lines.append("")  # blank line between positions

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
        "`/sell TICKER` — remove a position\n"
        "`/portfolio` — show holdings with live P&L\n"
        "`/analyze` — daily HOLD/SELL review on holdings\n"
        "`/monthly` — monthly S&P 500 buy screen (top 2 picks)\n\n"
        "_Hard rules: forced SELL at -7% stop loss or +25% take profit._\n"
        "_Soft alert: PARTIAL SELL (50%) at +15%._",
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
# Scheduled daily run at 09:00 UTC
# ---------------------------------------------------------------------------
def scheduled_run():
    chat_id = recall_chat()
    if chat_id is None:
        print("Skipping scheduled run — no chat has interacted with the bot yet.")
        return
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
