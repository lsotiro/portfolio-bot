from main import score_momentum_bulk
from watchlist_tracker import (
    get_buy_alerts,
    load_watchlist,
    update_consecutive_days,
)


def run_daily_check():
    """Score every watchlist stock, update consecutive-day counters, and
    return any stocks that have hit 2+ consecutive BUY days.

    Pipeline:
      1. Load tickers from watchlist_candidates.json.
      2. Score all in parallel with score_momentum_bulk.
      3. Call update_consecutive_days for each ticker (removes dead ones).
      4. Fetch buy alerts (consecutive_days >= 2).
      5. Enrich each alert with the full momentum details for Telegram display.

    Returns a list of dicts, one per alert, sorted by consecutive_days desc:
      ticker           – stock symbol
      score            – today's momentum score (0–100)
      consecutive_days – how many days in a row above 65
      details          – full score_momentum details dict (returns, RS, RVOL, …)
    """
    watchlist = load_watchlist()
    if not watchlist:
        print("[daily-watchlist] watchlist is empty — nothing to score")
        return []

    tickers = list(watchlist.keys())
    print(f"[daily-watchlist] scoring {len(tickers)} watchlist tickers...")

    score_map = score_momentum_bulk(tickers)
    print(f"[daily-watchlist] scores returned for {len(score_map)} tickers")

    removed = []
    for ticker in tickers:
        result = score_map.get(ticker)
        if result is None:
            # No data — treat as a missed day; don't penalise the counter.
            print(f"[daily-watchlist] {ticker}: no score data, skipping update")
            continue
        score, _details = result
        was_removed = update_consecutive_days(ticker, score)
        if was_removed:
            removed.append(ticker)
            print(f"[daily-watchlist] {ticker}: removed (2 consecutive low-score days)")

    if removed:
        print(f"[daily-watchlist] removed {len(removed)} stale tickers: {removed}")

    raw_alerts = get_buy_alerts()

    enriched = []
    for alert in raw_alerts:
        ticker = alert["ticker"]
        result = score_map.get(ticker)
        score = result[0] if result else alert.get("last_score")
        details = result[1] if result else {}
        enriched.append({
            "ticker": ticker,
            "score": score,
            "consecutive_days": alert["consecutive_days"],
            "details": details,
        })

    print(
        f"[daily-watchlist] {len(enriched)} buy alert(s) "
        f"(consecutive_days >= 2)"
    )
    return enriched
