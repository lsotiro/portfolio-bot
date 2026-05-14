import re
import urllib.request

from watchlist import score_watchlist
from watchlist_tracker import add_to_watchlist

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


def run_weekly_scan():
    """Scan S&P 500 + Nasdaq 100, score all tickers, add qualifiers to watchlist.

    Returns a summary dict:
      sp500_count      – tickers fetched from S&P 500 list
      ndq100_count     – tickers fetched from Nasdaq 100 list
      universe_count   – deduplicated combined universe
      scored_count     – tickers for which a momentum score was computed
      added_count      – stocks added to the watchlist (score >= 65)
      added_tickers    – list of (ticker, score) for the new additions
    """
    print("[weekly-scan] fetching S&P 500 tickers...")
    try:
        sp500 = _fetch_csv_tickers(_SP500_CSV_URL)
        print(f"[weekly-scan] S&P 500: {len(sp500)} tickers")
    except Exception as exc:
        print(f"[weekly-scan] S&P 500 fetch failed: {exc}")
        sp500 = []

    print("[weekly-scan] fetching Nasdaq 100 tickers...")
    try:
        ndq100 = _fetch_csv_tickers(_NDQ100_CSV_URL)
        print(f"[weekly-scan] Nasdaq 100: {len(ndq100)} tickers")
    except Exception as exc:
        print(f"[weekly-scan] Nasdaq 100 fetch failed: {exc}")
        ndq100 = []

    # Deduplicate while preserving S&P 500 ordering first.
    seen = set(sp500)
    universe = sp500 + [t for t in ndq100 if t not in seen]
    print(f"[weekly-scan] combined universe: {len(universe)} tickers")

    print("[weekly-scan] scoring tickers (this takes several minutes)...")
    qualifying = score_watchlist(universe)
    print(f"[weekly-scan] {len(qualifying)} tickers scored >= 65")

    added = []
    for ticker, score, _details in qualifying:
        try:
            add_to_watchlist(ticker, score)
            added.append((ticker, score))
        except Exception as exc:
            print(f"[weekly-scan] failed to add {ticker} to watchlist: {exc}")

    summary = {
        "sp500_count": len(sp500),
        "ndq100_count": len(ndq100),
        "universe_count": len(universe),
        "scored_count": len(qualifying),
        "added_count": len(added),
        "added_tickers": added,
    }
    print(
        f"[weekly-scan] done — scanned {len(universe)}, "
        f"scored {len(qualifying)} >= 65, added {len(added)} to watchlist"
    )
    return summary
