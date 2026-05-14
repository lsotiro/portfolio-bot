from main import score_momentum_bulk


def score_watchlist(tickers):
    """Score a list of tickers and return those with momentum score >= 65.

    Returns a list of (ticker, score, details) tuples sorted by score descending.
    """
    if not tickers:
        return []

    results = score_momentum_bulk(tickers)

    qualifying = [
        (ticker, score, details)
        for ticker, (score, details) in results.items()
        if score is not None and score >= 65
    ]

    return sorted(qualifying, key=lambda x: x[1], reverse=True)
