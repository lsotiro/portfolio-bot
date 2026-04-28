import yfinance as yf
import anthropic
import requests
import os
import json

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

def get_chat_id():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    res = requests.get(url).json()
    try:
        return res["result"][-1]["message"]["chat"]["id"]
    except:
        return None

def send_telegram(message, chat_id):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown"
    })

def get_sp500_tickers():
    import urllib.request
    url = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
    response = urllib.request.urlopen(url)
    lines = response.read().decode().split("\n")[1:]
    tickers = [line.split(",")[0] for line in lines if line.strip()]
    return tickers[:100]

def fetch_stock_data(tickers):
    print(f"Fetching data for {len(tickers)} stocks...")
    data = []
    stocks = yf.download(tickers, period="5d", interval="1d", group_by="ticker", progress=False)
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                ticker_data = stocks
            else:
                ticker_data = stocks[ticker]
            closes = ticker_data["Close"].dropna()
            if len(closes) >= 2:
                price = round(float(closes.iloc[-1]), 2)
                prev = round(float(closes.iloc[-2]), 2)
                change_pct = round(((price - prev) / prev) * 100, 2)
                volume = int(ticker_data["Volume"].dropna().iloc[-1])
                data.append({
                    "ticker": ticker,
                    "price": price,
                    "change_pct": change_pct,
                    "volume": volume
                })
        except:
            continue
    return data

def analyze_with_claude(stock_data):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    stocks_text = "\n".join([
        f"{s['ticker']}: price=${s['price']}, change={s['change_pct']}%, volume={s['volume']}"
        for s in stock_data
    ])
    prompt = f"""You are a senior equity analyst. Here is today's market data for S&P 500 stocks:

{stocks_text}

Based on price momentum, volume, and daily change:
1. Pick the TOP 5 BUY candidates - stocks showing strength and upward momentum
2. Pick the TOP 5 SELL/AVOID candidates - stocks showing weakness or risk

For each stock give:
- Ticker
- Signal: BUY or SELL
- One sentence reason

Format your response clearly with BUY section and SELL section. Be direct and concise."""

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def run():
    print("Starting portfolio bot...")
    chat_id = get_chat_id()
    if not chat_id:
        print("ERROR: Could not get Telegram chat ID. Make sure you sent a message to your bot first!")
        return
    print(f"Telegram chat ID found: {chat_id}")
    send_telegram("*Portfolio Bot starting analysis...*\nFetching S&P 500 data, this takes ~1 minute.", chat_id)
    tickers = get_sp500_tickers()
    print(f"Got {len(tickers)} tickers")
    stock_data = fetch_stock_data(tickers)
    print(f"Fetched data for {len(stock_data)} stocks")
    print("Sending to Claude for analysis...")
    analysis = analyze_with_claude(stock_data)
    print("Analysis done!")
    message = f"*Daily S&P 500 Analysis*\n\n{analysis}"
    send_telegram(message, chat_id)
    print("Sent to Telegram!")

run()
