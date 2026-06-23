# CLAUDE.md

This file is the operating guide for AI coding agents working on Stockey.

Stockey is an Indian-equity advisory research and operator system. Treat it as a
financial decision-support platform, not as a toy app. Correctness, point-in-time
discipline, traceability, and explicit authority boundaries matter more than UI
polish or broad refactoring.

## Project Principles

- Do not let an LLM directly decide trades or bypass deterministic gates.
- Use LLMs for structured extraction, event interpretation, hypothesis/playbook
  notes, company-memory summaries, adversarial review notes, and operator
  explanations.
- Trading authority must flow through deterministic technical, risk, lifecycle,
  action-consolidation, reason-contract, identity, and execution-planning gates.
- A broad single regime label must not be the default explanation for no BUY
  recommendations. Prefer layered context: breadth, macro stress, sector/symbol
  leadership, technical confirmation, source-family overlays, and exact event
  class reliability.
- Every fallback, degradation, skipped source, stale input, and data-quality
  issue should be visible through telemetry, sync state, Operator Health, logs,
  or diagnostics. Avoid silent fallback.
- Preserve point-in-time behavior. Do not use future data, backdated portfolio
  assumptions, stale latest rows, or non-causal labels.
- Prefer research-only evidence and manual reviewed-diff workflows before any
  config or threshold changes.
- Do not mutate broker behavior unless the user explicitly asks for that task.

## Current Architecture

Main pipeline:

1. `complete_data.sh` downloads/parses raw market, macro, company, NSE, news,
   announcement, and compact evidence.
2. Screeners and ad hoc Screener.in queries create candidate universes.
3. Dhan OHLCV, macro, bhavcopy, announcement, exchange-event, fundamental,
   peer, and intraday feature builders persist point-in-time features.
4. Context overlays create review-only pressure from macro, announcement,
   bhavcopy, exchange, theme/news, and causal-memory evidence.
5. Watchers incrementally consume OHLCV, news, announcements, wait signals, and
   context-overlay watch rows.
6. Signal refresh creates fast review-only action rows. These rows must retain
   `portfolio_authority=none`, `broker_execution_allowed=false`, and
   `full_advisory_required=true` unless a full advisory path later validates
   them.
7. `all_advisory.sh` performs authoritative reconciliation: context signal
   refresh, context-watchlist reconciliation, causal-memory signal refresh,
   rule/technical scoring, event policy, risk, portfolio, lifecycle, action
   consolidation, operator snapshots, trace summaries, and diagnostics.
8. Action consolidation should produce one final action per symbol.
9. Execution planning can only consume validated, broker-capable action
   contracts.
10. Research jobs evaluate technical thresholds, context overlays, event policy,
    signal quality, adversarial review, TS forecasts, and optional ML separately
    from live authority.

## Important Authority Boundaries

- `WATCH`, `BUY_WATCH`, `REDUCE_EXPOSURE_REVIEW`, `TIGHTEN_STOP`, and most
  signal-refresh rows are review-only unless explicitly upgraded by full
  advisory/risk/lifecycle/action gates.
- Context overlays are watchlist/de-risk pressure only. They must not create
  direct BUY or SELL authority.
- Event-policy and company-memory bridges can create review-only watch/de-risk
  signals, not broker-capable actions.
- Technical threshold calibration is research-only. It can propose candidate
  reviewed patches, but it must not auto-apply thresholds.
- Signal-quality promotion/reliability checks are research/review gates. Raw
  positive returns are not enough; use benchmark-excess and sector/exact-class
  diagnostics.
- Manual Review should be reserved for truly unresolved, high-impact ambiguity.
  Routine low-actionability rows should resolve to `NO_ACTION`, `WATCH`, or
  `REDUCE_EXPOSURE_REVIEW` with explicit no-broker authority.

## Current Development Priorities

1. Improve BUY underparticipation using evidence, not intuition.
   - Use `advisory.recommendation_diagnostics` and
     `advisory.technical_threshold_calibration`.
   - Current evidence has repeatedly shown no-BUY causes are mostly technical:
     missing entry trigger, weak participation, total technical score below BUY
     threshold, breakout volume, pivot clearance, close quality, and stale
     candidate evidence.
   - Do not loosen global regime policy to force BUYs.

2. Continue replacing single-regime thinking with layered context.
   - Keep broad regime as annotation/context.
   - Use source-family, exact event class, sector split, macro/breadth, and
     benchmark-excess evidence.

3. Make context-to-entry reliable.
   - Fresh context rows should become durable watchlist pressure.
   - Durable watch rows should get point-in-time technical prechecks.
   - Technical/risk/lifecycle confirmation should be required before portfolio
     or broker authority.

4. Keep source degradation visible.
   - NSE/Dhan/Screener/announcement/macro failures should persist fallback
     telemetry and sync-state health.
   - Do not hide source unavailability as empty data.

5. Keep research evidence operational.
   - Technical threshold calibration, signal-quality split reports,
     event-policy evaluator, causal-memory evaluator, and adversarial-review
     evaluator should remain easy to run and inspect.

6. Performance matters, but correctness comes first.
   - Avoid long full-advisory runs when a bounded repair is sufficient.
   - Do not parallelize browser-bound NSE/Dhan/Screener flows aggressively;
     these sources are rate/session sensitive.

## Key Commands

Daily/operator:

```sh
./complete_data.sh
./all_watchers.sh
./all_context_to_entry_repair.sh
./all_advisory.sh
./all_frontend.sh
```

Research:

```sh
./all_research_evidence.sh
./all_ml.sh
python -m advisory.technical_threshold_calibration --dry-run --horizons 5 10 20 --max-configs 512 --progress-every 128
python -m advisory.recommendation_diagnostics --format text
python -m advisory.operator_health --skip-dhan
```

Cron:

```sh
python builder.py
python scripts/cron_preflight.py
./go-crond config/stockey.generated.crontab --allow-unprivileged
```

Dhan/Screener browser automation:

```sh
scripts/start_chrome_cdp.sh
```

Keep `CDP_ENDPOINT=http://localhost:9222`. Dhan auto-login should fail hard if
Chrome/CDP is unavailable; do not add a hidden manual-consent fallback unless
explicitly requested.

## Files To Inspect First

- `README.md` for operator script groups and current system flow.
- `todo.md` for current roadmap and active project state.
- `todo.md` for the priority backlog, current-state summary, and remaining gaps.
- `docs/operators_manual.md` for runbooks.
- `docs/scripts.md` for script inventory.
- `advisory/recommendation_diagnostics.py` for no-BUY and stale-evidence
  diagnosis.
- `advisory/action_recommender.py` for consolidated action authority.
- `advisory/technical_engine.py` and `advisory/technical_threshold_calibration.py`
  for technical setup and threshold evidence.
- `advisory/context_overlay_refresh.py`, `advisory/watchlist_builder.py`, and
  `advisory/signal_refresh.py` for context-to-entry flow.
- `advisory/operator_health.py` for visible failures, trust gates, and fix hints.

## Coding Rules

- Use `rg` / `rg --files` for search.
- Use `apply_patch` for manual edits.
- Preserve unrelated dirty worktree changes.
- Do not run destructive git commands unless explicitly asked.
- Add narrow tests for every behavioral change.
- Prefer bounded, focused fixes over large refactors.
- Keep existing design language and operator contracts unless there is a
  correctness issue.
- Keep new comments rare and useful.
- Default to ASCII in new files.

## Validation Checklist

Choose the smallest meaningful set for the change:

```sh
python -m py_compile path/to/module.py
pytest -q tests/test_advisory_regression.py::specific_test_name
python scripts/docs_state_audit.py --strict
python scripts/env_example_audit.py --strict
git diff --check
```

For UI/API changes, also use:

```sh
npm --prefix apps/operator-web run typecheck
python scripts/api_performance_report.py --limit 20
```

For cron/script changes:

```sh
python scripts/cron_preflight.py
python scripts/docs_state_audit.py --strict
```

## Research And Policy Guardrails

- Evaluate signals after costs.
- Compare context/event families against NIFTY or sector benchmark excess
  returns. Do not treat market beta as alpha.
- Require enough matured labels before trusting calibration.
- Keep false-discovery and backtest-overfitting risk visible.
- Do not promote thresholds, event rules, or source-family overlays from one
  good-looking sample.
- Prefer sector/exact-class reliability over broad source-family labels.
- Keep review outputs explicit: `candidate`, `do_not_relax`, `needs_more_data`,
  `benchmark_beta_not_alpha`, `needs_benchmark_attribution`, or
  `manual_review_required`.

## What Not To Do

- Do not make BUYs appear by weakening all thresholds globally.
- Do not use a broad `RISK_ON` or `RISK_OFF` regime as the sole trade gate.
- Do not let review-only watcher rows enter portfolio/execution.
- Do not hide Dhan/NSE/Screener failures as empty outputs.
- Do not add more UI unless it surfaces a critical backend truth or fixes an
  operator-trust issue.
- Do not assume latest rows are current; use trading-day-aware diagnostics and
  sync-state contracts.

## Good Next Slice Pattern

For most work:

1. Read `todo.md` and the relevant module.
2. Pick one bounded correctness or traceability gap.
3. Implement the minimal backend change.
4. Add a focused regression test.
5. Update `todo.md` if project state changed.
6. Run compile, focused tests, docs audit, and diff check.
7. Report what changed, what was validated, and the next most important blocker.
