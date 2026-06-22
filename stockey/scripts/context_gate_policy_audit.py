from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from advisory.recommendation_diagnostics import current_context_gate_policy_snapshot


def build_context_gate_policy_audit() -> dict[str, Any]:
    snapshot = current_context_gate_policy_snapshot()
    exposure = snapshot.get("single_regime_hard_gate_exposure") if isinstance(snapshot.get("single_regime_hard_gate_exposure"), dict) else {}
    active_single_regime_flags = list(exposure.get("active_single_regime_flags") or [])
    active_context_hard_flags = list(exposure.get("active_context_hard_flags") or [])
    return {
        "status": exposure.get("status") or "unknown",
        "active_single_regime_flags": active_single_regime_flags,
        "active_context_hard_flags": active_context_hard_flags,
        "global_regime_label_blocks_buy": bool(exposure.get("global_regime_label_blocks_buy")),
        "operator_action": exposure.get("operator_action"),
        "policy_summary": snapshot.get("policy_summary") if isinstance(snapshot.get("policy_summary"), dict) else {},
        "env": snapshot.get("env") if isinstance(snapshot.get("env"), dict) else {},
        "policy_boundary": {
            "report_mode": "read_only_env_policy_audit",
            "portfolio_authority": "none",
            "broker_execution_allowed": False,
            "decision_use": "fail fast when broad regime labels are accidentally restored as hard BUY gates",
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only audit for Stockey context-gate policy. It verifies whether broad regime labels are configured "
            "as hard BUY gates instead of diagnostic/layered context."
        )
    )
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument(
        "--fail-on-single-regime",
        action="store_true",
        help="Exit non-zero if any broad single-regime hard gate is enabled.",
    )
    parser.add_argument(
        "--fail-on-any-hard-context",
        action="store_true",
        help="Exit non-zero if any hard context gate is enabled, including non-regime macro/breadth gates.",
    )
    return parser.parse_args(argv)


def exit_code_for_payload(
    payload: dict[str, Any],
    *,
    fail_on_single_regime: bool = False,
    fail_on_any_hard_context: bool = False,
) -> int:
    if fail_on_single_regime and payload.get("active_single_regime_flags"):
        return 2
    if fail_on_any_hard_context and (payload.get("active_single_regime_flags") or payload.get("active_context_hard_flags")):
        return 3
    return 0


def _print_text(payload: dict[str, Any]) -> None:
    print(f"context_gate_policy status={payload.get('status')}")
    print(f"global_regime_label_blocks_buy={payload.get('global_regime_label_blocks_buy')}")
    print(f"active_single_regime_flags={','.join(payload.get('active_single_regime_flags') or []) or 'none'}")
    print(f"active_context_hard_flags={','.join(payload.get('active_context_hard_flags') or []) or 'none'}")
    print(f"operator_action={payload.get('operator_action') or ''}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    payload = build_context_gate_policy_audit()
    if args.format == "json":
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_text(payload)
    return exit_code_for_payload(
        payload,
        fail_on_single_regime=bool(args.fail_on_single_regime),
        fail_on_any_hard_context=bool(args.fail_on_any_hard_context),
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
