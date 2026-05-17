"""Momentum scoring engine — extracted from main.py to break the circular
import between main.py (which imports watchlist.py) and watchlist.py (which
needs score_momentum_bulk).  Both main.py and watchlist.py now import from
this module instead of from each other.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
import yfinance as yf

FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY")
NEWS_API_KEY = os.environ.get("NEWS_API_KEY")
NEWS_ENDPOINT = "https://newsapi.org/v2/everything"

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


def get_stock_news(ticker, days=3, page_size=10):
    """Fetch recent NewsAPI headlines for a ticker.

    Queries both the ticker symbol AND company name to maximise recall.
    Each article dict carries:
      title, description, source, publishedAt, url, sentiment

    Returns a list of dicts. Empty list on any error or missing key.
    """
    if not NEWS_API_KEY:
        return []
    # Use a broader query: ticker symbol plus common name variants.
    # e.g. "ORCL" alone rarely matches; "ORCL OR Oracle" is far more effective.
    _COMPANY_NAMES = {
        "ORCL": "Oracle", "MSFT": "Microsoft", "AAPL": "Apple",
        "GOOGL": "Alphabet", "GOOG": "Alphabet", "AMZN": "Amazon",
        "META": "Meta", "NVDA": "NVIDIA", "TSLA": "Tesla",
        "AMD": "AMD", "INTC": "Intel", "CRM": "Salesforce",
        "ADBE": "Adobe", "NOW": "ServiceNow", "SNOW": "Snowflake",
        "PLTR": "Palantir", "UBER": "Uber", "LYFT": "Lyft",
        "NFLX": "Netflix", "DIS": "Disney", "JPM": "JPMorgan",
        "GS": "Goldman", "BAC": "Bank of America", "V": "Visa",
        "MA": "Mastercard", "PYPL": "PayPal", "SQ": "Block",
    }
    company = _COMPANY_NAMES.get(ticker.upper())
    query = f'"{ticker}" OR "{company}"' if company else f'"{ticker}"'

    from_dt = datetime.utcnow() - timedelta(days=days)
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "from": from_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "apiKey": NEWS_API_KEY,
    }
    try:
        r = requests.get(NEWS_ENDPOINT, params=params, timeout=10)
        if r.status_code != 200:
            print(f"[newsapi {ticker}] HTTP {r.status_code}: {r.text[:120]}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"[newsapi {ticker}] request failed: {exc}")
        return []
    out = []
    for art in (data.get("articles") or [])[:page_size]:
        title = (art.get("title") or "").strip()
        desc = (art.get("description") or "").strip()
        out.append(
            {
                "title": title,
                "description": desc,
                "source": (art.get("source") or {}).get("name", ""),
                "publishedAt": art.get("publishedAt", "")[:10],
                "url": art.get("url", ""),
                "sentiment": headline_sentiment(title),
            }
        )
    print(
        f"[newsapi {ticker}] query={query!r}  days={days}  "
        f"returned={len(out)} articles"
    )
    return out


def get_finnhub_news(ticker, days=3):
    """Fetch company news from Finnhub (primary news source for scoring).

    Finnhub /company-news returns articles with `headline` and `summary`.
    We normalise them to {title, description, source, publishedAt} so that
    `_news_keyword_score` can process them identically to NewsAPI articles.

    Returns a list of normalised dicts. Empty list on any error / missing key.
    """
    if not FINNHUB_API_KEY:
        return []
    from_date = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    to_date = datetime.utcnow().strftime("%Y-%m-%d")
    url = (
        f"https://finnhub.io/api/v1/company-news"
        f"?symbol={ticker}&from={from_date}&to={to_date}"
        f"&token={FINNHUB_API_KEY}"
    )
    try:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            print(f"[finnhub-news {ticker}] HTTP {r.status_code}: {r.text[:120]}")
            return []
        data = r.json()
    except Exception as exc:
        print(f"[finnhub-news {ticker}] request failed: {exc}")
        return []
    out = []
    for art in (data or [])[:20]:          # cap at 20 — plenty for sentiment
        headline = (art.get("headline") or "").strip()
        summary  = (art.get("summary")  or "").strip()
        if not headline:
            continue
        out.append(
            {
                "title": headline,
                "description": summary,
                "source": art.get("source", ""),
                "publishedAt": str(art.get("datetime", ""))[:10],
                "url": art.get("url", ""),
                "sentiment": headline_sentiment(headline),
            }
        )
    print(
        f"[finnhub-news {ticker}] days={days}  raw={len(data)}  "
        f"normalised={len(out)}"
    )
    return out


# ---------------------------------------------------------------------------
# Momentum scoring (100-point) — primary signal engine
# ---------------------------------------------------------------------------
# Five components, all derived from price/volume/news/earnings data:
#   - Price momentum    (30 pts)  — 1w, 4w, 12w returns
#   - RS vs SPY         (20 pts)  — 4w outperformance vs the index
#   - Volume confirm    (15 pts)  — up-day vs down-day volume ratio
#   - News sentiment    (20 pts)  — keyword tally on last 10 headlines
#   - Earnings momentum (15 pts)  — last quarter beat + estimates rising
# Score thresholds → signal (see get_momentum_signal).
_POSITIVE_NEWS = (
    "beat", "surge", "growth", "record", "strong", "upgrade", "buy",
    "bullish", "profit", "raised", "exceeded", "outperform",
)
_NEGATIVE_NEWS = (
    "miss", "drop", "fall", "weak", "downgrade", "sell", "bearish",
    "loss", "cut", "below", "concern", "risk", "lawsuit",
)


def _news_keyword_score(articles):
    """Return (news_score:int, label:str) per the spec's keyword heuristic.

    Label contract:
      "No relevant news"          — no articles fetched at all
      "X positive, Y negative"    — articles were fetched; X/Y are keyword counts
                                    (both may be 0 if no signal words matched)
    This distinction lets the display layer show accurate counts vs a true
    data-absent state.
    """
    if not articles:
        return 10, "No relevant news"
    pos = neg = 0
    for a in articles:
        text = (
            ((a.get("title") or "") + " " + (a.get("description") or ""))
            .lower()
        )
        if any(w in text for w in _POSITIVE_NEWS):
            pos += 1
        if any(w in text for w in _NEGATIVE_NEWS):
            neg += 1
    total = pos + neg
    if total == 0:
        # Articles were fetched but none matched signal keywords — neutral,
        # not "no news". Show the count so the display is honest.
        return 10, f"0 positive, 0 negative ({len(articles)} articles, no signal words)"
    ratio = pos / total
    if ratio >= 0.7:
        score = 20
    elif ratio >= 0.4:
        score = 10
    else:
        score = 0
    return score, f"{pos} positive, {neg} negative"


def _finnhub_earnings_beat(ticker):
    """Query Finnhub for the most recent earnings beat.

    Returns (beat: bool | None, actual: float | None, estimate: float | None,
             period: str | None).  Returns (None, None, None, None) when the
    key is absent, the request fails, or no quarterly data is available.

    Finnhub /stock/earnings returns quarters newest-first.
    """
    if not FINNHUB_API_KEY:
        return None, None, None, None
    try:
        url = (
            f"https://finnhub.io/api/v1/stock/earnings"
            f"?symbol={ticker}&token={FINNHUB_API_KEY}"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            print(
                f"[finnhub {ticker}] earnings HTTP {resp.status_code}: "
                f"{resp.text[:120]}"
            )
            return None, None, None, None
        data = resp.json()
        if not data:
            return None, None, None, None
        latest = data[0]
        actual = latest.get("actual")
        estimate = latest.get("estimate")
        period = latest.get("period", "")
        print(
            f"[finnhub {ticker}] earnings  period={period}  "
            f"actual={actual}  estimate={estimate}  "
            f"surprise={latest.get('surprisePercent')}%"
        )
        if actual is None or estimate is None:
            return None, actual, estimate, period
        beat = float(actual) > float(estimate)
        return beat, actual, estimate, period
    except Exception as exc:
        print(f"[finnhub {ticker}] earnings fetch failed: {exc}")
        return None, None, None, None


def _finnhub_price_target(ticker):
    """Return (target_mean: float | None) from Finnhub /stock/price-target."""
    if not FINNHUB_API_KEY:
        return None
    try:
        url = (
            f"https://finnhub.io/api/v1/stock/price-target"
            f"?symbol={ticker}&token={FINNHUB_API_KEY}"
        )
        resp = requests.get(url, timeout=8)
        if resp.status_code != 200:
            return None
        data = resp.json()
        target = data.get("targetMean") or data.get("targetMedian")
        print(
            f"[finnhub {ticker}] price-target  mean={data.get('targetMean')}  "
            f"median={data.get('targetMedian')}  high={data.get('targetHigh')}  "
            f"analysts={data.get('numberOfAnalysts')}"
        )
        return float(target) if target else None
    except Exception as exc:
        print(f"[finnhub {ticker}] price-target fetch failed: {exc}")
        return None


def score_momentum(ticker, hist=None, spy_hist=None, info=None,
                   news_articles=None):
    """Compute the 100-point momentum score for one ticker.

    Returns ``(score:int|None, details:dict)``. Returns ``(None, {})`` on
    a hard failure (insufficient history, etc). All upstream inputs are
    optional — when omitted the function fetches them itself, so the
    function is usable both stand-alone (/deep, /buy) and from a bulk
    pre-fetched context (handle_portfolio, /monthly).
    """
    score = 0
    details = {}
    try:
        if hist is None:
            try:
                hist = yf.Ticker(ticker).history(period="1y")
            except Exception as exc:
                print(f"[momentum {ticker}] history fetch failed: {exc}")
                hist = None
        if hist is None or len(hist) < 20:
            return None, {}

        if spy_hist is None:
            try:
                spy_hist = yf.Ticker("SPY").history(period="3mo")
            except Exception as exc:
                print(f"[momentum {ticker}] SPY fetch failed: {exc}")
                spy_hist = None

        current = float(hist["Close"].iloc[-1])
        week4_ago = float(
            hist["Close"].iloc[-20] if len(hist) >= 20 else hist["Close"].iloc[0]
        )
        week12_ago = float(hist["Close"].iloc[0])
        mo6_ago = float(
            hist["Close"].iloc[-126] if len(hist) >= 126 else hist["Close"].iloc[0]
        )
        mo12_ago = float(
            hist["Close"].iloc[-252] if len(hist) >= 252 else hist["Close"].iloc[0]
        )

        # ── Price momentum (25 pts) ─────────────────────────────────────
        ret_4w = (current - week4_ago) / week4_ago if week4_ago else 0
        ret_12w = (current - week12_ago) / week12_ago if week12_ago else 0
        ret_6m = (current - mo6_ago) / mo6_ago if mo6_ago else 0
        ret_12m = (current - mo12_ago) / mo12_ago if mo12_ago else 0
        if ret_4w > 0:
            score += 5
        if ret_12w > 0:
            score += 8
        if ret_6m > 0:
            score += 7
        if ret_12m > 0:
            score += 5
        details["ret_4w"] = round(ret_4w * 100, 1)
        details["ret_12w"] = round(ret_12w * 100, 1)
        details["ret_6m"] = round(ret_6m * 100, 1)
        details["ret_12m"] = round(ret_12m * 100, 1)

        # ── Relative strength vs SPY (20 pts) ───────────────────────────
        spy_ret_4w = 0.0
        if spy_hist is not None and not spy_hist.empty:
            spy_current = float(spy_hist["Close"].iloc[-1])
            spy_w4 = float(
                spy_hist["Close"].iloc[-20] if len(spy_hist) >= 20
                else spy_hist["Close"].iloc[0]
            )
            spy_ret_4w = (spy_current - spy_w4) / spy_w4 if spy_w4 else 0
        rs = ret_4w - spy_ret_4w
        if rs > 0.03:
            score += 20
        elif rs > 0:
            score += 10
        details["rs_vs_spy"] = round(rs * 100, 1)
        details["spy_ret_4w"] = round(spy_ret_4w * 100, 1)

        # ── Momentum consistency (15 pts) ────────────────────────────────
        last_126 = hist.tail(126)
        total_days = len(last_126)
        positive_days = int((last_126["Close"] > last_126["Open"]).sum())
        consistency_pct = (positive_days / total_days * 100) if total_days > 0 else 0.0
        if consistency_pct > 60:
            score += 15
        elif consistency_pct >= 55:
            score += 8
        elif consistency_pct >= 50:
            score += 4
        details["consistency_pct"] = round(consistency_pct, 1)

        # ── Volume confirmation via RVOL (15 pts) ───────────────────────
        # RVOL = today's volume ÷ 20-day average volume. Awards points
        # only on up days (close > open) — institutional buying is what
        # we're trying to detect, not panic selling. A high RVOL on a
        # down day is bearish, so it scores 0.
        try:
            last20 = hist.tail(20)
            avg_vol_20d = float(last20["Volume"].mean()) if len(last20) > 0 else 0.0
            today_vol = float(hist["Volume"].iloc[-1])
            today_open = float(hist["Open"].iloc[-1])
            today_close = float(hist["Close"].iloc[-1])
            up_day = today_close > today_open
            if avg_vol_20d > 0:
                rvol = today_vol / avg_vol_20d
            else:
                rvol = 0.0

            vol_score = 0
            if up_day:
                if rvol > 2.0:
                    vol_score = 15
                elif rvol >= 1.5:
                    vol_score = 10
                elif rvol >= 1.0:
                    vol_score = 7
            score += vol_score

            details["rvol"] = round(rvol, 2)
            details["rvol_up_day"] = bool(up_day)
            details["rvol_score"] = vol_score
            # Backward-compat: keep the old vol_ratio key populated so
            # any callers / tests that still reference it don't break —
            # we now stuff RVOL into it. The new _vol_label inspects the
            # rvol key first and only falls back to vol_ratio.
            details["vol_ratio"] = round(rvol, 2)
        except Exception as exc:
            print(f"[momentum {ticker}] RVOL calc failed: {exc}")
            details["rvol"] = None
            details["rvol_up_day"] = None
            details["rvol_score"] = 0
            details["vol_ratio"] = None

        # ── News sentiment (20 pts) ─────────────────────────────────────
        # Primary: Finnhub /company-news (ticker-specific, 20 articles, 3 days).
        # Fallback: NewsAPI (broader query with company name alias, 10 articles).
        # Both sources normalise to {title, description} so _news_keyword_score
        # works identically on either.
        if news_articles is None:
            try:
                news_articles = get_finnhub_news(ticker, days=3)
                if not news_articles:
                    print(
                        f"[momentum {ticker}] Finnhub news empty, "
                        "falling back to NewsAPI"
                    )
                    news_articles = get_stock_news(ticker, days=3, page_size=10)
            except Exception as exc:
                print(f"[momentum {ticker}] news fetch failed: {exc}")
                news_articles = []

        news_score, news_label = _news_keyword_score(news_articles or [])

        # Detailed log so every run is auditable
        _pos = sum(
            1 for a in (news_articles or [])
            if any(
                w in ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
                for w in _POSITIVE_NEWS
            )
        )
        _neg = sum(
            1 for a in (news_articles or [])
            if any(
                w in ((a.get("title") or "") + " " + (a.get("description") or "")).lower()
                for w in _NEGATIVE_NEWS
            )
        )
        print(
            f"[momentum {ticker}] news  articles={len(news_articles or [])}  "
            f"pos={_pos}  neg={_neg}  score={news_score}/20  label={news_label!r}"
        )
        if news_articles:
            for _a in (news_articles or [])[:5]:
                print(
                    f"  headline: {(_a.get('title') or '')[:72]}"
                )

        score += news_score
        details["news"] = news_label
        details["news_score"] = news_score
        details["_news_article_count"] = len(news_articles or [])

        # ── Earnings momentum (15 pts) ──────────────────────────────────
        # 12 pts: beat last quarter EPS estimate (Finnhub primary, yfinance fallback)
        # 10 pts: analyst price target ≥10% above current price
        # +3 pts bonus: both earnings_beat AND estimates_rising (max total 25)
        # Each sub-component is wrapped so one failure never zeroes the other.
        _ts = datetime.utcnow().strftime("%H:%M:%S UTC")
        print(f"[momentum {ticker}] earnings check at {_ts}")
        earnings_score = 0
        details["earnings_beat"] = None
        details["estimates_rising"] = None

        # ── 12 pts: EPS beat ────────────────────────────────────────────
        try:
            # PRIMARY: Finnhub (more reliable, real-time consensus data)
            fh_beat, fh_actual, fh_estimate, fh_period = _finnhub_earnings_beat(ticker)
            if fh_beat is not None:
                details["earnings_beat"] = fh_beat
                details["_earnings_source"] = "finnhub"
                details["_earnings_period"] = fh_period
                details["_earnings_actual"] = fh_actual
                details["_earnings_estimate"] = fh_estimate
                if fh_beat:
                    earnings_score += 12
            else:
                # FALLBACK: yfinance earnings_history
                # Uses epsActual / epsEstimate columns (oldest-first, iloc[-1] = newest).
                # More stable than earnings_dates whose column names have changed across
                # yfinance versions.
                print(f"[momentum {ticker}] Finnhub beat=None, falling back to yfinance")
                stock_obj = yf.Ticker(ticker)
                eh = stock_obj.earnings_history
                if (eh is not None and not eh.empty
                        and "epsActual" in eh.columns
                        and "epsEstimate" in eh.columns):
                    reported_only = eh.dropna(subset=["epsActual", "epsEstimate"])
                    if not reported_only.empty:
                        latest = reported_only.iloc[-1]   # oldest-first → last = newest
                        yf_actual = float(latest["epsActual"])
                        yf_estimate = float(latest["epsEstimate"])
                        yf_period = str(latest.name) if latest.name else ""
                        yf_beat = yf_actual > yf_estimate
                        details["earnings_beat"] = yf_beat
                        details["_earnings_source"] = "yfinance"
                        details["_earnings_period"] = yf_period
                        details["_earnings_actual"] = yf_actual
                        details["_earnings_estimate"] = yf_estimate
                        print(
                            f"[momentum {ticker}] yfinance fallback  "
                            f"period={yf_period}  actual={yf_actual}  "
                            f"estimate={yf_estimate}  beat={yf_beat}"
                        )
                        if yf_beat:
                            earnings_score += 12
        except Exception as exc:
            print(f"[momentum {ticker}] earnings_beat check failed: {exc}")

        # ── 10 pts: analyst target ≥10% upside ──────────────────────────
        try:
            # Ensure we have a usable info dict (may be None if called standalone)
            _info = info or {}
            target = None

            # PRIMARY: Finnhub price target (fresher consensus than yfinance)
            fh_target = _finnhub_price_target(ticker)
            if fh_target:
                target = fh_target
                details["_target_source"] = "finnhub"
            else:
                # FALLBACK: yfinance info dict — fetch it now if not pre-supplied
                if not _info:
                    try:
                        _yf_obj = yf.Ticker(ticker)
                        _info = _yf_obj.info or {}
                        print(
                            f"[momentum {ticker}] fetched yfinance info standalone  "
                            f"targetMeanPrice={_info.get('targetMeanPrice')}  "
                            f"currentPrice={_info.get('currentPrice')}"
                        )
                    except Exception as exc:
                        print(f"[momentum {ticker}] yfinance info fetch failed: {exc}")
                target = _info.get("targetMeanPrice") or 0
                details["_target_source"] = "yfinance"

            current_px = _info.get("currentPrice") or current
            upside_pct = ((target / current_px) - 1) * 100 if target and current_px else 0
            print(
                f"[momentum {ticker}] price-target  target={target}  "
                f"current={current_px:.2f}  upside={upside_pct:.1f}%  "
                f"qualifies={bool(target and target > current_px * 1.10)}"
            )
            if target and target > current_px * 1.10:
                earnings_score += 10
                details["estimates_rising"] = True
            else:
                details["estimates_rising"] = False
            details["_target_price"] = round(target, 2) if target else None
            details["_target_upside_pct"] = round(upside_pct, 1)
        except Exception as exc:
            print(f"[momentum {ticker}] price-target check failed: {exc}")
            details["estimates_rising"] = False

        if details.get("earnings_beat") and details.get("estimates_rising"):
            earnings_score += 3
        earnings_score = min(earnings_score, 25)
        score += earnings_score
        details["earnings_score"] = earnings_score
        print(
            f"[momentum {ticker}] earnings_score={earnings_score}  "
            f"beat={details.get('earnings_beat')}  "
            f"target_upside={details.get('_target_upside_pct')}%  "
            f"estimates_rising={details.get('estimates_rising')}"
        )

    except Exception as exc:
        print(f"[momentum {ticker}] hard failure: {exc}")
        return None, {}

    return score, details


def score_momentum_bulk(tickers, fundamentals=None, news_map=None):
    """Parallel momentum scoring for a list of tickers.

    Reuses pre-fetched ``fundamentals`` (one info dict per ticker) and
    ``news_map`` ({ticker: [articles]}) when supplied. SPY history is
    fetched once and shared across all worker threads — saves N round-trips.
    Returns ``{ticker: (score, details)}`` (excluding tickers that failed).
    """
    if not tickers:
        return {}
    try:
        spy_hist = yf.Ticker("SPY").history(period="3mo")
    except Exception as exc:
        print(f"[momentum-bulk] SPY history failed: {exc}")
        spy_hist = None

    out = {}

    def _one(t):
        info = (fundamentals or {}).get(t)
        news = (news_map or {}).get(t)
        try:
            hist = yf.Ticker(t).history(period="3mo")
        except Exception as exc:
            print(f"[momentum-bulk {t}] history fetch failed: {exc}")
            hist = None
        return t, score_momentum(
            t, hist=hist, spy_hist=spy_hist, info=info, news_articles=news,
        )

    with ThreadPoolExecutor(max_workers=8) as ex:
        for t, res in ex.map(_one, tickers):
            if res is not None:
                out[t] = res
    return out
