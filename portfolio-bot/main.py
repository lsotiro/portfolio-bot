"""Portfolio Bot — a Telegram-controlled portfolio assistant.

Commands you can send the bot:
  /buy TICKER SHARES PRICE   add a position (e.g. /buy AAPL 10 189.50)
  /sell TICKER               remove a position from the portfolio
  /portfolio                 show current holdings
  /analyze                   manually trigger an analysis

Once a day at 09:00 UTC the bot also runs the analysis automatically and
sends the report to whichever chat last interacted with it.
"""

import json
import os
import time
import urllib.request
from datetime import datetime
from threading import Thread

import anthropic
import requests
import schedule
import yfinance as yf
from flask import Flask

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


def fetch_sp500_data():
    """Fetch 5-day price data for the top 100 S&P 500 tickers."""
    tickers = get_sp500_tickers()
    stocks = yf.download(
        tickers, period="5d", interval="1d", group_by="ticker", progress=False
    )
    data = []
    for ticker in tickers:
        try:
            t = stocks[ticker]
            closes = t["Close"].dropna()
            if len(closes) < 2:
                continue
            price = round(float(closes.iloc[-1]), 2)
            prev = round(float(closes.iloc[-2]), 2)
            change_pct = round(((price - prev) / prev) * 100, 2)
            volume = int(t["Volume"].dropna().iloc[-1])
            data.append(
                {
                    "ticker": ticker,
                    "price": price,
                    "change_pct": change_pct,
                    "volume": volume,
                }
            )
        except Exception:
            continue
    return data


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------
def analyze_with_claude(positions, sp500_data):
    """Ask Claude for HOLD/SELL on each position and up to 2 new BUYs."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    positions_text = "\n".join(
        f"{p['ticker']}: {p['shares']} shares, entry ${p['entry_price']}, "
        f"current ${p['current_price']}, P/L {p['pl_pct']}%"
        for p in positions
    )

    sp500_text = "\n".join(
        f"{s['ticker']}: price=${s['price']}, change={s['change_pct']}%, "
        f"volume={s['volume']}"
        for s in sp500_data
    )

    prompt = f"""You are a senior equity analyst reviewing a client's portfolio.

CURRENT PORTFOLIO:
{positions_text}

S&P 500 MARKET DATA (top 100 stocks):
{sp500_text}

Tasks:

1. For EACH position in the portfolio, give a clear HOLD or SELL signal with
   ONE sentence of reasoning. When deciding SELL, consider:
   - down more than 8% from entry (stop loss)
   - up more than 25% from entry (take profit)
   - momentum has reversed
   - clearly better opportunities elsewhere

2. Recommend AT MOST 2 new BUY opportunities from the S&P 500 list above —
   only if there is a really strong setup. If nothing stands out, write
   "No new buys today."

Format the reply exactly like this:

PORTFOLIO REVIEW:
TICKER — HOLD or SELL — reason

NEW BUY OPPORTUNITIES:
TICKER — BUY — reason
(or "No new buys today.")

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
# The main analysis routine — used by /analyze and the scheduler
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
        "*Running portfolio analysis...*\nFetching prices and market data.",
        chat_id,
    )

    # Build position rows with current price and P/L %
    positions = []
    for ticker, pos in portfolio.items():
        current = get_current_price(ticker)
        if current is None:
            continue
        pl_pct = round(
            ((current - pos["entry_price"]) / pos["entry_price"]) * 100, 2
        )
        positions.append(
            {
                "ticker": ticker,
                "shares": pos["shares"],
                "entry_price": pos["entry_price"],
                "current_price": current,
                "pl_pct": pl_pct,
            }
        )

    if not positions:
        send_telegram(
            "Could not fetch current prices for any of your holdings.",
            chat_id,
        )
        return

    sp500_data = fetch_sp500_data()
    analysis = analyze_with_claude(positions, sp500_data)
    send_telegram(f"*Daily Portfolio Report*\n\n{analysis}", chat_id)


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
        "`/portfolio` — show holdings\n"
        "`/analyze` — run analysis now",
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
# Boot everything
# ---------------------------------------------------------------------------
Thread(target=keep_alive, daemon=True).start()
Thread(target=poll_telegram, daemon=True).start()

print("Portfolio Bot started. Daily analysis scheduled at 09:00 UTC.")
while True:
    schedule.run_pending()
    time.sleep(30)
