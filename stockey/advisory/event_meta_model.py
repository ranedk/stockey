from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pandas as pd
from sklearn.metrics import accuracy_score, precision_score, recall_score, roc_auc_score
from xgboost import XGBClassifier

from advisory.llm_event_evaluator import ensure_output_tables as ensure_event_evaluation_tables
from utils.db import db_session, sql_to_df, upsert_to_db
from utils.sync import parse_datetime_arg


EVENTS_TABLE = "advisory_event_evaluations"
SCORES_TABLE = "advisory_event_model_scores"
DEFAULT_ARTIFACT_DIR = Path(".cache/advisory_event_meta_model")
DEFAULT_MODEL_BASENAME = "event_meta_model"
DEFAULT_HORIZON_DAYS = 10
DEFAULT_RETURN_THRESHOLD = 0.02


def normalize_timestamp(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce").dt.normalize()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def table_exists(table_name: str) -> bool:
    df = sql_to_df(
        """
        SELECT 1 AS exists_flag
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = %s
        LIMIT 1
        """,
        params=(table_name,),
    )
    return not df.empty


def ensure_scores_table() -> None:
    with db_session() as (_, cur):
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {SCORES_TABLE} (
                published_on TIMESTAMPTZ NOT NULL,
                asof_date TIMESTAMPTZ,
                setup_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                unique_id TEXT NOT NULL,
                scored_at TIMESTAMPTZ,
                model_name TEXT,
                model_version TEXT,
                horizon_days INTEGER,
                event_meta_score DOUBLE PRECISION,
                event_meta_label INTEGER,
                feature_snapshot_json TEXT,
                model_meta_json TEXT,
                load_ts TIMESTAMPTZ,
                UNIQUE (published_on, setup_id, symbol, unique_id, model_name, model_version, horizon_days)
            )
            """
        )


def _artifact_paths(artifact_dir: Path, model_basename: str) -> tuple[Path, Path]:
    model_path = artifact_dir / f"{model_basename}.json"
    meta_path = artifact_dir / f"{model_basename}.meta.json"
    return model_path, meta_path


def model_artifact_exists(artifact_dir: Path | None = None, model_basename: str = DEFAULT_MODEL_BASENAME) -> bool:
    artifact_dir = artifact_dir or DEFAULT_ARTIFACT_DIR
    model_path, meta_path = _artifact_paths(Path(artifact_dir), model_basename)
    return model_path.exists() and meta_path.exists()


def load_event_rows(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
) -> pd.DataFrame:
    ensure_event_evaluation_tables()
    if not table_exists(EVENTS_TABLE):
        return pd.DataFrame()
    clauses = ["1 = 1"]
    params: list[object] = []
    if asof_date is not None:
        clauses.append("asof_date = %s")
        params.append(asof_date)
    if symbols:
        clauses.append("symbol = ANY(%s)")
        params.append([value.upper() for value in symbols])
    if setup_ids:
        clauses.append("setup_id = ANY(%s)")
        params.append([value.upper() for value in setup_ids])
    df = sql_to_df(
        f"""
        SELECT
            published_on,
            asof_date,
            setup_id,
            symbol,
            unique_id,
            event_source,
            sentiment,
            materiality,
            setup_effect,
            direction,
            surprise,
            novelty,
            contradiction,
            expected_decay_days,
            source_reliability,
            governance_risk,
            balance_sheet_risk,
            execution_risk,
            investable_now,
            verdict,
            event_class,
            state_transition_hint,
            score_impact,
            confidence
        FROM {EVENTS_TABLE}
        WHERE {' AND '.join(clauses)}
        ORDER BY published_on, setup_id, symbol, unique_id
        """,
        params=tuple(params) if params else None,
    )
    if df.empty:
        return df
    df["published_on"] = pd.to_datetime(df["published_on"], utc=True, errors="coerce")
    df["asof_date"] = normalize_timestamp(df["asof_date"])
    df["symbol"] = df["symbol"].astype("string").str.upper()
    return df


def load_price_history(symbols: list[str], start_date: pd.Timestamp, end_date: pd.Timestamp) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    df = sql_to_df(
        """
        SELECT ticker AS symbol, date, close
        FROM dhan_ohlcv_daily
        WHERE exchange = 'NSE'
          AND asset_type = 'stock'
          AND ticker = ANY(%(symbols)s)
          AND date BETWEEN %(start_date)s AND %(end_date)s
        ORDER BY ticker, date
        """,
        params={"symbols": symbols, "start_date": start_date, "end_date": end_date},
    )
    if df.empty:
        return df
    df["symbol"] = df["symbol"].astype("string").str.upper()
    df["date"] = pd.to_datetime(df["date"], utc=True, errors="coerce").dt.normalize()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _direction_sign(row: pd.Series) -> int:
    setup_effect = str(row.get("setup_effect") or "").lower()
    direction = str(row.get("direction") or "").lower()
    sentiment = str(row.get("sentiment") or "").lower()
    if setup_effect == "strengthens" or direction == "positive" or sentiment == "positive":
        return 1
    if setup_effect in {"weakens", "contradicts"} or direction == "negative" or sentiment == "negative":
        return -1
    return 0


def build_labeled_event_dataset(
    *,
    asof_date: pd.Timestamp | None = None,
    symbols: list[str] | None = None,
    setup_ids: list[str] | None = None,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
) -> pd.DataFrame:
    events = load_event_rows(asof_date=asof_date, symbols=symbols, setup_ids=setup_ids)
    if events.empty:
        return events
    start_date = events["published_on"].min().normalize() + pd.Timedelta(days=1)
    end_date = (events["published_on"].max().normalize() + pd.Timedelta(days=horizon_days + 40))
    prices = load_price_history(events["symbol"].astype(str).dropna().unique().tolist(), start_date, end_date)
    if prices.empty:
        return pd.DataFrame()

    feature_rows: list[dict[str, Any]] = []
    for symbol, price_group in prices.groupby("symbol", sort=False):
        price_group = price_group.sort_values("date", kind="stable").reset_index(drop=True)
        for h in [1, 3, 5, 10, 20]:
            price_group[f"future_close_{h}"] = price_group["close"].shift(-h)
            price_group[f"forward_return_{h}"] = (price_group[f"future_close_{h}"] / price_group["close"]) - 1.0
        symbol_events = events[events["symbol"] == symbol]
        if symbol_events.empty:
            continue
        for _, event_row in symbol_events.iterrows():
            anchor_candidates = price_group[price_group["date"] > event_row["published_on"].normalize()]
            if anchor_candidates.empty:
                continue
            anchor = anchor_candidates.iloc[0]
            direction_sign = _direction_sign(event_row)
            directional_return = pd.to_numeric(anchor.get(f"forward_return_{horizon_days}"), errors="coerce")
            if direction_sign == 0 or pd.isna(directional_return):
                label = pd.NA
            else:
                directional_return = float(directional_return) * float(direction_sign)
                label = int(directional_return >= return_threshold)

            row = event_row.to_dict()
            row["anchor_date"] = anchor["date"]
            row["anchor_close"] = anchor["close"]
            row["direction_sign"] = direction_sign
            for h in [1, 3, 5, 10, 20]:
                value = pd.to_numeric(anchor.get(f"forward_return_{h}"), errors="coerce")
                row[f"forward_return_{h}d"] = None if pd.isna(value) else round(float(value), 6)
            row[f"directional_return_{horizon_days}d"] = None if pd.isna(directional_return) else round(float(directional_return), 6)
            row["target_label"] = label
            feature_rows.append(row)
    out = pd.DataFrame(feature_rows)
    if out.empty:
        return out
    return out


def _prepare_feature_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    frame = df.copy()
    frame["investable_now"] = frame["investable_now"].fillna(False).astype(int)
    numeric_cols = [
        "surprise",
        "novelty",
        "contradiction",
        "expected_decay_days",
        "score_impact",
        "confidence",
        "investable_now",
    ]
    for col in numeric_cols:
        frame[col] = pd.to_numeric(frame.get(col), errors="coerce").fillna(0.0)
    categorical_cols = [
        "event_source",
        "sentiment",
        "materiality",
        "setup_effect",
        "direction",
        "source_reliability",
        "governance_risk",
        "balance_sheet_risk",
        "execution_risk",
        "verdict",
        "event_class",
        "state_transition_hint",
        "setup_id",
    ]
    feature_df = frame[numeric_cols + categorical_cols].copy()
    feature_df = pd.get_dummies(feature_df, columns=categorical_cols, dummy_na=False)
    return feature_df, list(feature_df.columns)


def train_model(
    *,
    dataset: pd.DataFrame,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    model_basename: str = DEFAULT_MODEL_BASENAME,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    return_threshold: float = DEFAULT_RETURN_THRESHOLD,
) -> dict[str, Any]:
    if dataset.empty or "target_label" not in dataset.columns:
        raise ValueError("No labeled event rows are available for training yet.")
    labeled = dataset.dropna(subset=["target_label"]).copy()
    if len(labeled) < 20:
        raise ValueError("Not enough labeled event rows to train the event meta-model.")
    labeled = labeled.sort_values("published_on", kind="stable").reset_index(drop=True)
    features, feature_columns = _prepare_feature_frame(labeled)
    target = labeled["target_label"].astype(int)

    split_idx = max(1, int(len(labeled) * 0.8))
    if split_idx >= len(labeled):
        split_idx = len(labeled) - 1
    X_train = features.iloc[:split_idx]
    y_train = target.iloc[:split_idx]
    X_test = features.iloc[split_idx:]
    y_test = target.iloc[split_idx:]

    positives = int(y_train.sum())
    negatives = int(len(y_train) - positives)
    scale_pos_weight = 1.0 if positives <= 0 else max(1.0, negatives / max(positives, 1))

    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.9,
        colsample_bytree=0.9,
        objective="binary:logistic",
        eval_metric="logloss",
        random_state=42,
        scale_pos_weight=scale_pos_weight,
    )
    model.fit(X_train, y_train)
    test_proba = model.predict_proba(X_test)[:, 1]
    test_pred = (test_proba >= 0.5).astype(int)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_path, meta_path = _artifact_paths(artifact_dir, model_basename)
    model.save_model(model_path)

    metrics: dict[str, Any] = {
        "train_rows": int(len(X_train)),
        "test_rows": int(len(X_test)),
        "feature_count": int(len(feature_columns)),
        "horizon_days": int(horizon_days),
        "return_threshold": float(return_threshold),
        "positive_rate_train": round(float(y_train.mean()), 6),
        "positive_rate_test": round(float(y_test.mean()), 6) if len(y_test) else None,
        "accuracy": round(float(accuracy_score(y_test, test_pred)), 6) if len(y_test) else None,
        "precision": round(float(precision_score(y_test, test_pred, zero_division=0)), 6) if len(y_test) else None,
        "recall": round(float(recall_score(y_test, test_pred, zero_division=0)), 6) if len(y_test) else None,
    }
    if len(y_test) and len(set(y_test.tolist())) > 1:
        metrics["roc_auc"] = round(float(roc_auc_score(y_test, test_proba)), 6)
    metadata = {
        "model_name": "xgboost_event_meta_model",
        "model_version": f"{model_basename}_h{horizon_days}",
        "feature_columns": feature_columns,
        "horizon_days": int(horizon_days),
        "return_threshold": float(return_threshold),
        "metrics": metrics,
    }
    meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return metadata


def load_model_metadata(
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    model_basename: str = DEFAULT_MODEL_BASENAME,
) -> dict[str, Any]:
    _, meta_path = _artifact_paths(artifact_dir, model_basename)
    return json.loads(meta_path.read_text(encoding="utf-8"))


def score_events(
    *,
    dataset: pd.DataFrame,
    artifact_dir: Path = DEFAULT_ARTIFACT_DIR,
    model_basename: str = DEFAULT_MODEL_BASENAME,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if dataset.empty:
        return pd.DataFrame(), {"input_row_count": 0, "scored_row_count": 0}
    model_path, _ = _artifact_paths(artifact_dir, model_basename)
    metadata = load_model_metadata(artifact_dir, model_basename)
    model = XGBClassifier()
    model.load_model(model_path)

    features, _ = _prepare_feature_frame(dataset)
    for col in metadata["feature_columns"]:
        if col not in features.columns:
            features[col] = 0
    features = features[metadata["feature_columns"]]
    scores = model.predict_proba(features)[:, 1]

    out = dataset[["published_on", "asof_date", "setup_id", "symbol", "unique_id"]].copy()
    out["scored_at"] = pd.Timestamp.utcnow()
    out["model_name"] = metadata["model_name"]
    out["model_version"] = metadata["model_version"]
    out["horizon_days"] = int(metadata["horizon_days"])
    out["event_meta_score"] = [round(float(value), 6) for value in scores]
    out["event_meta_label"] = pd.to_numeric(dataset.get("target_label"), errors="coerce")
    feature_snapshots: list[str] = []
    for idx in range(len(features)):
        sparse = {col: float(features.iloc[idx][col]) for col in features.columns if float(features.iloc[idx][col]) != 0.0}
        feature_snapshots.append(json_dumps(sparse))
    out["feature_snapshot_json"] = feature_snapshots
    out["model_meta_json"] = json_dumps({"horizon_days": metadata["horizon_days"], "return_threshold": metadata["return_threshold"]})
    out["load_ts"] = pd.Timestamp.utcnow()
    meta = {
        "input_row_count": int(len(dataset)),
        "scored_row_count": int(len(out)),
        "model_name": metadata["model_name"],
        "model_version": metadata["model_version"],
        "horizon_days": int(metadata["horizon_days"]),
    }
    return out, meta


def persist_scores(df: pd.DataFrame) -> None:
    ensure_scores_table()
    if df.empty:
        return
    upsert_to_db(
        df,
        SCORES_TABLE,
        unique_keys=["published_on", "setup_id", "symbol", "unique_id", "model_name", "model_version", "horizon_days"],
        timescaledb_column="published_on",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train or score the advisory event meta-model.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    train_parser = subparsers.add_parser("train")
    train_parser.add_argument("--date", type=parse_datetime_arg, help="Optional max asof date in YYYY-MM-DD")
    train_parser.add_argument("--symbols", nargs="*")
    train_parser.add_argument("--setup", dest="setup_ids", nargs="*")
    train_parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    train_parser.add_argument("--return-threshold", type=float, default=DEFAULT_RETURN_THRESHOLD)
    train_parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    train_parser.add_argument("--model-basename", default=DEFAULT_MODEL_BASENAME)

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--date", type=parse_datetime_arg, help="Optional asof date in YYYY-MM-DD")
    score_parser.add_argument("--symbols", nargs="*")
    score_parser.add_argument("--setup", dest="setup_ids", nargs="*")
    score_parser.add_argument("--artifact-dir", default=str(DEFAULT_ARTIFACT_DIR))
    score_parser.add_argument("--model-basename", default=DEFAULT_MODEL_BASENAME)
    score_parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof_date = pd.Timestamp(args.date, tz="UTC") if getattr(args, "date", None) else None
    artifact_dir = Path(args.artifact_dir)
    try:
        if args.command == "train":
            dataset = build_labeled_event_dataset(
                asof_date=asof_date,
                symbols=args.symbols,
                setup_ids=args.setup_ids,
                horizon_days=int(args.horizon_days),
                return_threshold=float(args.return_threshold),
            )
            metadata = train_model(
                dataset=dataset,
                artifact_dir=artifact_dir,
                model_basename=args.model_basename,
                horizon_days=int(args.horizon_days),
                return_threshold=float(args.return_threshold),
            )
            print(json.dumps({"status": "ok", "row_count": int(len(dataset)), **metadata}, indent=2, ensure_ascii=False, default=str))
            return 0

        dataset = build_labeled_event_dataset(asof_date=asof_date, symbols=args.symbols, setup_ids=args.setup_ids)
        scores, meta = score_events(dataset=dataset, artifact_dir=artifact_dir, model_basename=args.model_basename)
        if not args.dry_run:
            persist_scores(scores)
        print(json.dumps({"status": "ok", "dry_run": bool(args.dry_run), **meta, "sample": scores.head(10).to_dict(orient="records")}, indent=2, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "command": args.command, "error": str(exc)}, indent=2, ensure_ascii=False, default=str))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
