"""Web dashboard for the Portfolio Bot.

Runs on port 8080 (or the next free port if 8080 is taken by another
artifact) in its own daemon thread alongside the existing keep-alive
server (port 5000). Reads portfolio.json and recommendations_log.json,
refreshes live prices via yfinance on every page load, and renders a
dark, mobile-friendly summary.

All heavy lifting reuses helpers from main.py — imports are LAZY
(inside the route handler) so there is no circular-import risk at
module load time.
"""
import socket
import threading
import time
from datetime import datetime

from flask import Flask, render_template_string

dashboard_app = Flask(__name__)

# Server-side TTL cache so concurrent visitors / multiple open tabs
# don't hammer yfinance every 60 seconds. The whole rendered
# context dict is cached for CACHE_TTL_SECONDS; the lock collapses
# concurrent in-flight refreshes into one fetch.
CACHE_TTL_SECONDS = 30
_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "context": None}


TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="60" />
  <title>Portfolio Dashboard</title>
  <style>
    :root {
      --bg: #0d1117;
      --panel: #161b22;
      --panel-2: #1c2128;
      --border: #30363d;
      --text: #c9d1d9;
      --muted: #8b949e;
      --green: #3fb950;
      --green-bg: rgba(63, 185, 80, 0.10);
      --red: #f85149;
      --red-bg: rgba(248, 81, 73, 0.12);
      --yellow: #d29922;
      --yellow-bg: rgba(210, 153, 34, 0.10);
      --accent: #58a6ff;
    }
    * { box-sizing: border-box; }
    html, body {
      margin: 0; padding: 0;
      background: var(--bg); color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Oxygen, Ubuntu, sans-serif;
      font-size: 15px; line-height: 1.5;
    }
    .container { max-width: 1100px; margin: 0 auto; padding: 20px 16px 60px; }
    header { display: flex; align-items: baseline; justify-content: space-between;
             flex-wrap: wrap; gap: 8px; margin-bottom: 20px; }
    header h1 { margin: 0; font-size: 22px; font-weight: 600; }
    header .updated { color: var(--muted); font-size: 12px; }
    .grid {
      display: grid; gap: 14px;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-bottom: 22px;
    }
    .card {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px 16px;
    }
    .card .label { color: var(--muted); font-size: 12px;
                   text-transform: uppercase; letter-spacing: 0.04em; }
    .card .value { font-size: 22px; font-weight: 600; margin-top: 4px; }
    .card .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .green { color: var(--green); }
    .red { color: var(--red); }
    .yellow { color: var(--yellow); }
    .muted { color: var(--muted); }

    section { margin-bottom: 26px; }
    section h2 {
      font-size: 14px; font-weight: 600; text-transform: uppercase;
      letter-spacing: 0.06em; color: var(--muted);
      margin: 0 0 10px; padding-bottom: 6px;
      border-bottom: 1px solid var(--border);
    }

    .health-bar {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 14px 16px;
    }
    .health-bar .top {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 12px; margin-bottom: 10px;
    }
    .health-bar .score { font-size: 26px; font-weight: 700; }
    .health-bar .rating { color: var(--muted); }
    .health-bar ul { list-style: none; padding: 0; margin: 0; }
    .health-bar li {
      display: flex; justify-content: space-between; gap: 12px;
      padding: 6px 0; border-top: 1px solid var(--border); font-size: 13px;
    }
    .health-bar li:first-child { border-top: 0; }
    .health-bar li .name { color: var(--text); }
    .health-bar li .detail { color: var(--muted); text-align: right; flex: 1;
                             margin-left: 12px; }

    table { width: 100%; border-collapse: collapse; overflow: hidden;
            border-radius: 8px; }
    th, td { padding: 10px 10px; text-align: right; font-size: 13px;
             white-space: nowrap; }
    th:first-child, td:first-child { text-align: left; }
    thead th { background: var(--panel-2); color: var(--muted);
               font-weight: 500; text-transform: uppercase;
               letter-spacing: 0.04em; font-size: 11px;
               border-bottom: 1px solid var(--border); }
    tbody tr { background: var(--panel); border-bottom: 1px solid var(--border); }
    tbody tr.row-green   { background: linear-gradient(to right, var(--green-bg), var(--panel) 30%); }
    tbody tr.row-yellow  { background: linear-gradient(to right, var(--yellow-bg), var(--panel) 30%); }
    tbody tr.row-red     { background: linear-gradient(to right, var(--red-bg), var(--panel) 30%); }
    tbody tr.row-muted   { color: var(--muted); }
    .ticker { font-weight: 600; color: var(--text); }
    .scroll-x { overflow-x: auto; -webkit-overflow-scrolling: touch;
                border: 1px solid var(--border); border-radius: 8px; }

    .recs, .stats { background: var(--panel); border: 1px solid var(--border);
                    border-radius: 8px; padding: 6px 0; }
    .recs .row, .stats .row {
      display: flex; justify-content: space-between; gap: 12px;
      padding: 10px 16px; border-top: 1px solid var(--border); font-size: 13px;
    }
    .recs .row:first-child, .stats .row:first-child { border-top: 0; }
    .recs .meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .empty { color: var(--muted); padding: 14px 16px; }

    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 999px;
      font-size: 11px; font-weight: 600; letter-spacing: 0.04em;
      text-transform: uppercase;
    }
    .badge.buy { background: var(--green-bg); color: var(--green); }
    .badge.sb  { background: var(--green); color: #0d1117; }
    .badge.sell { background: var(--red-bg); color: var(--red); }
    .badge.hold { background: var(--panel-2); color: var(--muted); }
    .badge.open { background: var(--panel-2); color: var(--accent); }
    .badge.correct { background: var(--green-bg); color: var(--green); }
    .badge.incorrect { background: var(--red-bg); color: var(--red); }
    .badge.stopped { background: var(--red-bg); color: var(--red); }

    @media (max-width: 600px) {
      .container { padding: 12px 10px 40px; }
      header h1 { font-size: 18px; }
      .card .value { font-size: 18px; }
      th, td { padding: 8px 8px; font-size: 12px; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>Portfolio Dashboard</h1>
      <div class="updated">Updated {{ updated }} · auto-refresh 60s</div>
    </header>

    {% if empty %}
      <div class="empty card">Your portfolio is empty. Use <code>/buy TICKER SHARES PRICE</code> in Telegram first.</div>
    {% else %}

      <div class="grid">
        <div class="card">
          <div class="label">Total Value</div>
          <div class="value">${{ "{:,.2f}".format(total_value) }}</div>
          <div class="sub">{{ n_positions }} position{{ '' if n_positions == 1 else 's' }}</div>
        </div>
        <div class="card">
          <div class="label">Total P&amp;L</div>
          <div class="value {{ 'green' if total_pl >= 0 else 'red' }}">
            {{ '+' if total_pl >= 0 else '' }}${{ "{:,.2f}".format(total_pl) }}
          </div>
          <div class="sub {{ 'green' if total_pl >= 0 else 'red' }}">
            {{ '+' if total_pl_pct >= 0 else '' }}{{ "%.2f"|format(total_pl_pct) }}%
          </div>
        </div>
        <div class="card">
          <div class="label">Cost Basis</div>
          <div class="value">${{ "{:,.2f}".format(total_cost) }}</div>
          <div class="sub">avg per position ${{ "{:,.0f}".format(total_cost / n_positions) }}</div>
        </div>
      </div>

      <section>
        <h2>Health Score</h2>
        {% if health %}
        <div class="health-bar">
          <div class="top">
            <div class="score">{{ health.score }}/10 {{ health.rating_emoji }}</div>
            <div class="rating">{{ health.rating_label }}</div>
          </div>
          <ul>
            {% for name, score, detail in health.components %}
            <li>
              <span class="name">{{ name }} — <strong>{{ "%.1f"|format(score) }}/10</strong></span>
              <span class="detail">{{ detail }}</span>
            </li>
            {% endfor %}
          </ul>
        </div>
        {% else %}
          <div class="empty card">No health score available.</div>
        {% endif %}
      </section>

      <section>
        <h2>Holdings</h2>
        <div class="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Ticker</th>
                <th>Shares</th>
                <th>Entry</th>
                <th>Current</th>
                <th>P&amp;L $</th>
                <th>P&amp;L %</th>
                <th>Target</th>
                <th>Upside</th>
              </tr>
            </thead>
            <tbody>
              {% for r in rows %}
              <tr class="{{ r.row_class }}">
                <td class="ticker">{{ r.ticker }}</td>
                <td>{{ r.shares }}</td>
                <td>${{ "%.2f"|format(r.entry) }}</td>
                {% if r.current is not none %}
                  <td>${{ "%.2f"|format(r.current) }}</td>
                  <td class="{{ 'green' if r.pl_dollar >= 0 else 'red' }}">
                    {{ '+' if r.pl_dollar >= 0 else '' }}${{ "{:,.2f}".format(r.pl_dollar) }}
                  </td>
                  <td class="{{ 'green' if r.pl_pct >= 0 else 'red' }}">
                    {{ '+' if r.pl_pct >= 0 else '' }}{{ "%.2f"|format(r.pl_pct) }}%
                  </td>
                {% else %}
                  <td class="muted">—</td>
                  <td class="muted">—</td>
                  <td class="muted">—</td>
                {% endif %}
                {% if r.target is not none %}
                  <td>${{ "%.2f"|format(r.target) }}</td>
                {% else %}
                  <td class="muted">—</td>
                {% endif %}
                {% if r.upside_pct is not none %}
                  <td class="{{ 'green' if r.upside_pct > 0 else 'red' }}">
                    {{ '+' if r.upside_pct >= 0 else '' }}{{ "%.1f"|format(r.upside_pct) }}%
                  </td>
                {% else %}
                  <td class="muted">—</td>
                {% endif %}
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>

      <section>
        <h2>Last 5 Recommendations</h2>
        <div class="recs">
          {% if recent_recs %}
            {% for r in recent_recs %}
            <div class="row">
              <div>
                <div>
                  <span class="ticker">{{ r.ticker }}</span>
                  <span class="badge {{ r.signal_class }}">{{ r.signal_display }}</span>
                  {% if r.status_class %}
                  <span class="badge {{ r.status_class }}">{{ r.status_display }}</span>
                  {% endif %}
                </div>
                <div class="meta">
                  {{ r.date }} · {{ r.source }}
                  {% if r.framework_score is not none %} · score {{ r.framework_score }}{% endif %}
                </div>
              </div>
              <div style="text-align:right;">
                {% if r.return_pct is not none %}
                <div class="{{ 'green' if r.return_pct >= 0 else 'red' }}">
                  {{ '+' if r.return_pct >= 0 else '' }}{{ "%.2f"|format(r.return_pct) }}%
                </div>
                {% else %}
                <div class="muted">open</div>
                {% endif %}
                {% if r.entry_price %}
                <div class="meta">entry ${{ "%.2f"|format(r.entry_price) }}</div>
                {% endif %}
              </div>
            </div>
            {% endfor %}
          {% else %}
            <div class="empty">No recommendations logged yet.</div>
          {% endif %}
        </div>
      </section>

      <section>
        <h2>Bot Track Record</h2>
        <div class="stats">
          <div class="row"><span>Total recommendations logged</span><span>{{ perf.total_logged }}</span></div>
          <div class="row"><span>Closed (8-week reviewed)</span><span>{{ perf.total }}</span></div>
          <div class="row"><span>Win rate vs SPY</span><span class="{{ 'green' if perf.win_rate >= 50 else 'red' }}">{{ "%.1f"|format(perf.win_rate) }}%</span></div>
          <div class="row"><span>Average return</span><span class="{{ 'green' if perf.avg_stock >= 0 else 'red' }}">{{ '+' if perf.avg_stock >= 0 else '' }}{{ "%.2f"|format(perf.avg_stock) }}%</span></div>
          <div class="row"><span>SPY benchmark over same windows</span><span>{{ '+' if perf.avg_sp >= 0 else '' }}{{ "%.2f"|format(perf.avg_sp) }}%</span></div>
          {% if perf.best %}
          <div class="row"><span>Best call</span><span class="green">{{ perf.best[0] }} {{ '+' if perf.best[1] >= 0 else '' }}{{ "%.2f"|format(perf.best[1]) }}%</span></div>
          {% endif %}
          {% if perf.worst %}
          <div class="row"><span>Worst call</span><span class="red">{{ perf.worst[0] }} {{ '+' if perf.worst[1] >= 0 else '' }}{{ "%.2f"|format(perf.worst[1]) }}%</span></div>
          {% endif %}
        </div>
      </section>

    {% endif %}
  </div>
</body>
</html>
"""


def _signal_class(signal):
    s = (signal or "").upper()
    if s == "STRONG BUY":
        return "sb"
    if s == "BUY":
        return "buy"
    if s == "SELL":
        return "sell"
    return "hold"


def _status_class(status, result):
    if result == "CORRECT":
        return "correct", "WIN"
    if result == "INCORRECT":
        return "incorrect", "LOSS"
    if result == "STOPPED":
        return "stopped", "STOPPED"
    if status == "open":
        return "open", "OPEN"
    return None, None


def _build_context():
    """Compute the full template context. Hits yfinance + reads JSON."""
    # Lazy imports — main.py registers itself in sys.modules under
    # both "__main__" and "main" at startup so this returns the SAME
    # module instance the bot is running in (no double-execution).
    from main import (
        STOP_LOSS_PCT,
        _final_result,
        _final_return,
        _safe_target,
        calculate_health_score,
        compute_performance_stats,
        fetch_fundamentals_bulk,
        get_current_price,
        load_portfolio,
    )

    updated = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    portfolio = load_portfolio()

    if not portfolio:
        return {"empty": True, "updated": updated}

    fundamentals = fetch_fundamentals_bulk(list(portfolio.keys()))
    stop_factor = 1.0 + (STOP_LOSS_PCT / 100.0)

    rows = []
    total_value = 0.0
    total_cost = 0.0

    for ticker, pos in portfolio.items():
        f = fundamentals.get(ticker) or {}
        current = f.get("currentPrice") or get_current_price(ticker)
        target = (
            _safe_target(pos.get("analyst_target"))
            or _safe_target(f.get("targetMeanPrice"))
        )
        shares = pos["shares"]
        entry = pos["entry_price"]
        stop_price = entry * stop_factor

        if current is None:
            rows.append({
                "ticker": ticker, "shares": shares, "entry": entry,
                "current": None, "pl_dollar": None, "pl_pct": None,
                "target": target, "upside_pct": None,
                "row_class": "row-muted",
            })
            continue

        pl_dollar = (current - entry) * shares
        pl_pct = ((current - entry) / entry) * 100
        upside_pct = (
            ((target - current) / current) * 100
            if target and current > 0 else None
        )

        if current >= entry:
            row_class = "row-green"
        elif current <= stop_price:
            row_class = "row-red"
        else:
            row_class = "row-yellow"

        rows.append({
            "ticker": ticker, "shares": shares, "entry": entry,
            "current": current, "pl_dollar": pl_dollar, "pl_pct": pl_pct,
            "target": target, "upside_pct": upside_pct,
            "row_class": row_class,
        })
        total_value += current * shares
        total_cost += entry * shares

    total_pl = total_value - total_cost
    total_pl_pct = (total_pl / total_cost * 100) if total_cost else 0.0

    health = calculate_health_score(portfolio, fundamentals)

    raw_perf = compute_performance_stats()
    perf = {
        "total_logged": len(raw_perf["all_recs"]),
        "total": raw_perf["total"],
        "win_rate": raw_perf["win_rate"],
        "avg_stock": raw_perf["avg_stock"],
        "avg_sp": raw_perf["avg_sp"],
        "best": raw_perf["best"],
        "worst": raw_perf["worst"],
    }

    recent_recs = []
    for r in reversed(raw_perf["all_recs"][-5:]):
        ret, _ = _final_return(r)
        result = _final_result(r)
        status_class, status_display = _status_class(r.get("status"), result)
        recent_recs.append({
            "ticker": r.get("ticker", "?"),
            "date": r.get("date", "?"),
            "source": r.get("source", "?"),
            "signal_class": _signal_class(r.get("signal")),
            "signal_display": (r.get("signal") or "?").upper(),
            "status_class": status_class,
            "status_display": status_display,
            "framework_score": r.get("framework_score"),
            "entry_price": r.get("entry_price"),
            "return_pct": ret,
        })

    return {
        "empty": False, "updated": updated,
        "total_value": total_value, "total_cost": total_cost,
        "total_pl": total_pl, "total_pl_pct": total_pl_pct,
        "n_positions": len(portfolio),
        "health": health, "rows": rows,
        "recent_recs": recent_recs, "perf": perf,
    }


@dashboard_app.route("/")
def index():
    """Serve the dashboard. Reuses cached context if it's <30s old."""
    now = time.time()
    with _cache_lock:
        if (
            _cache["context"] is not None
            and (now - _cache["ts"]) < CACHE_TTL_SECONDS
        ):
            ctx = _cache["context"]
        else:
            try:
                ctx = _build_context()
                _cache["context"] = ctx
                _cache["ts"] = now
            except Exception as exc:
                print(f"[dashboard] context build failed: {exc}")
                # Fall back to last-known-good if we have one, else surface the error.
                if _cache["context"] is not None:
                    ctx = _cache["context"]
                else:
                    return render_template_string(
                        TEMPLATE, empty=True,
                        updated=datetime.utcnow().strftime(
                            "%Y-%m-%d %H:%M:%S UTC"
                        ),
                    )
    return render_template_string(TEMPLATE, **ctx)


def _port_is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pick_dashboard_port(preferred=8082):
    """Return the first free port from the candidate list.

    8081 is skipped because in this Replit environment it is mapped to
    external port 80 (the path-routing proxy) and shouldn't be hijacked.
    """
    for candidate in (preferred, 8082, 8083, 8084):
        if _port_is_free(candidate):
            return candidate
    return preferred  # let Flask raise a clear error


def start_dashboard(port=None):
    """Run the dashboard server. Call from a daemon thread."""
    if port is None:
        port = pick_dashboard_port()
    print(f"[dashboard] starting on port {port}")
    # use_reloader=False is critical — Flask's reloader spawns a child
    # process which would double-start the rest of the bot.
    dashboard_app.run(host="0.0.0.0", port=port, use_reloader=False)
