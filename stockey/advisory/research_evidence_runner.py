from __future__ import annotations

import argparse
import json
import time
from typing import Any

from advisory import model_training_runner as runner


def _emit(message: str) -> None:
    runner._emit(message)


def _run_json_command(args: list[str]) -> dict[str, Any]:
    return runner._run_json_command(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run research-only evidence evaluators without event-model prep, training, scoring, "
            "portfolio mutation, config mutation, or broker execution."
        )
    )
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--cost-bps", type=float, default=25.0)
    parser.add_argument("--return-threshold", type=float, default=0.02)
    parser.add_argument("--signal-quality-horizons", nargs="*", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_HORIZONS)
    parser.add_argument("--skip-signal-quality", action="store_true")
    parser.add_argument("--run-signal-quality-window-runner", action="store_true")
    parser.add_argument("--signal-quality-window-days", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_WINDOW_DAYS)
    parser.add_argument("--signal-quality-step-days", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_STEP_DAYS)
    parser.add_argument("--signal-quality-windows", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_WINDOWS)
    parser.add_argument("--signal-quality-min-matured-rows", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_MIN_MATURED_ROWS)
    parser.add_argument("--signal-quality-min-stable-windows", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOWS)
    parser.add_argument("--signal-quality-min-stable-window-rate", type=float, default=runner.DEFAULT_SIGNAL_QUALITY_MIN_STABLE_WINDOW_RATE)
    parser.add_argument("--include-signal-quality-split-reports", action="store_true")
    parser.add_argument(
        "--signal-quality-split-report-min-matured-rows",
        type=int,
        default=runner.DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_MIN_MATURED_ROWS,
    )
    parser.add_argument("--signal-quality-split-report-top-n", type=int, default=runner.DEFAULT_SIGNAL_QUALITY_SPLIT_REPORT_TOP_N)
    parser.add_argument("--skip-context-overlay-signal-backfill", action="store_true")
    parser.add_argument("--context-overlay-signal-backfill-days", type=int, default=runner.DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_DAYS)
    parser.add_argument("--context-overlay-signal-backfill-step-days", type=int, default=runner.DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_STEP_DAYS)
    parser.add_argument("--context-overlay-signal-backfill-limit", type=int, default=runner.DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_LIMIT)
    parser.add_argument(
        "--context-overlay-signal-backfill-maturity-buffer-days",
        type=int,
        default=runner.DEFAULT_CONTEXT_OVERLAY_SIGNAL_BACKFILL_MATURITY_BUFFER_DAYS,
    )
    parser.add_argument("--skip-event-evidence-store", action="store_true")
    parser.add_argument("--skip-causal-event-memory", action="store_true")
    parser.add_argument("--skip-causal-event-memory-evaluator", action="store_true")
    parser.add_argument("--skip-causal-event-provenance", action="store_true")
    parser.add_argument("--causal-event-memory-min-matured-rows", type=int, default=runner.DEFAULT_CAUSAL_EVENT_MEMORY_MIN_MATURED_ROWS)
    parser.add_argument("--causal-event-memory-limit-per-source", type=int, default=runner.DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)
    parser.add_argument("--skip-negative-pressure-evaluator", action="store_true")
    parser.add_argument("--negative-pressure-min-matured-rows", type=int, default=runner.DEFAULT_NEGATIVE_PRESSURE_MIN_MATURED_ROWS)
    parser.add_argument("--skip-context-watch-evaluator", action="store_true")
    parser.add_argument("--context-watch-min-matured-rows", type=int, default=runner.DEFAULT_CONTEXT_WATCH_MIN_MATURED_ROWS)
    parser.add_argument("--skip-adversarial-review-evaluator", action="store_true")
    parser.add_argument("--adversarial-review-min-matured-rows", type=int, default=runner.DEFAULT_ADVERSARIAL_REVIEW_MIN_MATURED_ROWS)
    parser.add_argument("--skip-action-transition-evaluator", action="store_true")
    parser.add_argument("--action-transition-min-matured-rows", type=int, default=runner.DEFAULT_ACTION_TRANSITION_MIN_MATURED_ROWS)
    parser.add_argument("--skip-action-evidence-provenance", action="store_true")
    parser.add_argument("--action-evidence-provenance-limit", type=int, default=runner.DEFAULT_CAUSAL_EVENT_MEMORY_LIMIT_PER_SOURCE)
    parser.add_argument(
        "--allow-manual-review-rows",
        action="store_true",
        help="Allow signal-quality promotion review rows. Defaults off so this runner only refreshes evidence.",
    )
    parser.add_argument(
        "--generate-causal-event-memory-config-previews",
        action="store_true",
        help="Generate disabled causal-memory config-preview audit rows. Defaults off.",
    )
    parser.add_argument("--include-causal-event-memory-suppression-previews", action="store_true")
    parser.add_argument("--max-causal-event-memory-config-previews", type=int, default=5)
    return parser.parse_args(argv)


def run_research_evidence(args: argparse.Namespace) -> dict[str, Any]:
    started_at = time.monotonic()
    args.skip_signal_quality_promotion = not bool(getattr(args, "allow_manual_review_rows", False))

    signal_summary = None
    family_report = None
    split_queue = None
    split_evaluator = None
    if args.skip_signal_quality:
        _emit("[research_evidence] signal_quality skipped")
    else:
        signal_started_at = time.monotonic()
        _emit("[research_evidence] signal_quality start")
        signal_summary = _run_json_command(runner._build_signal_quality_command(args))
        _emit(
            "[research_evidence] "
            f"signal_quality done elapsed={time.monotonic() - signal_started_at:.2f}s "
            f"summary_rows={signal_summary.get('summary_rows')}"
        )

        family_started_at = time.monotonic()
        _emit("[research_evidence] signal_quality_family_report start")
        family_report = _run_json_command(runner._build_signal_quality_family_report_command(args))
        _emit(
            "[research_evidence] "
            f"signal_quality_family_report done elapsed={time.monotonic() - family_started_at:.2f}s "
            f"status={family_report.get('status')} candidate_helpful_count={family_report.get('candidate_helpful_count')}"
        )

        split_queue_started_at = time.monotonic()
        _emit("[research_evidence] signal_quality_split_queue start")
        split_queue = _run_json_command(runner._build_signal_quality_split_queue_command(args))
        queue = split_queue.get("research_queue") if isinstance(split_queue.get("research_queue"), dict) else {}
        _emit(
            "[research_evidence] "
            f"signal_quality_split_queue done elapsed={time.monotonic() - split_queue_started_at:.2f}s "
            f"candidate_build_count={int(queue.get('candidate_build_count') or 0)} "
            f"negative_control_count={int(queue.get('negative_control_count') or 0)}"
        )

        if args.include_signal_quality_split_reports:
            split_eval_started_at = time.monotonic()
            _emit("[research_evidence] signal_quality_split_evaluator start")
            split_evaluator = _run_json_command(runner._build_signal_quality_split_evaluator_command(args))
            meta = split_evaluator.get("meta") if isinstance(split_evaluator.get("meta"), dict) else {}
            _emit(
                "[research_evidence] "
                f"signal_quality_split_evaluator done elapsed={time.monotonic() - split_eval_started_at:.2f}s "
                f"spec_count={int(meta.get('spec_count') or 0)} summary_rows={int(meta.get('summary_rows') or 0)}"
            )
        else:
            _emit("[research_evidence] signal_quality_split_evaluator skipped; include_signal_quality_split_reports=false")

    context_overlay_signal_backfill = None
    if args.skip_context_overlay_signal_backfill or (args.skip_negative_pressure_evaluator and args.skip_context_watch_evaluator):
        _emit("[research_evidence] context_overlay_signal_backfill skipped")
    else:
        backfill_started_at = time.monotonic()
        _emit("[research_evidence] context_overlay_signal_backfill start")
        context_overlay_signal_backfill = runner._run_context_overlay_signal_backfill(args)
        _emit(
            "[research_evidence] "
            f"context_overlay_signal_backfill done elapsed={time.monotonic() - backfill_started_at:.2f}s "
            f"date_count={int(context_overlay_signal_backfill.get('date_count') or 0)} "
            f"signal_rows={int(context_overlay_signal_backfill.get('signal_rows') or 0)}"
        )

    event_evidence_store = None
    if args.skip_event_evidence_store or args.skip_causal_event_memory:
        _emit("[research_evidence] event_evidence_store skipped")
    else:
        evidence_started_at = time.monotonic()
        _emit("[research_evidence] event_evidence_store start")
        event_evidence_store = _run_json_command(runner._build_event_evidence_store_command(args))
        _emit(
            "[research_evidence] "
            f"event_evidence_store done elapsed={time.monotonic() - evidence_started_at:.2f}s"
        )

    causal_event_memory = None
    if args.skip_causal_event_memory:
        _emit("[research_evidence] causal_event_memory skipped")
    else:
        causal_started_at = time.monotonic()
        _emit("[research_evidence] causal_event_memory start")
        causal_event_memory = _run_json_command(runner._build_causal_event_memory_command(args))
        _emit(
            "[research_evidence] "
            f"causal_event_memory done elapsed={time.monotonic() - causal_started_at:.2f}s "
            f"memory_rows={int(causal_event_memory.get('memory_rows') or 0)}"
        )

    causal_event_memory_evaluator = None
    if args.skip_causal_event_memory_evaluator or args.skip_causal_event_memory:
        _emit("[research_evidence] causal_event_memory_evaluator skipped")
    else:
        causal_eval_started_at = time.monotonic()
        _emit("[research_evidence] causal_event_memory_evaluator start")
        causal_event_memory_evaluator = _run_json_command(runner._build_causal_event_memory_evaluator_command(args))
        _emit(
            "[research_evidence] "
            f"causal_event_memory_evaluator done elapsed={time.monotonic() - causal_eval_started_at:.2f}s "
            f"evaluation_rows={int(causal_event_memory_evaluator.get('evaluation_rows') or 0)} "
            f"summary_rows={int(causal_event_memory_evaluator.get('summary_rows') or 0)}"
        )

    causal_event_provenance = None
    if args.skip_causal_event_provenance or args.skip_causal_event_memory:
        _emit("[research_evidence] causal_event_provenance skipped")
    else:
        provenance_started_at = time.monotonic()
        _emit("[research_evidence] causal_event_provenance start")
        causal_event_provenance = _run_json_command(runner._build_causal_event_provenance_command(args))
        _emit(
            "[research_evidence] "
            f"causal_event_provenance done elapsed={time.monotonic() - provenance_started_at:.2f}s "
            f"provenance_rows={int(causal_event_provenance.get('provenance_rows') or 0)}"
        )

    negative_pressure = None
    if args.skip_negative_pressure_evaluator:
        _emit("[research_evidence] negative_pressure_evaluator skipped")
    else:
        negative_started_at = time.monotonic()
        _emit("[research_evidence] negative_pressure_evaluator start")
        negative_pressure = _run_json_command(runner._build_negative_pressure_command(args))
        _emit(
            "[research_evidence] "
            f"negative_pressure_evaluator done elapsed={time.monotonic() - negative_started_at:.2f}s "
            f"evaluation_rows={int(negative_pressure.get('evaluation_rows') or 0)} "
            f"summary_rows={int(negative_pressure.get('summary_rows') or 0)}"
        )

    context_watch = None
    if args.skip_context_watch_evaluator:
        _emit("[research_evidence] context_watch_evaluator skipped")
    else:
        watch_started_at = time.monotonic()
        _emit("[research_evidence] context_watch_evaluator start")
        context_watch = _run_json_command(runner._build_context_watch_command(args))
        _emit(
            "[research_evidence] "
            f"context_watch_evaluator done elapsed={time.monotonic() - watch_started_at:.2f}s "
            f"evaluation_rows={int(context_watch.get('evaluation_rows') or 0)} "
            f"summary_rows={int(context_watch.get('summary_rows') or 0)}"
        )

    adversarial_review = None
    if args.skip_adversarial_review_evaluator:
        _emit("[research_evidence] adversarial_review_evaluator skipped")
    else:
        adversarial_started_at = time.monotonic()
        _emit("[research_evidence] adversarial_review_evaluator start")
        adversarial_review = _run_json_command(runner._build_adversarial_review_evaluator_command(args))
        _emit(
            "[research_evidence] "
            f"adversarial_review_evaluator done elapsed={time.monotonic() - adversarial_started_at:.2f}s "
            f"evaluation_rows={int(adversarial_review.get('evaluation_rows') or 0)} "
            f"summary_rows={int(adversarial_review.get('summary_rows') or 0)}"
        )

    action_transition = None
    if args.skip_action_transition_evaluator:
        _emit("[research_evidence] action_transition_evaluator skipped")
    else:
        transition_started_at = time.monotonic()
        _emit("[research_evidence] action_transition_evaluator start")
        action_transition = _run_json_command(runner._build_action_transition_evaluator_command(args))
        _emit(
            "[research_evidence] "
            f"action_transition_evaluator done elapsed={time.monotonic() - transition_started_at:.2f}s "
            f"evaluation_rows={int(action_transition.get('evaluation_rows') or 0)} "
            f"summary_rows={int(action_transition.get('summary_rows') or 0)}"
        )

    action_evidence_provenance = None
    if args.skip_action_evidence_provenance:
        _emit("[research_evidence] action_evidence_provenance skipped")
    else:
        action_provenance_started_at = time.monotonic()
        _emit("[research_evidence] action_evidence_provenance start")
        action_evidence_provenance = _run_json_command(runner._build_action_evidence_provenance_command(args))
        _emit(
            "[research_evidence] "
            f"action_evidence_provenance done elapsed={time.monotonic() - action_provenance_started_at:.2f}s "
            f"provenance_rows={int(action_evidence_provenance.get('provenance_rows') or 0)}"
        )

    context_overlay_reliability = None
    if args.skip_context_watch_evaluator and args.skip_negative_pressure_evaluator:
        _emit("[research_evidence] context_overlay_reliability_report skipped")
    else:
        reliability_started_at = time.monotonic()
        _emit("[research_evidence] context_overlay_reliability_report start")
        context_overlay_reliability = _run_json_command(runner._build_context_overlay_reliability_command(args))
        _emit(
            "[research_evidence] "
            f"context_overlay_reliability_report done elapsed={time.monotonic() - reliability_started_at:.2f}s "
            f"status={context_overlay_reliability.get('status')}"
        )

    window_preflight = None
    window_runner = None
    if args.run_signal_quality_window_runner and not args.skip_signal_quality:
        window_preflight, window_runner = runner._run_signal_quality_window_with_preflight(args)

    promotion_review = None
    if args.skip_signal_quality_promotion:
        _emit("[research_evidence] signal_quality_auto_promotion skipped")
    elif args.run_signal_quality_window_runner:
        _emit("[research_evidence] signal_quality_auto_promotion skipped; window runner owns stable-gated reviews")
    elif args.skip_signal_quality:
        _emit("[research_evidence] signal_quality_auto_promotion skipped; signal_quality skipped")
    else:
        promotion_started_at = time.monotonic()
        _emit("[research_evidence] signal_quality_auto_promotion start")
        promotion_review = _run_json_command(runner._build_signal_quality_auto_promotion_command(args))
        _emit(
            "[research_evidence] "
            f"signal_quality_auto_promotion done elapsed={time.monotonic() - promotion_started_at:.2f}s "
            f"status={promotion_review.get('status')} review_count={promotion_review.get('review_count')}"
        )

    payload = runner._build_research_evidence_payload(
        args=args,
        signal_summary=signal_summary or {},
        family_report=family_report or {},
        split_queue=split_queue,
        split_evaluator=split_evaluator,
        context_overlay_signal_backfill=context_overlay_signal_backfill,
        negative_pressure=negative_pressure,
        context_watch=context_watch,
        adversarial_review=adversarial_review,
        action_transition=action_transition,
        action_evidence_provenance=action_evidence_provenance,
        event_evidence_store=event_evidence_store,
        causal_event_memory=causal_event_memory,
        causal_event_memory_evaluator=causal_event_memory_evaluator,
        causal_event_provenance=causal_event_provenance,
        context_overlay_reliability=context_overlay_reliability,
        window_preflight=window_preflight,
        window_runner=window_runner,
        promotion_review=promotion_review,
    )
    payload["research_evidence"]["runner"] = "advisory.research_evidence_runner"
    payload["research_evidence"]["elapsed_seconds"] = round(time.monotonic() - started_at, 3)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = run_research_evidence(args)
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
