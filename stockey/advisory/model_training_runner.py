from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from environs import Env


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = Path(sys.executable)
env = Env()
env.read_env()


DEFAULT_S3_UPLOAD_ENABLED = env.bool("EVENT_MODEL_ARTIFACT_UPLOAD_ENABLED", True)
DEFAULT_S3_PREFIX = env.str("EVENT_MODEL_ARTIFACT_S3_PREFIX", "models/advisory_event_meta_model")


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run_json_command(args: list[str]) -> dict[str, Any]:
    proc = subprocess.Popen(
        args,
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    stdout, _ = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(args)}")
    try:
        return json.loads(stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"expected JSON output from {' '.join(args)}") from exc


def _run_streaming_command(args: list[str]) -> int:
    proc = subprocess.run(args, cwd=REPO_ROOT)
    return int(proc.returncode)


def _horizon_ready(prep_summary: dict[str, Any], horizon_days: int) -> bool:
    coverage = ((prep_summary.get("label_coverage") or {}).get("coverage") or [])
    for item in coverage:
        if int(item.get("horizon_days") or 0) == int(horizon_days):
            return bool(item.get("train_ready"))
    return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare, train, and score the advisory event meta-model when label coverage is sufficient.")
    parser.add_argument("--horizon-days", type=int, default=1)
    parser.add_argument("--min-labeled-rows", type=int, default=20)
    parser.add_argument("--return-threshold", type=float, default=0.02)
    parser.add_argument("--artifact-dir", default=".cache/advisory_event_meta_model")
    parser.add_argument("--model-basename", default="event_meta_model")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--include-evaluated", action="store_true")
    parser.add_argument("--skip-training-universe", action="store_true")
    parser.add_argument("--skip-screener-normalization", action="store_true")
    parser.add_argument("--skip-event-backfill", action="store_true")
    parser.add_argument("--skip-price-refresh", action="store_true")
    parser.add_argument("--skip-score", action="store_true")
    parser.add_argument("--skip-s3-upload", action="store_true")
    parser.add_argument("--s3-prefix", default=DEFAULT_S3_PREFIX)
    parser.add_argument("--prep-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started_at = time.monotonic()

    prep_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_model_data_prep",
        "--format",
        "json",
        "--min-labeled-rows",
        str(args.min_labeled_rows),
        "--horizons",
        str(args.horizon_days),
    ]
    if args.from_date:
        prep_cmd.extend(["--from-date", args.from_date])
    if args.to_date:
        prep_cmd.extend(["--to-date", args.to_date])
    if args.rebuild:
        prep_cmd.append("--rebuild")
    if args.include_evaluated:
        prep_cmd.append("--include-evaluated")
    if args.skip_training_universe:
        prep_cmd.append("--skip-training-universe")
    if args.skip_screener_normalization:
        prep_cmd.append("--skip-screener-normalization")
    if args.skip_event_backfill:
        prep_cmd.append("--skip-event-backfill")
    if args.skip_price_refresh:
        prep_cmd.append("--skip-price-refresh")

    prep_started_at = time.monotonic()
    _emit("[all_model_training] prep start")
    prep_summary = _run_json_command(prep_cmd)
    _emit(f"[all_model_training] prep done elapsed={time.monotonic() - prep_started_at:.2f}s")
    print(json.dumps({"prep_summary": prep_summary}, indent=2, ensure_ascii=False), flush=True)

    if args.prep_only:
        _emit(f"[all_model_training] prep_only requested; skipping train/score total_elapsed={time.monotonic() - started_at:.2f}s")
        return 0

    if not _horizon_ready(prep_summary, args.horizon_days):
        _emit(
            f"[all_model_training] horizon={args.horizon_days}d not ready; skipping train/score",
        )
        return 0

    train_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_meta_model",
        "train",
        "--horizon-days",
        str(args.horizon_days),
        "--return-threshold",
        str(args.return_threshold),
        "--artifact-dir",
        args.artifact_dir,
        "--model-basename",
        args.model_basename,
    ]
    if args.to_date:
        train_cmd.extend(["--date", args.to_date])

    train_started_at = time.monotonic()
    _emit("[all_model_training] train start")
    train_code = _run_streaming_command(train_cmd)
    if train_code != 0:
        return train_code
    _emit(f"[all_model_training] train done elapsed={time.monotonic() - train_started_at:.2f}s")

    should_upload = bool(DEFAULT_S3_UPLOAD_ENABLED) and not bool(args.skip_s3_upload)
    if should_upload:
        upload_cmd = [
            str(PYTHON_BIN),
            "-m",
            "advisory.event_model_artifact_store",
            "--artifact-dir",
            args.artifact_dir,
            "--model-basename",
            args.model_basename,
            "--s3-prefix",
            args.s3_prefix,
        ]
        upload_started_at = time.monotonic()
        _emit("[all_model_training] s3_upload start")
        upload_code = _run_streaming_command(upload_cmd)
        if upload_code != 0:
            return upload_code
        _emit(f"[all_model_training] s3_upload done elapsed={time.monotonic() - upload_started_at:.2f}s")
    else:
        _emit("[all_model_training] s3_upload skipped")

    if args.skip_score:
        _emit(f"[all_model_training] skip_score requested total_elapsed={time.monotonic() - started_at:.2f}s")
        return 0

    score_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.event_meta_model",
        "score",
        "--artifact-dir",
        args.artifact_dir,
        "--model-basename",
        args.model_basename,
    ]
    if args.to_date:
        score_cmd.extend(["--date", args.to_date])

    score_started_at = time.monotonic()
    _emit("[all_model_training] score start")
    score_code = _run_streaming_command(score_cmd)
    if score_code == 0:
        _emit(f"[all_model_training] score done elapsed={time.monotonic() - score_started_at:.2f}s")
        _emit(f"[all_model_training] complete total_elapsed={time.monotonic() - started_at:.2f}s")
    return score_code


if __name__ == "__main__":
    raise SystemExit(main())
