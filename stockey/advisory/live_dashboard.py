from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd

from advisory.dashboard import build_dashboard
from advisory.portfolio_engine import PORTFOLIO_TABLE, derive_thesis_policy
from advisory.position_lifecycle import LIFECYCLE_TABLE
from advisory.setup_registry import load_setup_registry
from advisory.sync_state import load_sync_states
from utils.db import sql_to_df
from utils.sync import parse_datetime_arg


ALERTS_TABLE = "advisory_live_watch_alerts"
DEFAULT_OUTPUT_DIR = Path("live_dashboard")
DEFAULT_OPERATOR_FEED_PATH = DEFAULT_OUTPUT_DIR / "operator_feed.json"
DEFAULT_CRON_LOG_DIR = Path("logs/cron")


def _setup_metadata() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for setup in load_setup_registry():
        setup_id = str(setup.get("setup_id") or "").upper()
        if not setup_id:
            continue
        out[setup_id] = {
            "setup_family": setup.get("setup_family"),
            "holding_horizon_note": setup.get("holding_horizon_note"),
            "screeners": setup.get("screeners") or setup.get("screener_slugs") or ([] if not setup.get("screener_slug") else [setup.get("screener_slug")]),
        }
    return out


def _table_columns(table_name: str) -> set[str]:
    try:
        df = sql_to_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
            """,
            params=(table_name,),
        )
    except Exception:
        return set()
    if df.empty or "column_name" not in df.columns:
        return set()
    return {str(value) for value in df["column_name"].dropna().astype(str)}


def _json_ready(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.isoformat()
    if isinstance(value, pd.DataFrame):
        return [_json_ready(row) for row in value.to_dict(orient="records")]
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [_json_ready(v) for v in value]
    if pd.isna(value):
        return None
    return value


def _summarize_exit_rules(raw_value: Any) -> str | None:
    if raw_value is None or (isinstance(raw_value, float) and pd.isna(raw_value)):
        return None
    parsed = raw_value
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return raw_value
    if not isinstance(parsed, list):
        return str(parsed)
    codes = [str(item.get("code")) for item in parsed if isinstance(item, dict) and item.get("code")]
    return ", ".join(codes) if codes else None


def _build_bucket_summary(row: pd.Series) -> str | None:
    raw_bucket = row.get("thesis_bucket")
    if raw_bucket is None or pd.isna(raw_bucket):
        return None
    bucket = str(raw_bucket).strip().upper()
    if not bucket or bucket == "NAN":
        return None
    bucket_reason = row.get("bucket_reason")
    bucket_reason_text = None if bucket_reason is None or pd.isna(bucket_reason) else str(bucket_reason).strip()
    if bucket == "TARGET":
        review = row.get("target_review_date")
        review_text = None if review is None or pd.isna(review) else str(review)
        return f"{bucket}: {bucket_reason_text or '-'}" + (f" Review {review_text}." if review_text else "")
    if bucket == "TIME_HORIZON":
        horizon = row.get("expected_horizon_days")
        horizon_type = row.get("horizon_type")
        horizon_type_text = None if horizon_type is None or pd.isna(horizon_type) else str(horizon_type).strip()
        horizon_text = f"{int(horizon)}d" if horizon is not None and not pd.isna(horizon) else (horizon_type_text or "-")
        return f"{bucket}: {bucket_reason_text or '-'} Window {horizon_text}."
    dependency_reason = row.get("data_dependency_reason")
    dependency_text = None if dependency_reason is None or pd.isna(dependency_reason) else str(dependency_reason).strip()
    return f"{bucket}: {bucket_reason_text or dependency_text or '-'}"


def _active_exit_from_action(next_action: Any) -> str | None:
    action = str(next_action or "").strip().lower()
    if action == "exit_invalidation":
        return "INVALIDATION_HIT"
    if action == "exit_stop":
        return "STOP_HIT"
    if action == "review_stale":
        return "STALE_REVIEW"
    if action == "trim_winner":
        return "TRIM_WINNER"
    if action == "tighten_stop":
        return "TIGHTEN_STOP"
    return None


def load_portfolio_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    available = _table_columns(PORTFOLIO_TABLE)
    select_columns = [
        "published_on",
        "asof_date",
        "setup_id",
        "symbol",
        "portfolio_status",
        "portfolio_reason",
        "stop_price",
        "invalidation_price",
        "approved_allocation_inr",
        "requested_allocation_inr",
        "priority_score",
    ]
    for optional in ["thesis_bucket", "bucket_reason", "target_review_date", "expected_horizon_days", "exit_event_rules_json"]:
        if optional in available:
            select_columns.insert(4 if optional == "thesis_bucket" else len(select_columns), optional)
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
            {", ".join(select_columns)}
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
    for column in ["published_on", "asof_date", "target_review_date"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    setup_meta = _setup_metadata()
    for idx, row in df.iterrows():
        setup_id = str(row.get("setup_id") or "").upper()
        meta = setup_meta.get(setup_id, {})
        enriched = pd.Series({**meta, **row.to_dict()})
        derived = derive_thesis_policy(enriched)
        for key, value in derived.items():
            if key not in df.columns:
                df[key] = None
            current = df.at[idx, key]
            if current is None or (isinstance(current, float) and pd.isna(current)):
                df.at[idx, key] = value
    df["bucket_summary"] = df.apply(lambda row: _build_bucket_summary(row), axis=1)
    df["exit_policy_summary"] = df.get("exit_event_rules_json", pd.Series(dtype="object")).map(_summarize_exit_rules)
    return df


def load_watchlist_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 50) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_watchlist)")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            setup_id,
            setup_name,
            symbol,
            watch_status,
            current_state,
            candidate_state,
            watch_reason_detail,
            attractive_price_low,
            attractive_price_high,
            invalidation_price,
            news_overlay,
            source_screener_slug,
            state_updated_at,
            last_state_transition_hint
        FROM advisory_watchlist
        WHERE {' AND '.join(clauses)}
        ORDER BY state_updated_at DESC NULLS LAST, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["asof_date", "state_updated_at"]:
        if column in df.columns:
            df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    return df


def load_candidate_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 50) -> pd.DataFrame:
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_candidates)")
    df = sql_to_df(
        f"""
        SELECT
            asof_date,
            setup_id,
            setup_name,
            symbol,
            candidate_state,
            setup_score,
            technical_score,
            fundamental_score,
            event_score,
            attractive_price_low,
            attractive_price_high,
            invalidation_price,
            source_screener_slug,
            news_overlay
        FROM advisory_candidates
        WHERE {' AND '.join(clauses)}
        ORDER BY
            CASE
                WHEN candidate_state = 'PASS_NOW' THEN 0
                WHEN candidate_state LIKE 'WATCH%%' THEN 1
                WHEN candidate_state = 'ABSTAIN' THEN 2
                ELSE 3
            END,
            setup_score DESC NULLS LAST,
            symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    if "asof_date" in df.columns:
        df["asof_date"] = pd.to_datetime(df["asof_date"], utc=True, errors="coerce")
    return df


def _safe_frame_loader(loader, *args, **kwargs) -> pd.DataFrame:
    try:
        return loader(*args, **kwargs)
    except Exception as exc:
        print(f"[advisory.live_dashboard] section loader failed {loader.__name__}: {exc}", flush=True)
        return pd.DataFrame()


def _safe_list_loader(loader, *args, **kwargs) -> list[dict[str, Any]]:
    try:
        return loader(*args, **kwargs)
    except Exception as exc:
        print(f"[advisory.live_dashboard] section loader failed {loader.__name__}: {exc}", flush=True)
        return []


def load_lifecycle_rows(*, asof_date: pd.Timestamp | None = None, limit: int = 25) -> pd.DataFrame:
    available = _table_columns(LIFECYCLE_TABLE)
    select_columns = [
        "published_on",
        "asof_date",
        "setup_id",
        "symbol",
        "unique_id",
        "position_status",
        "next_action",
        "next_action_reason",
        "current_price",
        "entry_price",
        "pnl_pct",
        "approved_allocation_inr",
    ]
    for optional in ["thesis_bucket", "active_exit_condition", "exit_condition_status", "bucket_status_note"]:
        if optional in available:
            select_columns.insert(4 if optional == "thesis_bucket" else len(select_columns), optional)
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    else:
        clauses.append("asof_date = (SELECT MAX(asof_date) FROM advisory_position_lifecycle)")
    df = sql_to_df(
        f"""
        SELECT
            {", ".join(select_columns)}
        FROM advisory_position_lifecycle
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on DESC, symbol
        LIMIT {int(limit)}
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    for column in ["published_on", "asof_date"]:
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce")
    portfolio_fallback = load_portfolio_rows(asof_date=asof_date, limit=max(limit * 3, 100))
    if not portfolio_fallback.empty and "unique_id" not in portfolio_fallback.columns:
        clauses = ["1 = 1"]
        params: list[object] = []
        if asof_date is not None:
            clauses.append("asof_date = %s")
            params.append(asof_date)
        else:
            clauses.append(f"asof_date = (SELECT MAX(asof_date) FROM {PORTFOLIO_TABLE})")
        fallback_df = sql_to_df(
            f"""
            SELECT unique_id, setup_id, symbol, thesis_bucket, bucket_reason, target_review_date, expected_horizon_days, exit_event_rules_json
            FROM {PORTFOLIO_TABLE}
            WHERE {' AND '.join(clauses)}
            """,
            params=tuple(params) if params else None,
        )
        if not fallback_df.empty:
            portfolio_fallback = portfolio_fallback.merge(
                fallback_df,
                how="left",
                on=["setup_id", "symbol"],
                suffixes=("", "_fallback"),
            )
    if not portfolio_fallback.empty:
        fallback_cols = [
            "thesis_bucket",
            "bucket_reason",
            "target_review_date",
            "expected_horizon_days",
            "exit_event_rules_json",
            "bucket_summary",
            "exit_policy_summary",
        ]
        available_cols = [col for col in ["unique_id", "setup_id", "symbol", *fallback_cols] if col in portfolio_fallback.columns]
        fallback_map = portfolio_fallback[available_cols].drop_duplicates(subset=[col for col in ["unique_id", "setup_id", "symbol"] if col in available_cols], keep="first")
        join_keys = [col for col in ["unique_id", "setup_id", "symbol"] if col in df.columns and col in fallback_map.columns]
        if join_keys:
            df = df.merge(fallback_map, how="left", on=join_keys, suffixes=("", "_portfolio"))
            for column in fallback_cols:
                portfolio_col = f"{column}_portfolio"
                if portfolio_col in df.columns:
                    if column not in df.columns:
                        df[column] = None
                    df[column] = df[column].where(df[column].notna(), df[portfolio_col])
                    df = df.drop(columns=[portfolio_col])
    setup_meta = _setup_metadata()
    for idx, row in df.iterrows():
        if row.get("thesis_bucket") is None or pd.isna(row.get("thesis_bucket")):
            setup_id = str(row.get("setup_id") or "").upper()
            meta = setup_meta.get(setup_id, {})
            if meta:
                derived = derive_thesis_policy(pd.Series({**meta, **row.to_dict()}))
                for key, value in derived.items():
                    if key not in df.columns:
                        df[key] = None
                    current = df.at[idx, key]
                    if current is None or (isinstance(current, float) and pd.isna(current)):
                        df.at[idx, key] = value
    if "active_exit_condition" not in df.columns:
        df["active_exit_condition"] = None
    if "exit_condition_status" not in df.columns:
        df["exit_condition_status"] = None
    if "bucket_status_note" not in df.columns:
        df["bucket_status_note"] = None
    df["active_exit_condition"] = df.apply(
        lambda row: row.get("active_exit_condition")
        or ("ENTRY_PRICE_MISSING" if str(row.get("next_action") or "").strip().lower() == "review_manual" else _active_exit_from_action(row.get("next_action"))),
        axis=1,
    )
    df["exit_condition_status"] = df.apply(
        lambda row: row.get("exit_condition_status") or ("triggered" if row.get("active_exit_condition") else "active"),
        axis=1,
    )
    df["bucket_summary"] = df.apply(lambda row: _build_bucket_summary(row), axis=1)
    df["exit_policy_summary"] = df.get("exit_event_rules_json", pd.Series(dtype="object")).map(_summarize_exit_rules)
    return df


def load_recent_watch_events(*, limit: int = 25) -> pd.DataFrame:
    df = sql_to_df(
        f"""
        SELECT
            published_on,
            asof_date,
            setup_id,
            symbol,
            subject,
            parse_status,
            event_status,
            concise_summary_text
        FROM advisory_watch_events
        ORDER BY published_on DESC, symbol
        LIMIT {int(limit)}
        """
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


def load_runtime_processes(limit: int = 30) -> list[dict[str, Any]]:
    try:
        output = subprocess.check_output(
            [
                "ps",
                "-eo",
                "pid,etimes,args",
            ],
            text=True,
        )
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    for line in output.splitlines()[1:]:
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, etimes, args = parts
        if "stockey" not in args and "advisory." not in args and "data." not in args and "go-crond" not in args:
            continue
        rows.append(
            {
                "pid": int(pid),
                "elapsed_seconds": int(etimes),
                "command": args,
            }
        )
    rows.sort(key=lambda item: item["elapsed_seconds"], reverse=True)
    return rows[: int(limit)]


def load_cron_status(*, log_dir: str | Path = DEFAULT_CRON_LOG_DIR, tail_lines: int = 8) -> list[dict[str, Any]]:
    log_path = Path(log_dir)
    if not log_path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for file_path in sorted(log_path.glob("*.log")):
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            content = []
        stat = file_path.stat()
        rows.append(
            {
                "log_file": file_path.name,
                "modified_at": pd.Timestamp(stat.st_mtime, unit="s", tz="UTC"),
                "size_bytes": int(stat.st_size),
                "tail": "\n".join(content[-int(tail_lines):]) if content else "",
            }
        )
    rows.sort(key=lambda item: item["modified_at"], reverse=True)
    return rows


def build_live_dashboard_payload(*, asof_date: pd.Timestamp | None = None, output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    dashboard_df = _safe_frame_loader(build_dashboard, asof_date=asof_date)
    portfolio_df = _safe_frame_loader(load_portfolio_rows, asof_date=asof_date)
    watchlist_df = _safe_frame_loader(load_watchlist_rows, asof_date=asof_date)
    candidates_df = _safe_frame_loader(load_candidate_rows, asof_date=asof_date)
    lifecycle_df = _safe_frame_loader(load_lifecycle_rows, asof_date=asof_date)
    watch_events_df = _safe_frame_loader(load_recent_watch_events)
    alerts_df = _safe_frame_loader(load_alert_rows)
    sync_state_df = _safe_frame_loader(load_sync_states)
    operator_feed = _safe_list_loader(load_operator_feed, output_dir=output_dir)
    runtime_processes = _safe_list_loader(load_runtime_processes)
    cron_status = _safe_list_loader(load_cron_status)
    return {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "asof_date": None if asof_date is None else asof_date.isoformat(),
        "dashboard": _json_ready(dashboard_df),
        "portfolio": _json_ready(portfolio_df),
        "watchlist": _json_ready(watchlist_df),
        "candidates": _json_ready(candidates_df),
        "lifecycle": _json_ready(lifecycle_df),
        "watch_events": _json_ready(watch_events_df),
        "alerts": _json_ready(alerts_df),
        "operator_feed": _json_ready(operator_feed),
        "sync_state": _json_ready(sync_state_df),
        "runtime_processes": _json_ready(runtime_processes),
        "cron_status": _json_ready(cron_status),
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
      grid-template-columns: 1.4fr 1.4fr;
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
    .wide {{ grid-column: 1 / -1; }}
    @media (max-width: 960px) {{
      .grid {{ grid-template-columns: 1fr; }}
      .wide {{ grid-column: auto; }}
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
        <h2>Watchlist</h2>
        <div id="watchlist"></div>
      </section>
      <section class="card">
        <h2>Candidates</h2>
        <div id="candidates"></div>
      </section>
      <section class="card">
        <h2>Lifecycle</h2>
        <div id="lifecycle"></div>
      </section>
      <section class="card">
        <h2>Live Alerts</h2>
        <div id="alerts"></div>
      </section>
      <section class="card">
        <h2>Recent Watch Events</h2>
        <div id="watch_events"></div>
      </section>
      <section class="card">
        <h2>Operator Feed</h2>
        <div id="operator_feed"></div>
      </section>
      <section class="card">
        <h2>Running Processes</h2>
        <div id="runtime_processes"></div>
      </section>
      <section class="card wide">
        <h2>Cron Logs</h2>
        <div id="cron_status"></div>
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
        ["bucket_summary", "Bucket Plan"],
        ["exit_policy_summary", "Exit Policy"],
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

      const watchlist = Array.isArray(data.watchlist) ? data.watchlist : [];
      document.getElementById("watchlist").innerHTML = renderTable(watchlist, [
        ["symbol", "Symbol"],
        ["setup_id", "Setup"],
        ["watch_status", "Watch"],
        ["current_state", "State"],
        ["attractive_price_low", "Buy Low"],
        ["attractive_price_high", "Buy High"],
        ["invalidation_price", "Invalid"],
        ["last_state_transition_hint", "Transition"]
      ]);

      const candidates = Array.isArray(data.candidates) ? data.candidates : [];
      document.getElementById("candidates").innerHTML = renderTable(candidates, [
        ["symbol", "Symbol"],
        ["setup_id", "Setup"],
        ["candidate_state", "State"],
        ["setup_score", "Score"],
        ["attractive_price_low", "Buy Low"],
        ["attractive_price_high", "Buy High"],
        ["invalidation_price", "Invalid"],
        ["source_screener_slug", "Screener"]
      ]);

      const lifecycle = Array.isArray(data.lifecycle) ? data.lifecycle : [];
      document.getElementById("lifecycle").innerHTML = renderTable(lifecycle, [
        ["symbol", "Symbol"],
        ["setup_id", "Setup"],
        ["bucket_summary", "Bucket Plan"],
        ["exit_policy_summary", "Exit Policy"],
        ["position_status", "Status"],
        ["next_action", "Next"],
        ["active_exit_condition", "Exit"],
        ["exit_condition_status", "Exit State"],
        ["bucket_status_note", "Bucket State"],
        ["current_price", "Price"],
        ["entry_price", "Entry"],
        ["pnl_pct", "PnL%"],
        ["next_action_reason", "Reason"]
      ]);

      const operatorFeed = Array.isArray(data.operator_feed) ? data.operator_feed : [];
      document.getElementById("operator_feed").innerHTML = renderTable(operatorFeed.slice().reverse(), [
        ["received_at", "Received"],
        ["channel", "Channel"],
        ["message", "Message"]
      ]);

      const watchEvents = Array.isArray(data.watch_events) ? data.watch_events : [];
      document.getElementById("watch_events").innerHTML = renderTable(watchEvents, [
        ["published_on", "Published"],
        ["symbol", "Symbol"],
        ["event_status", "Status"],
        ["subject", "Subject"],
        ["concise_summary_text", "Summary"]
      ]);

      const runtimeProcesses = Array.isArray(data.runtime_processes) ? data.runtime_processes : [];
      document.getElementById("runtime_processes").innerHTML = renderTable(runtimeProcesses, [
        ["pid", "PID"],
        ["elapsed_seconds", "Elapsed"],
        ["command", "Command"]
      ]);

      const cronStatus = Array.isArray(data.cron_status) ? data.cron_status : [];
      document.getElementById("cron_status").innerHTML = renderTable(cronStatus, [
        ["log_file", "Log"],
        ["modified_at", "Modified"],
        ["size_bytes", "Bytes"],
        ["tail", "Tail"]
      ], (key, value) => {{
        if (key === "tail") {{
          return `<pre>${{String(value || "").replace(/[&<>]/g, s => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[s]))}}</pre>`;
        }}
        return value ?? "-";
      }});

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
    (output_path / "dashboard.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str, allow_nan=False),
        encoding="utf-8",
    )
    (output_path / "index.html").write_text(render_html(payload), encoding="utf-8")
    return {
        "status": "ok",
        "output_dir": str(output_path),
        "json_path": str(output_path / "dashboard.json"),
        "html_path": str(output_path / "index.html"),
        "dashboard_rows": len(payload.get("dashboard") or []),
        "portfolio_rows": len(payload.get("portfolio") or []),
        "watchlist_rows": len(payload.get("watchlist") or []),
        "candidate_rows": len(payload.get("candidates") or []),
        "lifecycle_rows": len(payload.get("lifecycle") or []),
        "alert_rows": len(payload.get("alerts") or []),
        "watch_event_rows": len(payload.get("watch_events") or []),
        "operator_feed_rows": len(payload.get("operator_feed") or []),
        "runtime_process_rows": len(payload.get("runtime_processes") or []),
        "cron_status_rows": len(payload.get("cron_status") or []),
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
