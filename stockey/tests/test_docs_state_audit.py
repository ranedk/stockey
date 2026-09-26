from __future__ import annotations

from pathlib import Path

from scripts import docs_state_audit


def test_docs_state_audit_flags_removed_static_dashboard_command(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    text = "Cron no longer runs python -m advisory.live_dashboard.\n"

    findings = docs_state_audit.check_stale_terms(path, text, repo_root=tmp_path)

    assert [(finding.severity, finding.code) for finding in findings] == [
        ("error", "removed_static_dashboard_command")
    ]


def test_docs_state_audit_allows_explicit_legacy_references(tmp_path: Path) -> None:
    path = tmp_path / "docs.md"
    text = "\n".join(
        [
            "Static `live_dashboard/` generation is deprecated.",
            "Reliability checks use the legacy backend promotion-audit endpoint.",
        ]
    )

    assert docs_state_audit.check_stale_terms(path, text, repo_root=tmp_path) == []


def test_docs_state_audit_warns_on_unqualified_legacy_terms(tmp_path: Path) -> None:
    path = tmp_path / "docs.md"
    text = "Run the promotion audit from the dashboard in live_dashboard/.\n"

    findings = docs_state_audit.check_stale_terms(path, text, repo_root=tmp_path)

    assert [(finding.severity, finding.code) for finding in findings] == [
        ("warning", "legacy_promotion_audit_label"),
        ("warning", "legacy_live_dashboard_path"),
    ]
