from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.dashboard import build_dashboard
from advisory.portfolio_engine import PORTFOLIO_TABLE
from advisory.sync_state import load_sync_states
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


ALERTS_TABLE = "advisory_live_watch_alerts"
DEFAULT_OUTPUT_DIR = Path("live_dashboard")
DEFAULT_OPERATOR_FEED_PATH = DEFAULT_OUTPUT_DIR / "operator_feed.json"


def _json_ready(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return value.to_dict(orient="records")
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    return value


def load_portfolio_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_portfolio_orders)")
    df = sql_to_df(
        f"""
        SELECT
            published_on,
            asof_date,
            setup_id,
            symbol,
            portfolio_status,
            portfolio_reason,
            approved_allocation_inr,
            requested_allocation_inr,
            priority_score
        FROM {PORTFOLIO_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE WHEN portfolio_status = 'approved' THEN 0 ELSE 1 END,
            approved_allocation_inr DESC NULLS LAST,
            priority_score DESC NULLS LAST,
            symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["published_on", "asof_date"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_alert_rows(limit: int = 100) -> pd.DataFrame:
    try:
        df = sql_to_df(
            f"""
            SELECT *
            FROM {ALERTS_TABLE}
            ORDER BY observed_at DESC, symbol
            LIMIT {int(limit)}
            """
        )
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return df
    for column in ["observed_at", "asof_date", "load_ts"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_operator_feed(*, output_dir: str | Path = DEFAULT_OUTPUT_DIR, limit: int = 50) -> list[dict[str, Any]]:
    feed_path = Path(output_dir) / "operator_feed.json"
    if not feed_path.exists():
        return []
    try:
        payload = json.loads(feed_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    return payload[-int(limit) :]


def build_live_dashboard_payload(*, asof_date: pd.Timestamp | None = None, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    dashboard_df = build_dashboard(asof_date=asof_date)
    portfolio_df = load_portfolio_rows(asof_date=asof_date)
    alerts_df = load_alert_rows()
    sync_state_df = load_sync_states()
    operator_feed = load_operator_feed(output_dir=output_dir)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "asof_date": None if asof_date is None else asof_date.isoformat(),
        "dashboard": _json_ready(dashboard_df),
        "portfolio": _json_ready(portfolio_df),
        "alerts": _json_ready(alerts_df),
        "operator_feed": _json_ready(operator_feed),
        "sync_state": _json_ready(sync_state_df),
    }


def render_html(payload: dict[str, Any]) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stockey Live Dashboard</title>
  <style>
    :root {{
      --bg: #f3efe4;
      --ink: #17201f;
      --muted: #65716e;
      --card: rgba(255,255,255,0.72);
      --line: rgba(23,32,31,0.12);
      --good: #0c7a43;
      --warn: #a05a00;
      --bad: #a32626;
      --accent: #124e66;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(18,78,102,0.14), transparent 32%),
        radial-gradient(circle at bottom right, rgba(12,122,67,0.12), transparent 26%),
        var(--bg);
    }}
    .wrap {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    h1, h2 {{ margin: 0 0 12px; }}
    .meta {{ color: var(--muted); margin-bottom: 20px; }}
    .grid {{
      display: grid;
      grid-template-columns: 2fr 1.2fr;
      gap: 18px;
    }}
    .card {{
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      backdrop-filter: blur(12px);
      box-shadow: 0 12px 40px rgba(23,32,31,0.08);
    }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ text-align: left; padding: 8px 6px; border-bottom: 1px solid var(--line); vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 12px;
      background: rgba(18,78,102,0.10);
      color: var(--accent);
    }}
    .approved {{ color: var(--good); font-weight: 700; }}
    .deferred {{ color: var(--warn); font-weight: 700; }}
    .alert-bad {{ color: var(--bad); font-weight: 700; }}
    .alert-good {{ color: var(--good); font-weight: 700; }}
    pre {{
      white-space: pre-wrap;
      word-break: break-word;
      background: rgba(23,32,31,0.04);
      padding: 12px;
      border-radius: 12px;
      overflow: auto;
      max-height: 360px;
    }}
    @media (max-width: 960px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Stockey Live Dashboard</h1>
    <div class="meta" id="meta">Loading…</div>
    <div class="grid">
      <section class="card">
        <h2>Setup Dashboard</h2>
        <div id="dashboard"></div>
      </section>
      <section class="card">
        <h2>Portfolio</h2>
        <div id="portfolio"></div>
      </section>
      <section class="card">
        <h2>Live Alerts</h2>
        <div id="alerts"></div>
      </section>
      <section class="card">
        <h2>Operator Feed</h2>
        <div id="operator_feed"></div>
      </section>
      <section class="card">
        <h2>Sync State</h2>
        <pre id="sync_state"></pre>
      </section>
    </div>
  </div>
  <script>
    async function loadDashboard() {{
      const res = await fetch("dashboard.json?_ts=" + Date.now());
      const data = await res.json();
      document.getElementById("meta").textContent =
        "Generated at: " + (data.generated_at || "-") + " | Asof date: " + (data.asof_date || "latest");

      const dashboard = Array.isArray(data.dashboard) ? data.dashboard : [];
      document.getElementById("dashboard").innerHTML = renderTable(dashboard, [
        ["setup_id", "Setup"],
        ["regime_name", "Regime"],
        ["overlay_name", "Overlay"],
        ["candidate_count", "Cand"],
        ["watchlist_count", "Watch"],
        ["allocation_count", "Alloc"],
        ["portfolio_count", "Port"],
        ["avg_setup_score", "Score"]
      ]);

      const portfolio = Array.isArray(data.portfolio) ? data.portfolio : [];
      document.getElementById("portfolio").innerHTML = renderTable(portfolio, [
        ["symbol", "Symbol"],
        ["setup_id", "Setup"],
        ["portfolio_status", "Status"],
        ["approved_allocation_inr", "Approved"],
        ["portfolio_reason", "Reason"]
      ], (key, value) => {{
        if (key === "portfolio_status") {{
          const cls = String(value || "").toLowerCase() === "approved" ? "approved" : "deferred";
          return `<span class="${{cls}}">${{value ?? "-"}}</span>`;
        }}
        return value ?? "-";
      }});

      const alerts = Array.isArray(data.alerts) ? data.alerts : [];
      document.getElementById("alerts").innerHTML = renderTable(alerts, [
        ["observed_at", "Observed"],
        ["symbol", "Symbol"],
        ["alert_type", "Type"],
        ["last_price", "Price"],
        ["alert_reason", "Reason"]
      ], (key, value, row) => {{
        if (key === "alert_type") {{
          const cls = String(value || "").includes("INVALIDATION") ? "alert-bad" : "alert-good";
          return `<span class="${{cls}}">${{value ?? "-"}}</span>`;
        }}
        return value ?? "-";
      }});

      const operatorFeed = Array.isArray(data.operator_feed) ? data.operator_feed : [];
      document.getElementById("operator_feed").innerHTML = renderTable(operatorFeed.slice().reverse(), [
        ["received_at", "Received"],
        ["channel", "Channel"],
        ["message", "Message"]
      ]);

      document.getElementById("sync_state").textContent = JSON.stringify(data.sync_state || [], null, 2);
    }}

    function renderTable(rows, columns, formatter) {{
      if (!rows.length) return "<p>No rows.</p>";
      const thead = "<thead><tr>" + columns.map(col => `<th>${{col[1]}}</th>`).join("") + "</tr></thead>";
      const tbody = "<tbody>" + rows.map(row => {{
        return "<tr>" + columns.map(col => {{
          const raw = row[col[0]];
          const value = formatter ? formatter(col[0], raw, row) : (raw ?? "-");
          return `<td>${{value}}</td>`;
        }}).join("") + "</tr>";
      }}).join("") + "</tbody>";
      return `<table>${{thead}}${{tbody}}</table>`;
    }}

    loadDashboard();
    setInterval(loadDashboard, 30000);
  </script>
</body>
</html>"""


def write_live_dashboard(*, output_dir: str | Path = DEFAULT_OUTPUT_DIR, asof_date: pd.Timestamp | None = None) -> dict[str, Any]:
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    payload = build_live_dashboard_payload(asof_date=asof_date, output_dir=output_path)
    (output_path / "dashboard.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    (output_path / "index.html").write_text(render_html(payload), encoding="utf-8")
    return {
        "status": "ok",
        "output_dir": str(output_path),
        "json_path": str(output_path / "dashboard.json"),
        "html_path": str(output_path / "index.html"),
        "dashboard_rows": len(payload.get("dashboard") or []),
        "portfolio_rows": len(payload.get("portfolio") or []),
        "alert_rows": len(payload.get("alerts") or []),
        "operator_feed_rows": len(payload.get("operator_feed") or []),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Write a simple auto-refresh live advisory dashboard as static HTML and JSON.")
    parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if args.date else None
    result = write_live_dashboard(output_dir=args.output_dir, asof_date=asof_date)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
