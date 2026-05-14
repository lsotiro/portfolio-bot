import json
import os
from datetime import datetime

WATCHLIST_FILE = "watchlist_candidates.json"


def _today():
    return datetime.utcnow().strftime("%Y-%m-%d")


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return {}
    with open(WATCHLIST_FILE) as f:
        return json.load(f)


def save_watchlist(watchlist):
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(watchlist, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, WATCHLIST_FILE)


def add_to_watchlist(ticker, score):
    """Add a ticker to the watchlist with today's date and initial score.

    If the ticker is already present, updates the score but leaves the
    existing consecutive_days counter intact.
    """
    watchlist = load_watchlist()
    if ticker not in watchlist:
        watchlist[ticker] = {
            "added_date": _today(),
            "score_at_add": score,
            "consecutive_days": 1 if score >= 65 else 0,
            "low_score_days": 0,
            "last_score": score,
            "last_updated": _today(),
        }
    else:
        watchlist[ticker]["last_score"] = score
        watchlist[ticker]["last_updated"] = _today()
    save_watchlist(watchlist)


def update_consecutive_days(ticker, score):
    """Update the consecutive BUY-day counter for a watchlist stock.

    Rules:
      score >= 65  → increment consecutive_days, reset low_score_days
      score <  50  → reset consecutive_days to 0, increment low_score_days;
                     remove the stock when low_score_days reaches 2
      50 ≤ score < 65 → hold position (neither increment nor reset either counter)

    Returns True if the ticker was removed from the watchlist, False otherwise.
    Silently ignores tickers not on the watchlist.
    """
    watchlist = load_watchlist()
    if ticker not in watchlist:
        return False

    entry = watchlist[ticker]
    entry["last_score"] = score
    entry["last_updated"] = _today()

    if score >= 65:
        entry["consecutive_days"] = entry.get("consecutive_days", 0) + 1
        entry["low_score_days"] = 0
    elif score < 50:
        entry["consecutive_days"] = 0
        entry["low_score_days"] = entry.get("low_score_days", 0) + 1
        if entry["low_score_days"] >= 2:
            del watchlist[ticker]
            save_watchlist(watchlist)
            return True
    # 50–64: HOLD zone — leave both counters unchanged

    save_watchlist(watchlist)
    return False


def get_buy_alerts():
    """Return watchlist stocks with consecutive_days >= 2.

    Returns a list of dicts sorted by consecutive_days descending, each with:
      ticker, consecutive_days, last_score, added_date, last_updated
    """
    watchlist = load_watchlist()
    alerts = [
        {
            "ticker": ticker,
            "consecutive_days": entry.get("consecutive_days", 0),
            "last_score": entry.get("last_score"),
            "added_date": entry.get("added_date"),
            "last_updated": entry.get("last_updated"),
        }
        for ticker, entry in watchlist.items()
        if entry.get("consecutive_days", 0) >= 2
    ]
    return sorted(alerts, key=lambda x: x["consecutive_days"], reverse=True)
