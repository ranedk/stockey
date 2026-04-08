from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON_BIN = Path(sys.executable)


def _emit(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _run_streaming_command(args: list[str], *, label: str) -> int:
    started_at = time.monotonic()
    _emit(f"[all_full_advisory] {label} start")
    proc = subprocess.run(args, cwd=REPO_ROOT)
    elapsed = time.monotonic() - started_at
    if proc.returncode == 0:
        _emit(f"[all_full_advisory] {label} done elapsed={elapsed:.2f}s")
    else:
        _emit(f"[all_full_advisory] {label} failed elapsed={elapsed:.2f}s returncode={proc.returncode}")
    return int(proc.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run event-model training if ready, then the full advisory pipeline, then print the current portfolio summary."
    )
    parser.add_argument("--date", help="Optional as-of date in YYYY-MM-DD for both model prep and advisory run")
    parser.add_argument("--horizon-days", type=int, default=1, help="Forward-return horizon for event-model training")
    parser.add_argument("--min-labeled-rows", type=int, default=20, help="Minimum labeled rows before training is allowed")
    parser.add_argument("--return-threshold", type=float, default=0.02, help="Directional-return threshold for event-model training")
    parser.add_argument("--artifact-dir", default=".cache/advisory_event_meta_model", help="Directory for trained event-model artifacts")
    parser.add_argument("--model-basename", default="event_meta_model", help="Model artifact basename")
    parser.add_argument("--skip-model-training", action="store_true", help="Skip all_model_training.sh and run only advisory plus portfolio summary")
    parser.add_argument("--skip-advisory", action="store_true", help="Skip all_advisory.sh and print only the current portfolio summary")
    parser.add_argument("--skip-downloads", action="store_true", help="Skip raw downloads inside the advisory pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Run training and advisory in dry-run mode")
    parser.add_argument("--portfolio-planned", action="store_true", help="Include already planned portfolio rows in the final printed portfolio summary")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.skip_model_training:
        training_cmd = [
            str(PYTHON_BIN),
            "-m",
            "advisory.model_training_runner",
            "--horizon-days",
            str(args.horizon_days),
            "--min-labeled-rows",
            str(args.min_labeled_rows),
            "--return-threshold",
            str(args.return_threshold),
            "--artifact-dir",
            args.artifact_dir,
            "--model-basename",
            args.model_basename,
        ]
        if args.date:
            training_cmd.extend(["--to-date", args.date])
        if args.dry_run:
            training_cmd.append("--prep-only")
        training_code = _run_streaming_command(training_cmd, label="model_training")
        if training_code != 0:
            return training_code

    if not args.skip_advisory:
        advisory_cmd = [
            str(PYTHON_BIN),
            "-m",
            "advisory.master_pipeline",
            "--event-model-artifact-dir",
            args.artifact_dir,
            "--format",
            "text",
        ]
        if args.date:
            advisory_cmd.extend(["--date", args.date])
        if args.skip_downloads:
            advisory_cmd.append("--skip-downloads")
        if args.dry_run:
            advisory_cmd.append("--dry-run")
        advisory_code = _run_streaming_command(advisory_cmd, label="advisory")
        if advisory_code != 0:
            return advisory_code

    portfolio_cmd = [
        str(PYTHON_BIN),
        "-m",
        "advisory.portfolio_engine",
        "--format",
        "text",
    ]
    if args.date:
        portfolio_cmd.extend(["--date", args.date])
    if args.portfolio_planned or args.dry_run:
        portfolio_cmd.append("--include-planned")
    return _run_streaming_command(portfolio_cmd, label="portfolio_summary")


if __name__ == "__main__":
    raise SystemExit(main())
