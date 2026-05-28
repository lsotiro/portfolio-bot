"""Weekly scanner — ticker-fetch utilities.

run_weekly_scan() has moved to main.py and now uses the same
_quality_scan() pipeline as /monthly (fundamental filter + momentum
scoring + disqualification rules) so both entry points share one
quality bar.
"""
import re
import urllib.request

_SP500_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)
_NDQ100_CSV_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "nasdaq-100/main/data/constituents.csv"
)

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _fetch_csv_tickers(url, column_hint=None):
    """Fetch tickers from a CSV at *url*.

    Reads the header row to locate the ticker column, trying *column_hint*
    first, then common names ("Symbol", "Ticker", "ticker", "symbol").
    Falls back to the first column if none match.  Returns a list of valid
    uppercase ticker strings (1–5 uppercase letters only).
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    response = urllib.request.urlopen(req, timeout=20)
    lines = response.read().decode().splitlines()
    if not lines:
        return []

    header = [h.strip().strip('"') for h in lines[0].split(",")]

    candidates = ([column_hint] if column_hint else []) + [
        "Symbol", "Ticker", "ticker", "symbol"
    ]
    col_idx = 0
    for name in candidates:
        if name in header:
            col_idx = header.index(name)
            break

    tickers = []
    for line in lines[1:]:
        if not line.strip():
            continue
        parts = line.split(",")
        if col_idx >= len(parts):
            continue
        raw = parts[col_idx].strip().strip('"')
        if _TICKER_RE.match(raw):
            tickers.append(raw)
    return tickers
