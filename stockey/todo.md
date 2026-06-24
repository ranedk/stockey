# Stockey Priority Todo

Updated: `2026-06-23`

This is the single active planning document for Stockey. It is the priority-ordered
backlog and current-state summary. It was rebuilt from a deep code+log audit, so
each task below is written to be picked up by an LLM agent working with a small
context window: read only the named files, follow the steps, obey the authority
guardrail, and run the listed validation.

## How To Use This File (LLM Task Contract)

Each task is a self-contained card:

- **Why** — the problem and evidence.
- **Files** — the only files you need to open. Prefer `rg` to locate the exact
  lines; line numbers are approximate and drift.
- **Do** — concrete steps.
- **Guardrail** — the authority/point-in-time boundary you must not break.
- **Validate** — the smallest meaningful check + acceptance criterion.
- **Confidence** — `verified` (audited against current source), or
  `verify-first` (claim is plausible but you must confirm the code still behaves
  this way before changing it; if the bug does not exist, record that and stop).

Global rules for every task:

- LLM decision authority (operator decision 2026-06-23, see CLAUDE.md): an LLM may
  decide review outcomes and trades (incl. BUY/SELL/size) when it has reviewed the
  evidence and recorded strong, provenance-backed reasons; the deterministic gates
  are advisory guardrails an LLM decision may override with a recorded rationale.
  No human manual-review step. STILL non-negotiable: typed evidence/provenance +
  reason contract on every decision, point-in-time discipline, a default-OFF master
  flag for any live LLM→broker authority, and paper/shadow graduation (after-cost +
  benchmark-excess + regime-robust) before live. Until the [P-LLM-AUTH] epic builds
  and enables that path, existing review-only rows keep `broker_execution_allowed=false`
  by default — do not flip an existing module to live LLM authority ad hoc.
- Do not use one broad regime label as the sole BUY/SELL gate. Prefer layered
  context (breadth, macro stress, sector/symbol leadership, technical
  confirmation, source-family + exact event-class reliability, benchmark-excess).
- Every fallback, stale source, skipped symbol, parser failure, and auth issue
  must stay visible (logs, sync state, fallback telemetry, Operator Health,
  diagnostics). Never hide a source failure as empty data.
- Keep point-in-time discipline. No future data, backdated portfolio, or
  non-causal labels.
- Add a narrow test for every behavioral change. Prefer bounded fixes over
  refactors. Preserve unrelated dirty-worktree changes.

Default validation (pick the smallest meaningful subset):

```sh
python -m py_compile <changed modules>
pytest -q tests/test_advisory_regression.py::<specific_test>
python scripts/docs_state_audit.py --strict
python scripts/env_example_audit.py --strict
git diff --check
# UI/API changes:
npm --prefix apps/operator-web run typecheck
python scripts/api_performance_report.py --limit 20
# cron/script changes:
python scripts/cron_preflight.py
```

## Primary Operator Commands

- Download + parse + compact/context refresh: `./complete_data.sh`
- Continuous monitoring: `./all_watchers.sh --loop`
- Context-to-entry repair: `./all_context_to_entry_repair.sh`
- Authoritative advisory: `./all_advisory.sh` (fast: `--fast`)
- Research evidence: `./all_research_evidence.sh`
- Optional ML/research: `./all_ml.sh`
- Frontend/API: `./all_frontend.sh`
- Diagnostics: `python -m advisory.recommendation_diagnostics --format text`
- Operator Health: `python -m advisory.operator_health --skip-dhan`

Use `python -m advisory.pipeline` only for stage-level debugging.

---

## P0 - Active Failures Blocking Evidence Or Runs

These were found in `logs/fallback/` and `logs/cron/`. Fix these before tuning
any policy: the evidence you would tune from is currently being corrupted or
blocked.

> **Status (2026-06-23 implementation pass).** On verification, three of the five
> were already fixed in the working tree and only flagged by stale Jun 19-21 logs:
> - **P0.2 (migration checksum):** already fixed by commit `0cb4401`
>   (`BASE_CANDIDATE_COLUMNS`). Live checksum matches the applied record
>   (`793bc4…`, MATCH). No change made.
> - **P0.4 (intraday unmapped Dhan id):** already guarded — current
>   `ensure_intraday_history` wraps the per-symbol sync in
>   `try/except (DhanAPIError, ValueError)` with fallback + `continue`. No change.
> - **P0.3 (NSE `EGR` holiday):** the specific `EGR` key is already in the segment
>   map. Implemented the defensive `.get` + `nse_holidays_unknown_segment`
>   telemetry anyway so the *next* unknown NSE segment cannot crash the download.
>
> Implemented this pass: **P0.1** (incl. the sibling `_json_ready_record` with the
> same latent bug), **P0.3** hardening, **P0.5** (`dhan_consent_limit_exceeded`
> Operator Health pattern). 6 tests added/updated; 13 targeted tests green.
>
> **New finding → see [P0.6].** The advisory regression suite is currently RED on
> this worktree: 16 pre-existing failures unrelated to these fixes.

### [P0.6] Triage 16 pre-existing advisory-regression failures  ✅ DONE (suite green: 2024 passed, 0 failed)
- **Outcome (2026-06-23):** All 16 fixed. **7 genuine code regressions** from the WIP
  commits — fix in production: conflict-precedence ordering (ADVERSARIAL_VETO must
  beat MARKET_GATE) in `action_recommender.py`; `recent_events.py` `rows_read`
  (single-fetch semantics); `signal_refresh.py` de-risk suppression reason
  precedence; and four in `advisory/api/app.py` `_compact_action_reason_contract`
  / watchlist payload (humanize `status`+`missing_fields`, surface
  `event`/`macro_regime`/deep `conflict_resolution`, and a ts-forecast-aware
  compaction so `model_name` survives). **9 stale tests** realigned to intended
  WIP contracts (migration count, transition-gate `position_status`, unconditional
  market-context annotation, `BUY_WATCH→WATCH`, expired date fixture, off-by-one
  index, `skip_if_current` kwarg, internal `captured["query"]` typo, cosmetic
  wording). No assertions weakened.

- **Why:** `pytest tests/test_advisory_regression.py` reports **16 failed, 2008
  passed** on the current worktree, and they fail with the P0 source edits stashed
  too — so they predate this work (WIP commits `0cb4401`/`2e1f5d0`). A red
  regression suite undermines trust in every other change. Examples:
  `test_rule_engine_ensure_output_tables_uses_schema_registry`
  (assertion `allocated` vs `rejected`), `test_recent_events_main_exports_runner_state`,
  `test_risk_engine_falls_back_to_base_candidates_without_event_rows`,
  `test_operator_api_splits_dashboard_payload`,
  `test_action_recommender_bridges_event_policy_actions`,
  `test_watchlist_builder_suppresses_context_overlay_hard_ohlcv_blockers`,
  `test_signal_refresh_*`, `test_recommendation_diagnostics_labels_stale_pass_now_*`.
- **Do:** Run the full list, group by root cause (several look like recent WIP
  behavior changes that the tests were not updated for), and for each either fix
  the code or update the test to the intended new contract. Treat any that reflect
  a real behavior regression as its own P0.
- **Guardrail:** Do not delete/weaken assertions to go green; confirm the intended
  behavior first.
- **Validate:** `pytest -q tests/test_advisory_regression.py` returns 0 failures.
- **Confidence:** verified (reproduced on baseline).

### [P0.1] Fix `pd.isna()` on non-scalar in action_recommender context builder  ✅ DONE
- **Why:** `_json_context_value` runs `if pd.isna(value):` on values that can be
  lists / dicts / numpy arrays, raising "truth value of an empty array is
  ambiguous". The `except` swallows it and emits fallback type
  `action_recommender_context_missing_check_failed` on every call — ~1,000 events
  per 2,000 fallback-log lines (tens of thousands/day). It is non-fatal but
  floods telemetry and hides real signals, violating "avoid silent fallback".
- **Files:** `advisory/action_recommender.py` — `_json_context_value` (~line 1260,
  the `pd.isna` at ~1264 and fallback emit at ~1269).
- **Do:** Guard the scalar check exactly like the existing correct call sites in
  the same file (lines ~516 and ~2493): `if pd.api.types.is_scalar(value) and
  pd.isna(value):`. Non-scalars must return the value unchanged without entering
  the isna branch and without emitting fallback telemetry.
- **Guardrail:** Behavior-preserving for scalars; only stops the spurious
  exception/fallback for containers.
- **Validate:** `python -m py_compile advisory/action_recommender.py`; add a test
  in `tests/test_advisory_regression.py` feeding `[]`, `[1,2]`, `np.array([])`,
  `{}`, `None`, `np.nan`, `1.0` through `_json_context_value` asserting no
  exception and correct passthrough/None. Acceptance: a fresh advisory run stops
  accumulating `action_recommender_context_missing_check_failed`.
- **Confidence:** verified.

### [P0.2] Resolve rule_engine migration checksum mismatch (blocks all_ml)  ✅ ALREADY FIXED (stale log)
- **Why:** `all_ml.log` shows `Migration checksum mismatch for
  20260611_advisory_rule_outputs_base`, which hard-fails `event_model_data_prep`
  and any path calling `ensure_rule_output_tables()`. Root cause: an
  already-applied migration's SQL was edited in place.
- **Files:** `advisory/rule_engine.py` (`RULE_ENGINE_SCHEMA_MIGRATION_ID` ~line 29
  and `RULE_ENGINE_SCHEMA_STATEMENTS`), `utils/schema_migrations.py` (~line 70).
- **Do:** First `git log -p -- advisory/rule_engine.py | grep -A30
  RULE_ENGINE_SCHEMA_STATEMENTS` to see what changed. Then EITHER (a) revert the
  edited SQL to its applied form and add a NEW migration id for the intended
  change, OR (b) if the live DB already matches the new SQL, add an explicit,
  operator-gated re-baseline path in `schema_migrations` that records the new
  checksum. Do NOT silently relax/skip checksum validation.
- **Guardrail:** Migrations stay append-only; no silent checksum override.
- **Validate:** `python -m advisory.event_model_data_prep --format json
  --min-labeled-rows 20 --horizons 1` runs without the ValueError;
  `python -m py_compile advisory/rule_engine.py utils/schema_migrations.py`.
- **Confidence:** verify-first (confirm the mismatch still reproduces).

### [P0.3] Make NSE holidays parser tolerant of unknown segment keys  ✅ DONE (hardened)
- **Why:** `complete_data.log` shows `KeyError: 'EGR'` in
  `download_holidays` — NSE now returns an Electronic Gold Receipts segment key
  absent from the hardcoded `nse_product_info` map, aborting the holidays
  download and degrading trading-calendar freshness.
- **Files:** `data/nseindia/holidays.py` (`nse_product_info` map ~lines 34-100;
  the lookup `nse_product_info[k]['name']` ~line 146).
- **Do:** Replace the hard index with `nse_product_info.get(k, {}).get('name', k)`
  and record a fallback telemetry event (`nse_holidays_unknown_segment`) listing
  the unknown key, instead of raising. Optionally add the `EGR` entry too.
- **Guardrail:** Unknown segments degrade visibly (telemetry), not silently.
- **Validate:** `python -m py_compile data/nseindia/holidays.py`; unit test that a
  product dict containing `EGR` parses without KeyError and emits one fallback.
- **Confidence:** verified (from log).

### [P0.4] Per-symbol skip + telemetry for unmapped Dhan security id  ✅ ALREADY FIXED (stale log)
- **Why:** `all_advisory.log` shows `ValueError: No Dhan security id mapped for
  NSE:HUIL` during intraday sync. One unmapped symbol (e.g. the HUL spinoff)
  raises through `build_intraday_features` and can abort the batch.
- **Files:** `data/dhanlive/dhan_db.py` (`resolve_dhan_identity` ~line 100),
  `advisory/intraday_features.py` (intraday sync/build loop).
- **Do:** In the intraday build loop, catch the unmapped-identity error per
  symbol, record a fallback telemetry event + an `advisory_identity_issues` row
  (`dhan_security_id_missing`), and continue with remaining symbols.
- **Guardrail:** Missing identity becomes a visible identity issue, never a
  hidden batch abort or silent drop.
- **Validate:** `python -m py_compile` both files; test that one unmapped symbol
  in a batch produces a telemetry/identity row and the other symbols still build.
- **Confidence:** verified (from log).

### [P0.5] Distinct operator message for Dhan CONSENT_LIMIT_EXCEED  ✅ DONE
- **Why:** Dhan auth returns `CONSENT_LIMIT_EXCEED`, which fails
  `dhan_auth_preflight` and aborts the entire advisory run. Hard-fail is the
  intended design, but the generic auth-failure message hides that this is a
  daily-consent-limit condition needing a specific operator action.
- **Files:** `data/dhanlive/auth_cli.py`, `all_advisory_preflight.sh` (where the
  preflight failure is surfaced), `advisory/operator_health.py`
  (DEGRADATION_PATTERNS / fix hints).
- **Do:** Detect `CONSENT_LIMIT_EXCEED` in the auth error and surface a distinct
  message + fix hint ("Dhan consent limit exceeded — wait for daily reset or run
  `auth_cli refresh --clear-cache-first --auto-login` after
  `scripts/start_chrome_cdp.sh`"). Add a matching Operator Health pattern.
- **Guardrail:** Still fail hard; do not add a hidden manual-consent fallback.
- **Validate:** `python -m py_compile`; unit test mapping the error payload to the
  distinct message/hint.
- **Confidence:** verified (from log).

---

## P1 - Decision Correctness And Authority Safety

### [P1.1] Close review-only sanitization bypass for non-standard action codes  ✅ ALREADY CLOSED (regression test added)
- **Why:** `enforce_review_only_signal_boundaries` only sanitizes when the action
  code is in `BROKER_CAPABLE_ACTIONS` or `execution_mode == "broker_order"`. A
  review-only source emitting a non-broker `action_code` (e.g. `HOLD`) while still
  carrying `transaction_type="BUY"` could slip a broker-capable transaction field
  through.
- **Files:** `advisory/action_recommender.py` —
  `enforce_review_only_signal_boundaries` (~line 589-651).
- **Do:** Make sanitization fire when `transaction_type in {"BUY","SELL"}` OR the
  action is broker-capable OR `execution_mode == "broker_order"`. When a
  review-only source carries any broker-capable field, force
  `execution_mode="review_only"`, clear `transaction_type`, and downgrade the
  action to `WATCH`/`REDUCE_EXPOSURE_REVIEW`. Record an audit note.
- **Guardrail:** Review-only sources can never emit a broker-capable transaction.
- **Validate:** Test: a review-only row with `action_code="HOLD",
  transaction_type="BUY"` ends with `execution_mode="review_only"` and no
  broker transaction. Assert no review-only-source row survives with
  `transaction_type in {BUY,SELL}` and `execution_mode != review_only`.
- **Confidence:** verify-first.

### [P1.2] Make non-existent-position SELL/TIGHTEN an execution precondition  ✅ CORE SAFETY VERIFIED + TEST ADDED
- **Outcome (2026-06-23):** Phantom-position safety is already enforced at action
  consolidation: `_action_transition_stability_section` adds the
  `position_state_missing_for_position_transition` blocker when a
  SELL/PARTIAL_SELL/TIGHTEN_STOP/BUY_MORE has no position state, and the default-on
  stability policy gate (`ACTION_TRANSITION_STABILITY_POLICY_GATE_ENABLED`)
  downgrades a broker-order phantom SELL to review-only `REDUCE_EXPOSURE_REVIEW`
  (incomplete contracts go to `MANUAL_REVIEW`) — so it never stays broker-capable.
  Added a regression unit test for the blocker + policy-target downgrade.
  **Residual (optional defense-in-depth, not required for safety):** skip emitting
  the phantom SELL in `position_lifecycle` and add a matching execution-layer
  `missing_preconditions` entry so the block is visible end-to-end.
- **Why:** `position_state_missing_for_position_transition` is added to
  `transition_blockers` (action_recommender ~line 4824), but the execution gate
  (`_action_transition_block_reasons`, execution_engine ~line 1256) keys only off
  `precondition_status`/`missing_preconditions`. So a SELL/TIGHTEN_STOP for a
  position that no longer exists (e.g. after a portfolio reset) may not be
  blocked at execution. Confirm whether the blocker reaches `missing_preconditions`.
- **Files:** `advisory/action_recommender.py` (transition contract build around
  ~4812-4860), `advisory/execution_engine.py`
  (`_action_transition_block_reasons` ~1256, `_action_row_block_reasons` ~1536),
  `advisory/position_lifecycle.py` (SELL/TIGHTEN emission).
- **Do:** (1) Verify the gap. (2) If real, add
  `position_state_missing_for_position_transition` to `missing_preconditions`
  (or add a direct execution-row block) so broker submission is blocked. (3) In
  `position_lifecycle`, skip emitting SELL/PARTIAL_SELL/TIGHTEN_STOP when the
  current portfolio snapshot has no open quantity for the symbol.
- **Guardrail:** Never submit a SELL for a position that does not exist; portfolio
  reset must not generate phantom SELLs.
- **Validate:** Test: a BUY_MORE/SELL with no open position → execution
  `submit_blocked`. Test: portfolio-reset snapshot → no SELL rows emitted.
- **Confidence:** verify-first.

### [P1.3] Corporate-action reconciliation for open positions (qty + cost basis)  ✅ DONE (operator P&L; scope corrected)
- **Outcome (2026-06-23):** Investigation showed this system has **no share quantity /
  cost basis** — positions are `entry_price` + percentage P&L — so the real risk is a
  price-scale mismatch, not quantity math. **Definite bug fixed:** the operator paper
  portfolio (`advisory/operator_portfolio.py`) compared a frozen pre-split ledger
  `entry_price` against a re-stated post-split `current_price` (a 1:2 split read a flat
  position as ~-48%). Added `_load_corporate_action_price_factors` (point-in-time
  split/bonus factors from `nseindia_corporate_actions_normalized`) and
  `_apply_corporate_action_adjustments`, which adjusts the entry price onto the
  current/exit scale for ex-dates strictly after entry, tags provenance
  (`entry_price_unadjusted`, `corporate_action_price_factor`, `corporate_action_events`,
  `corporate_action_adjustment_status`), and records a fallback (status `unavailable`)
  when the source can't be read so unadjusted P&L stays visible, never silently wrong.
  Paper-analytics only; no portfolio/broker authority. 5 tests added.
- **Residual (lifecycle, latent/self-healing):** `position_lifecycle` re-derives both
  entry and current from `dhan_ohlcv_daily`, which gets a full re-backfill when a split
  is detected (`data/dhanlive/ohlcv.py` `choose_daily_refresh_start`), so it is correct
  except in the window between the corporate action and that re-backfill. Optional
  follow-up: emit telemetry when an open position spans an unreconciled recent split.
- **Why:** Splits/bonus/symbol changes are handled in OHLCV adjusted prices, but
  open-position quantity and entry cost basis are not retroactively adjusted. A
  1:2 split leaves stale quantity, corrupting P&L, sizing, and SELL quantity.
  (Pairs with P2.9, the ingest side.)
- **Files:** new `advisory/corporate_action_processor.py`;
  `advisory/portfolio_engine.py` (holdings load), `advisory/position_lifecycle.py`
  (entry/cost-basis math). Source: `nseindia_corporate_actions_normalized` and/or
  Dhan corporate actions.
- **Do:** Build a processor that, for each open position, finds splits/bonuses
  between entry_date and asof_date and applies the cumulative price/volume factor
  to quantity and cost basis, tagging the position
  `adjusted_for_corporate_action` with the factor and source. Call it before
  portfolio/lifecycle build. Record a fallback if the action lookup fails.
- **Guardrail:** Point-in-time only — never compare pre/post-split prices without
  the adjustment; record provenance of every adjustment.
- **Validate:** Test: entry 100sh @ ₹500, 1:2 split → 200sh @ ₹250, P&L
  unchanged; SELL uses adjusted quantity.
- **Confidence:** verify-first (confirm portfolio math currently ignores splits).

### [P1.4] Audit trail for inferred vs explicit action authority  ✅ DONE
- **Outcome (2026-06-23):** The CLI authority path (`_authority_defaults_for_cli_row` /
  `normalize_cli_authority_frame`) only *gap-fills* missing authority fields (it never
  overrides explicit values), so the audit records inferred-vs-explicit rather than a
  downgrade. Added non-mutating `_authority_inference_audit(df)` (mirrors the gap-fill:
  a field is inferred when the row lacked an explicit value and a non-None default was
  applied) and folded it into `build_cli_result`'s `authority_summary.authority_inference`
  (`inferred_authority_rows`, `explicit_authority_rows`, `inferred_field_counts`,
  `inferred_rows_by_source`); `format_cli_text` now prints an `Authority inference:` line.
  Computed from the original frame (not the persisted/normalized one), so it adds no
  persisted column and changes no ranking/portfolio/broker state. 2 tests added.
- **Note:** no explicit→inferred *downgrade* path exists in this helper (explicit wins),
  so the audit focuses on visibility of gap-filled authority rather than override alerts.
- **Why:** `infer_action_authority_contract` (~line 7295) silently fills/overrides
  `portfolio_authority`/`broker_execution_allowed`/`full_advisory_required` from
  the action source. A downgrade of an explicit `broker_execution_allowed=true`
  to `false` leaves no trace, and there is no count of inferred-vs-explicit rows.
- **Files:** `advisory/action_recommender.py` (~7295-7323),
  `advisory/recommendation_diagnostics.py` (`_row_diagnostics` ~3112).
- **Do:** When authority is inferred or downgraded, write an
  `authority_inference_log` entry into the row's `raw_context_json`
  (`inferred_from`, `source_value`, `inferred_keys`, and any explicit→inferred
  downgrade). Surface inferred-vs-explicit counts in diagnostics text output.
- **Guardrail:** Audit-only; does not change ranking, portfolio, or broker state.
- **Validate:** Test: a row with explicit `broker_execution_allowed=true` inferred
  to `false` produces an audit entry; diagnostics report shows the count.
- **Confidence:** verify-first.

### [P1.5] Track context-overlay staleness and invalidate stale watch rows  ✅ DONE (scope refined)
- **Outcome (2026-06-23):** Verification showed the 14d intake lookback
  (`WATCHLIST_CONTEXT_OVERLAY_LOOKBACK_DAYS`) already hard-bounds candidate age, so a
  `>14d` hard-REJECT would be dead code in the normal same-day rebuild. Implemented the
  genuinely useful pieces in `advisory/watchlist_builder.py`: persist
  `context_overlay_days_old` in `watch_reasons` (transparency), and demote a positive row
  from `WATCH_BREAKOUT` to `WATCH_EVENT` when backing evidence is older than
  `WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_MAX_AGE_DAYS` (default 7) — recorded as
  `context_overlay_breakout_stale` with a watch_reason note. Review-only either way; no
  schema change, no buy/sell authority. Added `.env.example` entry and 1 test (+ extended
  the fresh-breakout test to assert days_old=0).
- **Note:** hard age-based REJECT is unnecessary in the same-day rebuild because intake
  already excludes overlays older than the lookback; the only cross-asof-date lingering
  risk is for consumers reading historical (non-current-asof) watch rows.
- **Why:** Context watch rows carry no `context_overlay_days_old`; stale evidence
  (>14d) can persist as active `WATCH_BREAKOUT` pressure without refresh or
  invalidation.
- **Files:** `advisory/watchlist_builder.py`
  (`load_context_overlay_watch_candidates` ~1792, context-watch row build
  ~2088-2240; schema columns ~113-122).
- **Do:** Compute `context_overlay_days_old = (asof - context_asof_date).days`,
  persist it in `watch_reasons_json`. When age exceeds a configurable threshold
  (default 14) and the row is not already `REJECT`, emit a `REJECT` audit row with
  reason `suppress_context_stale_overlay_no_trade_authority`.
- **Guardrail:** Watch-only/no-trade authority; a rejection is an audit row, not a
  SELL.
- **Validate:** Test: a 20-day-old overlay yields a stale `REJECT`; a fresh one
  stays active.
- **Confidence:** verify-first.

### [P1.6] Hard gate low technical actionability on WATCH_BREAKOUT  ✅ DONE
- **Outcome (2026-06-23):** In `advisory/watchlist_builder.py`, a positive row is demoted
  from `WATCH_BREAKOUT` to `WATCH_EVENT` when the symbol's point-in-time technical setup is
  covered but weak (`technical_actionability_score < WATCHLIST_CONTEXT_OVERLAY_BREAKOUT_MIN_ACTIONABILITY`,
  default 0.40). Recorded as `watch_breakout_low_technical_actionability_blocked` with a
  watch_reason note. Per the guardrail it is a DEMOTION (never REJECT), and it fires only
  when technical is `technical_covered` and not already a stale-OHLCV blocker — symbols with
  no technical row stay neutral. Review-only; no buy/sell authority. `.env.example` entry +
  1 test added.
- **Why:** `_load_context_technical_actionability` scores readiness but is used
  only for ranking. A `WATCH_BREAKOUT` row with very low actionability still
  enters advisory as high-urgency context.
- **Files:** `advisory/watchlist_builder.py`
  (`_load_context_technical_actionability` ~269-361; promotion to
  `WATCH_BREAKOUT` ~2088-2220).
- **Do:** When `candidate_state == WATCH_BREAKOUT` and
  `technical_actionability_score < threshold` (default 0.40), downgrade to
  `WATCH_EVENT` (not REJECT) and record
  `watch_breakout_priority_blocked=low_technical_actionability`.
- **Guardrail:** Downgrade urgency only; never grant BUY authority. Missing
  technical rows stay neutral, not auto-rejected.
- **Validate:** Test: low-score breakout → `WATCH_EVENT` with blocker flag; high
  score → unchanged.
- **Confidence:** verify-first.

### [P1.7] Apply fresh negative-context suppression in signal refresh  ✅ DONE
- **Outcome (2026-06-23):** Confirmed the gap — signal_refresh's positive loader builds from
  `load_context_overlay_watch_candidates`, which does NOT apply negative-context suppression
  (that runs later in watchlist maintenance, line ~2422), so a fresh negative overlay could
  still emit a review-only WATCH. Fixed in `advisory/signal_refresh.py`: added
  `_negative_context_suppressed_symbols` (reuses the builder's reliability-gated
  `load_negative_context_overlay_suppression_candidates`) and wired it into
  `load_positive_context_overlay_watch_target_frames` so negative-suppressed symbols are
  excluded BEFORE the limit and counted in `policy_suppressed_target_rows` with reason
  `suppress_context_fresh_negative_overlay_no_buy_authority`. Fails open with telemetry if the
  negative source is unavailable. Withholds a review-only WATCH only — no sell/portfolio/broker
  authority. 2 tests added.
- **Why:** `watchlist_builder` writes negative-context suppression `REJECT` rows,
  but `signal_refresh` positive-context loader does not consult them, so a fresh
  negative overlay newer than the positive watch can still emit a `WATCH` signal.
- **Files:** `advisory/signal_refresh.py` (positive context loader ~1649-1671 and
  the negative loader `load_negative_context_overlay_suppression_candidates`),
  `advisory/watchlist_builder.py` (suppression candidate query).
- **Do:** In signal refresh, load suppression rows and exclude positive `WATCH`
  signals for symbols whose newest negative context post-dates the positive
  context; record the suppression reason in the run summary
  (`policy_suppressed_target_rows`).
- **Guardrail:** Suppression creates no SELL/portfolio authority; it only
  withholds a review-only WATCH.
- **Validate:** Test: same symbol with positive + newer negative context → no
  WATCH signal, suppression counted.
- **Confidence:** verify-first.

### [P1.8] Make "reliability unavailable" explicit instead of silently neutral  ✅ DONE (scope refined)
- **Outcome (2026-06-23):** Verification showed the safety half is already enforced —
  `_runtime_contract_allows(None, 'watch_priority')` returns False, so a no-reliability row
  already cannot reach WATCH_BREAKOUT (stays WATCH_EVENT), and `_context_reliability_priority_multiplier`
  already distinguishes `no_evidence_neutral` from `classification_neutral`. The real gap was
  that a fully-FAILED reliability load (`load_context_family_reliability` -> {}) looked identical
  to a silently-neutral pass. Fixed in `advisory/watchlist_builder.py`: derive
  `reliability_source_available`, emit a one-per-run `watchlist_builder_context_reliability_unavailable`
  fallback (visible in Operator Health) when no evidence loaded, and tag each context-watch row's
  `watch_reasons.context_reliability_status` as `source_unavailable` / `family_unevaluated` /
  `evaluated`. Does not block explicit watchlist/market-context targets; no authority change. 1 test
  added (+ evaluated-case assertion).
- **Why:** `load_context_family_reliability` falls back persisted→fast→
  signal-quality and finally returns `{}`; downstream treats empty as "no
  restrictions" rather than "reliability unknown", so a row can pass intake when
  required reliability evidence is simply missing.
- **Files:** `advisory/watchlist_builder.py`
  (`load_context_family_reliability` ~921-1089 and its consumers).
- **Do:** Distinguish "evaluated as neutral" from "no evidence available". When no
  evidence exists, tag the row `context_reliability_classification=unavailable`,
  keep it as generic `WATCH_EVENT` (never `WATCH_BREAKOUT`), and surface an
  `unavailable` count in the run summary / Operator Health.
- **Guardrail:** Fail visible, not silently permissive; do not block explicit
  watchlist/market-context targets.
- **Validate:** Test: with all reliability sources empty, a positive overlay
  becomes `WATCH_EVENT` tagged `unavailable`, not `WATCH_BREAKOUT`.
- **Confidence:** verify-first.

### [P1.9] Candidate-state transition audit in watchlist builder  ✅ DONE
- **Outcome (2026-06-23):** P1.5/P1.6/P1.8 had already scattered the individual breakout-gate
  flags into `watch_reasons`; P1.9 consolidates them. Added `_context_watch_candidate_state_audit`
  in `advisory/watchlist_builder.py`, recorded per positive context-watch row as
  `watch_reasons.candidate_state_audit`: `output_state`, `promoted_to_breakout_watch`,
  `breakout_gate_results` (direction/watch_priority/exact_class/sector/reliability_split/
  overlay_fresh/technical_actionability/score_threshold), and `breakout_blocked_by` — so why a
  row is WATCH_EVENT vs WATCH_BREAKOUT is one explainable field instead of scattered booleans.
  Audit-only; derives nothing new; no authority change. 1 unit test + assertions wired into the
  stale/low-technical/breakout integration tests.
- **Note:** REJECT-row rejection reasons remain explicit via `context_policy_effect` +
  `watch_reason_detail` (identity/OHLCV/class/stale/negative suppression), so the audit focused
  on the positive WATCH_EVENT-vs-WATCH_BREAKOUT decision that previously required inference.
- **Why:** Why a context row became `WATCH_BREAKOUT` vs `WATCH_EVENT` vs `REJECT`
  is spread across many gates with no per-row record, making intake debugging
  hard.
- **Files:** `advisory/watchlist_builder.py` (row build/promotion ~2088-2240).
- **Do:** Add a helper that records `{input_state, output_state, gates_passed,
  gates_failed, promotion_reason, rejection_reason}` into `watch_reasons_json`.
- **Guardrail:** Audit-only.
- **Validate:** Test: accept, reject, and breakout-promotion rows each carry a
  populated audit block.
- **Confidence:** verify-first.

---

## P1.10 - LLM-Resolved Review (Epic): the LLM does the review, not a human

**Operator intent (2026-06-23).** "I do not want manual review really. I want the LLM to
do the review if need be. The LLM can web-search or use other data points to make a
decision on items that need manual review." Items that today land in a human Manual Review
queue should instead be resolved by a bounded LLM reviewer that can pull external evidence
(web search and other data points), with a human only as the fallback for genuinely
unresolved or low-confidence cases.

**Current state (verified 2026-06-23 — this intent is NOT yet captured well).**
- A narrow LLM reviewer exists for ONE source only: event-policy rows
  (`advisory/event_policy.py:34` `EVENT_POLICY_LLM_MANUAL_REVIEW_ENABLED`, model `codex`,
  capped by `EVENT_POLICY_LLM_MANUAL_REVIEW_MAX_ROWS=25`). It resolves ambiguous event rows
  into `NO_ACTION` / `BUY_WATCH` / `REDUCE_EXPOSURE_REVIEW` / `MANUAL_REVIEW`
  (`docs/advisory_manual.md:483`). This is the seed to generalize.
- Every other manual-review source (action conflicts, execution blockers, identity issues,
  threshold / signal-quality reviews, wait-signal follow-ups) is routed to a HUMAN. The PRD
  (`docs/operator_app_prd.md:163-200`) is written operator-first ("copy must be written for
  an operator", operator decisions in `advisory_manual_review_decisions`).
- There is NO web-search / external-data capability anywhere in the codebase. The
  event-policy reviewer decides only from the already-loaded source row. The only browser
  automation is the Dhan/NSE/Screener data loaders (Playwright/CDP), not general research.

**Hard guardrails (non-negotiable — these shape every sub-task).**
- LLM resolves the review (operator decision 2026-06-23: LLM, never a human). For the review
  queue the resolver chooses `NO_ACTION` / `WATCH` / `REDUCE_EXPOSURE_REVIEW` / hold. Direct
  BUY/SELL/size by the LLM is governed by the separate [P-LLM-AUTH] epic (default-OFF master
  flag + paper graduation); until that is enabled, review resolution stays review-only.
- Every externally fetched fact (web search or other data point) is evidence with a typed
  provenance record (source URL/pointer, fetch time, query, prompt/schema version) and must
  resolve through identity validation before it can influence a symbol/peer/sector. No
  silent fallback: a failed/empty/low-confidence search is recorded and escalates, it does
  not silently pass.
- No human fallback (operator decision). Low-confidence / contradictory-evidence items do not
  go to a person — the LLM escalates to a higher-rigor review pass (pull more evidence / web
  search) or holds as `NO_ACTION`. "Manual review" becomes "LLM-resolved", not "human-only".
- Point-in-time discipline holds for web evidence too: do not let a search surface
  post-dated information into a historical/as-of decision; stamp and bound by the item's
  asof date where the decision is point-in-time.

**Sub-tasks (each bounded, verify-first, pick up later).**

### [P1.10.1] Inventory and classify every Manual Review source
- **Do:** Enumerate all sources that currently create Manual Review items
  (`advisory/manual_review_state.py`, action-consolidation manual rows, identity issues,
  execution blockers, threshold/signal-quality/event-policy reviews, wait-signal follow-ups).
  For each, record: what decision it needs, what evidence is already loaded, whether external
  research could help, and whether it is safe to auto-resolve vs always-escalate.
- **Deliverable:** a short matrix (source -> needs-external-data? / auto-resolvable? /
  always-escalate?) in `docs/` to drive the rest of the epic.
- **Confidence:** verify-first.

### [P1.10.2] Extract a reusable bounded LLM review-resolver contract
- **Why:** Generalize the event-policy reviewer instead of copy-pasting per source.
- **Files:** new `advisory/llm_review_resolver.py` (or extend `advisory/event_policy.py`'s
  reviewer); reuse `advisory/prompts.py` / `advisory/prompt_registry.py` patterns.
- **Do:** Define one resolver that takes a review item + its compact evidence, returns a
  typed `{resolved_action, confidence, evidence_used, provenance, escalate_to_human}`
  contract with the review-only authority boundary baked in. Persist prompt id/version +
  response schema version (the existing provenance contract).
- **Guardrail:** outputs review-only; confidence below a configurable threshold escalates.
- **Confidence:** verify-first.

### [P1.10.3] Add a web-search / external-data tool with provenance + identity validation
- **Why:** This capability does not exist; it is the core of the operator's ask.
- **Files:** new `advisory/external_research.py` (tool wrapper) + a typed evidence/provenance
  table (mirror `advisory.action_evidence_provenance` / `llm_provenance_audit` conventions);
  `.env.example` for the provider/key.
- **Do:** Wrap a web-search/fetch provider behind one interface that returns
  structured snippets with source URL, fetch timestamp, and query; record every call as
  provenance; run extracted entities through the company/security master before they can
  affect a symbol. Fail visible (telemetry + escalate), never silent.
- **Open decision (ask operator):** which provider; rate/cost caps; allow-list of domains.
- **Guardrail:** research/evidence only; no broker authority; point-in-time aware.
- **Confidence:** verify-first.

### [P1.10.4] Wire the resolver across the highest-value auto-resolvable sources
- **Do:** Starting from the P1.10.1 matrix, route the safe sources through the resolver
  (event-policy first as the proven case, then the next-safest), keeping human escalation for
  the rest. Surface resolved-vs-escalated counts and the evidence/provenance in the existing
  Manual Review / trace surfaces.
- **Guardrail:** review-only outputs; unresolved/low-confidence still escalate; no source's
  authority scope changes.
- **Confidence:** verify-first.

### [P1.10.5] Update the PRD and operator surfaces
- **Why:** The PRD currently says human-only; it must describe "LLM-resolved with human
  fallback".
- **Files:** `docs/operator_app_prd.md` (Manual Review sections ~163-200),
  `docs/advisory_manual.md`, and the Manual Review API/UI copy.
- **Do:** Document the resolver contract, the evidence/provenance shown per resolved item,
  the confidence/escalation policy, and that LLM resolution never mutates portfolio/broker
  state. Keep `docs_state_audit --strict` green.
- **Confidence:** verify-first.

**Open decisions for the operator (resolve before P1.10.3/P1.10.4 build):**
- Which web-search/research provider, and cost/rate caps.
- Confidence threshold for auto-resolve vs escalate to a higher-rigor LLM pass, per source.
- Which sources hold as `NO_ACTION` rather than auto-resolve (e.g. execution blockers,
  identity-master repair) — escalation is to a stronger LLM review, never a human.

---

## P-LLM-AUTH - EPIC: LLM-direct trade decision authority (operator decision 2026-06-23)

**Operator decision.** The owner chose to let the LLM take trade decisions directly (incl.
BUY/SELL/size) when it has reviewed the evidence and has strong, provenance-backed reasons; the
deterministic technical/risk/lifecycle/action gates become **advisory guardrails** an LLM
decision may override with a recorded rationale (not hard blocks). This supersedes the former
"LLM must never decide trades" rule (CLAUDE.md updated). No human in the loop.

**Engineer's recorded dissent + retained safety (not authority limits — reversibility/audit only,
removable by the operator):** the cited 2025-26 leakage-controlled benchmarks (StockBench,
FINSABER, Profit Mirage) found capable LLM trading agents are run-to-run unstable and their
apparent alpha is largely market beta. So this epic keeps: (a) a typed evidence/provenance +
reason contract on every LLM decision (incl. which soft gates it overrode and why); (b)
point-in-time discipline / no lookahead; (c) a **master enable flag that DEFAULTS OFF** for any
live LLM→broker authority; (d) **deterministic risk bounds** — every live decision is sized
within position / exposure / stop caps so no single call is catastrophic; (e) **outcome
monitoring** — live decisions + outcomes + provenance are logged to detect systematic LLM error
patterns (alert, never block). **Paper-first graduation is intentionally NOT used** (operator
decision 2026-06-23: too many non-stationary dimensions for paper P&L to be informative; the floor
is risk bounds + monitoring, not paper validation). Build is **phased; do not wire LLM→broker in
one step.**

**Sub-tasks (phased, each verify-first, operator review per phase):**

### [P-LLM-AUTH.1] Evidence-completeness + data-grounding decision contract  ✅ DONE (revised 2026-06-23)
- **Operator design (2026-06-23):** the LLM may decide, but only over the COMPLETE validated
  evidence packet with data-grounded reasons — never on one news / one announcement / one
  indicator. A competent analyst given the same complete data should reach the same call.
- **Revised model (2026-06-23):** the original "cite >= 3 independent dimensions" rule was too
  rigid — it conflated *number* of signals with *strength* of evidence. Replaced with a clean split
  of **completeness** (did we LOOK at every dimension) from **sufficiency** (is the evidence strong
  enough to act), and sufficiency is met by ANY of three paths, not a count:
  1. **Valid Hypothesis match** — a validated/production investor playbook (the operator's
     experience-encoded sufficiency rule, the PRD "Valid Hypothesis" concept) whose conditions are
     met; can authorize a 1–2 dimension call. This is the primary path.
  2. **Dominant single signal** — one cited dimension with strength >=
     `LLM_DECISION_DOMINANT_SIGNAL_STRENGTH` (default 0.80), e.g. a revenue-material contract or a
     confirmed breakout with strong participation.
  3. **Aggregate corroboration** — summed cited strength >= `LLM_DECISION_AGGREGATE_SIGNAL_STRENGTH`
     (default 1.50). When the packet carries no per-dimension strengths, a legacy count
     (`LLM_DECISION_MIN_INDEPENDENT_CONFIRMATIONS`, default 3) approximates this.
- **Done:** pure, additive `advisory/llm_decision_contract.py` (no DB, no LLM call, no live consumer):
  - `build_evidence_completeness` — required dimensions (technical_confirmation, risk,
    market_context, sector_reliability, exact_class_reliability, benchmark_excess, event_provenance)
    must be looked-at (present + fresh); a genuine gap fails completeness.
  - `evaluate_beta_guard` — scoped to RETURN-based support: flags `beta_only_support` only when
    benchmark-excess is a beta classification or explicitly negative. An event thesis making no
    returns claim (`excess_positive` None) is NOT beta and is not auto-failed (the earlier bug).
  - `evaluate_hypothesis_match` — validated/production playbook with conditions met = sufficient.
  - `validate_decision_grounding` — completeness AND not-beta-only AND sufficiency (any path);
    records `sufficiency_path`, `max_signal_strength`, `aggregate_signal_strength`, `hypothesis_match`.
  - `build_llm_decision_contract` — `meets_data_grounding_for_live` (the DATA bar) plus the
    authority stamp: `broker_execution_allowed=False` always, `live_authority_master_flag=
    LLM_DIRECT_AUTHORITY_ENABLED` (default OFF), `eligible_for_live_authority = data bar AND master
    flag AND graduation_passed`. (schema_version bumped to 2.)
  - `.env.example`: `LLM_DIRECT_AUTHORITY_ENABLED=false`, `LLM_DECISION_DOMINANT_SIGNAL_STRENGTH=0.80`,
    `LLM_DECISION_AGGREGATE_SIGNAL_STRENGTH=1.50`, `LLM_DECISION_MIN_INDEPENDENT_CONFIRMATIONS=3`.
    9 tests (3 sufficiency paths + event-thesis-not-beta + originals).
- **Next bricks consume this:** .4's packet loader populates per-dimension `strength` and the
  `hypothesis_match` from `advisory_hypothesis_matches` (validated/production); .2 already sizes any
  contract that meets the data bar; .5 is the live bridge.

### [P-LLM-AUTH.2] Deterministic risk / position-sizing bounds (the safety floor)  ✅ DONE
- The seatbelt the operator chose. Added pure/additive `advisory/llm_decision_risk_bounds.py`
  (no LLM, no broker): `bound_position_size(contract, capital, price, conviction, atr, current
  sector/total exposure, caps...)` -> bounded sizing plan. Only decisions that pass the .1 data bar
  (`meets_data_grounding_for_live`) are sized; size = min(conviction-scaled position cap, sector
  headroom, total headroom) with the binding constraint recorded; stop = price - ATR*mult; exits
  pass through unsized (they reduce risk). Caps are env-tunable
  (`LLM_DECISION_MAX_POSITION_PCT`=0.05, `MIN`=0.01, `MAX_SECTOR_EXPOSURE_PCT`=0.25,
  `MAX_TOTAL_EXPOSURE_PCT`=1.0, `STOP_ATR_MULT`=2.0). `broker_execution_allowed` always False here.
  5 tests; env documented. This is risk-of-ruin protection, not LLM distrust.

### [P-LLM-AUTH.3] Live decision + outcome monitoring (systematic-error detector)  ✅ DONE
- The other half of the safety floor (with .2's risk bounds): catches *repeatable* mistakes live
  instead of pretending paper P&L is predictive. Added pure/additive
  `advisory/llm_decision_monitor.py` (no DB, no LLM, no broker):
  - `summarize_decision_outcomes` / `_aggregate` — matured-only, benchmark-EXCESS after cost
    (never raw return): excess-hit-rate, mean excess, beta-resolved rate.
  - `detect_systematic_errors` — overall alerts (`llm_decisions_low_excess_hit_rate`,
    `_negative_mean_excess`, `_systematic_beta_tilt`) once matured >= MIN_MATURED, plus per-group
    `_misjudged` flags for `event_class` and `sufficiency_path` (>= MIN_GROUP_MATURED). Thin groups
    yield an `under_baselined` *info* note, not an alert.
  - `build_llm_decision_monitor_report` — `blocking` is ALWAYS False (alert, never block);
    `has_systematic_error`/`alert_count` summarize.
  - `format_monitor_findings_for_operator_health` — maps findings into the Operator Health
    degradation-row shape (alert->error, info dropped) so wiring is trivial once .4 logs real
    decisions. No live consumer yet (no logged decisions exist until .4).
  - `.env.example`: `LLM_DECISION_MONITOR_MIN_MATURED=20`, `_MIN_GROUP_MATURED=8`,
    `_MIN_EXCESS_HIT_RATE=0.45`, `_MIN_MEAN_EXCESS=0.0`, `_BETA_TILT_RATE=0.50`. 5 tests.
- **Wires into Operator Health in .4/.5** once the decision-policy logs real decision+outcome rows.

### [P-LLM-AUTH.4] LLM decision policy over the complete evidence packet  (sliced .4a/.4b/.4c)
- Build the evidence-packet loader + the LLM call that fills the .1 contract from the real packet
  and is sized by .2. Produces grounded BUY/SELL + conviction with provenance. Until the master
  flag is on, output stays review-only.

#### [P-LLM-AUTH.4a] Evidence-packet loader  ✅ DONE
- Added `advisory/llm_evidence_packet.py` (mostly pure, DB loaders defensive). `assemble_evidence_packet`
  turns point-in-time row dicts into the packet the .1 contract consumes: the 7 required dimensions
  + `hypothesis_match`, each normalized to `{present, fresh, strength, classification, ...}`.
  - Schema verified against the live DB first: no stored `technical_total_score` (derived technical
    strength from the `pass_*` entry gates instead); `advisory_signal_quality_eval_summary` absent
    (sector/exact-class/benchmark all sourced from `advisory_context_watch_eval_summary` at the
    source_context / context_class grains).
  - v1 strength formulas (tunable, pinned by tests): technical = fraction of entry `pass_*` gates
    true; risk = conviction-bucket x risk-bucket discount (rejected/abstained -> absent); market =
    `risk_on_score`; sector/exact-class = `excess_opportunity_hit_rate_after_cost` gated by a
    helpful `classification` (else 0); benchmark = `classification` + sign of
    `avg_excess_watch_return_after_cost` (feeds the beta guard); event = `confidence`.
  - `hypothesis_match_dimension` -> the `{status, conditions_met, hypothesis_id}` dict .1 consumes;
    only validated/production hypotheses, conditions_met from explicit decision_json or
    match_score >= `LLM_DECISION_HYPOTHESIS_MIN_MATCH_SCORE` (0.5).
  - `load_evidence_packet(symbol, asof_date, row_loader=, rows_loader=)` wires defensive point-in-time
    SELECTs (injectable for tests); any missing table/column/row degrades to a `present=False`
    dimension, never a crash. 6 tests incl. a .1 integration (packet -> grounding paths).
- **Next:** .4b consumes the packet -> LLM proposal -> .1 contract -> .2 sizing.

#### [P-LLM-AUTH.4b] LLM decision policy  ✅ DONE
- Added `advisory/llm_decision_policy.py`: `decide(packet, capital, price, atr, exposures, ...)`
  asks the LLM (`LlmDecisionProposal`: action, conviction, cited_dimensions, claims_hypothesis_match,
  rationale) via `run_codex_structured`, then grades the proposal through `build_llm_decision_contract`
  (.1) and sizes it with `bound_position_size` (.2). The LLM only proposes; the deterministic layers
  decide grounding + size. The LLM's hypothesis claim never overrides the packet — .1 validates the
  actual `hypothesis_match`.
  - On any LLM failure: deterministic WATCH (conviction 0.2, no cited dims) + `record_fallback_event`
    (`llm_decision_policy_failed`) — never a silent crash, never a default BUY. `use_llm=False`
    yields a `disabled` WATCH. LLM call injectable (`llm_caller`) so it is tested without a model.
  - Review-only: `broker_execution_allowed=False` always; result stamps prompt_id/version/schema/model
    + as-of for provenance. Registered in `advisory/prompt_registry.py` as `llm_decision_policy`
    (authority_scope review_input_only, broker_execution_allowed False). Env `LLM_DECISION_POLICY_MODEL`.
    5 tests (grounded buy graded+sized+review-only, ungrounded not sized, failure->WATCH+telemetry,
    disabled, registry review-only).
- **Next:** .4c persists the decide() result + feeds matured outcomes to the .3 monitor.

#### [P-LLM-AUTH.4c] Decision persistence  ✅ DONE
- Added `advisory/llm_decision_store.py`: `advisory_llm_decisions` via an append-only migration
  (`apply_schema_migration`, id `20260623_advisory_llm_decisions_base`); `persist_decisions(results,
  decided_at=)` maps each `decide()` result -> row via the pure `build_decision_rows` and upserts
  (unique key asof_date+symbol+decided_at, timescaledb_column decided_at). `broker_execution_allowed`
  is forced False on every row (review-only). Stamps prompt_id/version/schema/model + asof + the full
  evidence packet / contract / sizing JSON for provenance. `decide()` now also returns
  `evidence_packet` so `event_class` and the packet are persisted.
- Registered a `ProvenanceSpec` for `advisory_llm_decisions` in `advisory/llm_provenance_audit.py`
  (date_column decided_at, authority_columns broker_execution_allowed -> audit flags any non-False).
- `decisions_to_monitor_records` (pure) adapts persisted rows into the .3 monitor record shape,
  attaching a realized outcome by (symbol, decided_at) when available; `build_decision_monitor_report`
  runs the .3 monitor over them. Until an outcome is attached, a decision is `matured=False` and the
  monitor treats it as not-yet-trustworthy (correct: no realized labels exist until .5).
- **Realized-outcome attachment is deferred to .5/labeling** (the matured benchmark-excess a decision
  is judged on). 4 tests. Full suite 2084 passed.

**[P-LLM-AUTH].4 is COMPLETE** (.4a packet loader + .4b decision policy + .4c persistence). The
decision engine runs end-to-end and review-only; only the live bridge (.5) remains in the epic.

### [P-LLM-AUTH.5] Live bridge behind the default-OFF master flag  ✅ DONE (gated contract only)
- Operator decision (2026-06-23): build the **gated contract only** — the bridge decides authority
  and builds the broker-capable contract, but does NOT submit and does NOT auto-write into
  `advisory_action_recommendations`. Wiring those contracts into the live execution table remains a
  separate, explicitly-requested step; nothing in this commit can move capital even with both flags on.
- Added pure/additive `advisory/llm_broker_bridge.py` — a TRANSLATION layer, not a submit path
  (verified the existing execution-safety contract first):
  - `evaluate_llm_broker_authority(result)` -> `broker_execution_allowed=True` ONLY when ALL hold:
    `LLM_DIRECT_AUTHORITY_ENABLED` master flag on (recomputed at bridge time, default OFF) AND the
    decision is data-grounded (.1) AND graduated (.3) AND sized (.2 `sizing.allowed` for entries)
    AND llm_status ok AND the action is broker-capable. Every failing condition is recorded in
    `blocked_reasons`.
  - `build_broker_action_contract(result, decided_at, reference_price, soft_gate_overrides=)` ->
    the `advisory_action_recommendations`-shaped row execution consumes: `execution_mode=broker_order`,
    `approved_allocation_inr` from .2 sizing, `stop_price`, `full_advisory_required=False`, a COMPLETE
    `reason_contract` embedding full LLM provenance/grounding/sizing + any recorded soft-gate-override
    rationale (CLAUDE.md authority rule), and `broker_execution_allowed` from the gate.
  - It NEVER calls `place_order`/`submit_live_orders`. Any real submission still requires the entire
    unchanged execution-safety contract: a SECOND default-OFF flag `STOCKEY_LIVE_TRADING_ENABLED`,
    the per-run confirmation token, operator approval, broker reconciliation, evidence checklist,
    max-order-value, and the Dhan identity hard-fail in `apply_live_execution_safety()`. Two
    independent default-OFF master flags must BOTH be on for any LLM capital movement — defense in depth.
  - 5 tests (master-flag off blocks, default-OFF, all-gates-pass allows, ungraduated/ungrounded
    blocks + incomplete reason contract, soft-gate-override recorded). No new env var. Full suite 2089.

**[P-LLM-AUTH] EPIC COMPLETE** (.1 contract + .2 risk bounds + .3 monitoring + .4 decision engine +
.5 gated broker bridge). The LLM can produce graduated, data-grounded, risk-bounded, provenance-stamped
trade decisions and a broker-capable contract — all review-only and behind two default-OFF master flags.
**Remaining explicit operator steps before any live trade:** (a) ✅ DONE — realized-outcome labeler
(below); (b) wire approved contracts into `advisory_action_recommendations` / execution (the deferred
.5 injection step); (c) flip `LLM_DIRECT_AUTHORITY_ENABLED` + `STOCKEY_LIVE_TRADING_ENABLED` per
deployment. Each is opt-in.

### [P-LLM-AUTH] Realized-outcome labeler — closes the loop  ✅ DONE
- Added `advisory/llm_decision_outcome_labeler.py` (pure compute + injectable DB loaders). The .3
  monitor judges MATURED benchmark-excess after cost; nothing produced those labels, so every
  decision read `matured=False`. This computes them point-in-time and makes graduation earnable.
  - `compute_decision_outcome` — entry = first close STRICTLY AFTER the decision as-of date, exit =
    horizon-th (mirrors `return_attribution`; no lookahead). Symbol forward return vs benchmark
    forward return, net `cost_bps`, -> `realized_excess_after_cost`, `excess_hit`,
    `resolved_beta_only` (made money but no alpha; entry-only). Entries = long alpha; exits = avoided
    relative move. Immature (horizon not elapsed) -> `matured=False`.
  - `evaluate_policy_graduation(monitor_report)` — the ONLY legitimate source of `graduation_passed`
    (.1/.5): graduates a policy only with >= `LLM_DECISION_GRADUATION_MIN_MATURED` matured outcomes,
    positive mean after-cost excess, and zero .3 systematic-error alerts. Never asserts confidence.
  - `label_decisions(asof_date, ...)` loads directional decisions, labels each, returns
    `outcomes_by_key` that feeds `llm_decision_store.decisions_to_monitor_records` -> the .3 monitor;
    `persist_outcomes` writes `advisory_llm_decision_outcomes` (append-only migration
    `20260624_*`). Env: `LLM_DECISION_OUTCOME_HORIZON_DAYS=20`, `_COST_BPS=25`,
    `LLM_DECISION_GRADUATION_MIN_MATURED=20`. 7 tests. Full suite 2096 passed.
- **The loop is now closed**: decide (.4) -> persist (.4c) -> label outcomes (here) -> monitor (.3)
  -> graduate (here) -> the .5 bridge can grant authority. Live trade still needs steps (b) and (c).

**Open decisions for the operator (before .5):** the risk-bound caps (per-position / per-sector /
total exposure %, stop ATR multiple, min conviction); whether/when to flip the master flag and at
what capital sleeve; per-policy scope; and whether to drop any retained safety item (a–e) — kept by
the engineer as survival/deployment hygiene, but yours to drop.

---

## P2 - Research/Evaluation Rigor And Source Visibility

Policy review is only as trustworthy as the evidence. These close attribution and
visibility gaps. All remain research-only / no auto-apply.

### [P2.1] Benchmark-excess attribution in signal-quality split evaluator  ✅ DONE
- **Outcome (2026-06-23):** The split evaluator reads from `advisory_signal_quality_evaluations`,
  which already carries `excess_forward_return_after_cost` (the main evaluator computes it), but
  the split path never selected/carried/gated it. Added a follow-on ALTER migration
  (`20260623_*_benchmark_excess`, not editing the applied CREATE TABLE), selected
  `excess_forward_return_after_cost` in `load_source_rows`, carried it into split rows, aggregated
  `avg_excess_forward_return_after_cost` in the summary, and gated `classify_summary_row`: a split
  now needs positive benchmark-excess to be `candidate_split_helpful` — otherwise
  `benchmark_beta_not_split_alpha` (excess<=0) or `needs_benchmark_attribution` (excess missing).
  Downstream stability/promotion already treat non-helpful classifications as non-promotable.
  research_only; no auto-apply. 2 tests added + fixtures updated for the benchmark-aware contract.
- **Why:** `signal_quality_split_evaluator` rows have only raw
  `forward_return_after_cost`, no benchmark-excess columns, so split candidates
  can be promoted on market beta.
- **Files:** `advisory/signal_quality_split_evaluator.py` (table schema ~34; row
  build ~466-510). Reuse `attach_benchmark_forward_returns` from
  `advisory/signal_quality_evaluator.py`.
- **Do:** Add `benchmark_name/entry_date/exit_date/forward_return`,
  `excess_forward_return_after_cost`, `excess_hit_after_cost` to split eval rows
  and `avg_excess_forward_return_after_cost` to the summary; require positive
  excess before `candidate_split_helpful`.
- **Guardrail:** research_only, `policy_auto_promotion_allowed=false`.
- **Validate:** Run on a sample; assert excess fields populated and that a
  beta-only split is not classified helpful.
- **Confidence:** verified (schema lacks fields).

### [P2.2] Complete after-cost + excess aggregation in event-policy evaluator  ✅ ALREADY DONE (audit finding was stale)
- **Outcome (2026-06-23):** Verified already complete — the audit snapshot predated the
  `20260622_advisory_event_policy_evaluator_benchmark_attribution` migration. The evaluator
  computes per-row `forward_return_after_cost` + `excess_forward_return_after_cost` +
  `hit_after_cost`/`excess_hit_after_cost` (`event_policy_evaluator.py:485-521`), and
  `summarize_evaluations` (615-645) populates `avg_forward_return_after_cost`,
  `avg_excess_forward_return_after_cost`, after-cost `hit_rate_after_cost`,
  `excess_hit_rate_after_cost`, and `positive_excess_return_rate`. `_summary_recommendation`
  (538-561) gates on benchmark-excess: `needs_benchmark_attribution` (excess missing),
  `benchmark_beta_not_policy_alpha` (positive after-cost but excess<=0), and
  `candidate_policy_strengthen` requires positive after-cost AND positive excess. Covered by
  `test_event_policy_evaluator_builds_rows_and_summary` and siblings. No code change needed.
- **Why:** `event_policy_evaluator` summary aggregation does not compute
  `avg_forward_return_after_cost` / `avg_excess_forward_return_after_cost`
  (schema exists but is unpopulated), so event-policy promotion can read raw beta.
- **Files:** `advisory/event_policy_evaluator.py` (summary aggregation ~530-660).
- **Do:** Compute after-cost average return and benchmark-excess average in the
  summary; ensure hit-rate uses after-cost returns. Document `DEFAULT_COST_BPS`.
- **Guardrail:** research_only.
- **Validate:** Hand-check after-cost/excess on a small sample matches output.
- **Confidence:** verified (aggregation incomplete).

### [P2.3] Maturity / min-sample gating on event-policy & adversarial promotions  ✅ DONE (already gated; regression test added)
- **Outcome (2026-06-23):** Both already gate on maturity. `event_policy_promotion.deterministic_review`
  defaults to `needs_more_data` and every promote/tighten/reject path requires `matured_count >= 30`
  (`event_policy_promotion.py:239-270`) — stricter than the audit's suggested 10 — plus a
  benchmark-excess gate. `adversarial_review_evaluator` has `DEFAULT_MIN_MATURED_ROWS = 10` and
  returns `insufficient_matured_rows` below it (`_summary_recommendation`:514). The `<30` event-policy
  gate was untested (existing tests used 45/60), so added one lock-in test. No code change.
- **Why:** `event_policy_promotion` (and adversarial promotion if present) lack a
  `DEFAULT_MIN_MATURED_ROWS` gate; small samples can reach a promote recommendation.
- **Files:** `advisory/event_policy_promotion.py`,
  `advisory/adversarial_review_evaluator.py` (promotion path).
- **Do:** Add `DEFAULT_MIN_MATURED_ROWS = 10` (mirror
  `signal_quality_promotion.py`); below it, force `needs_more_data`.
- **Guardrail:** research_only.
- **Validate:** Test: a group with <10 matured rows → `needs_more_data`.
- **Confidence:** verified.

### [P2.4] Multiple-testing / false-discovery control for promotions  ✅ DONE (net-new)
- **Outcome (2026-06-23):** No FDR/p-value/dispersion existed anywhere (confirmed net-new).
  Added pure `advisory/multiple_testing.py` (`binomial_right_tail_p_value`, `benjamini_hochberg`,
  `benjamini_hochberg_qvalues`) — dependency-free, no numpy/scipy. Wired Benjamini-Hochberg into
  `signal_quality_promotion.generate_family_candidate_reviews`: across the candidate
  (source family x horizon) batch, each candidate's null is 'beating the benchmark after costs is
  a coin flip' (excess-hit p=0.5), giving a binomial p-value from the already-present
  `excess_hit_rate_after_cost` + `matured_count` (no schema change). Only candidates surviving BH at
  `SIGNAL_QUALITY_PROMOTION_FDR_ALPHA` (default 0.10) generate reviews; the rest are skipped with
  `blocked_by_multiple_testing_fdr_control` (+ p/q values) or `blocked_by_missing_benchmark_excess_stats_for_fdr`
  (fail-closed). research_only; no auto-apply. 4 tests added; one stale fixture (matured=12 marked
  helpful) bumped to a statistically-significant sample.
- **Note (future):** the proportion-based binomial p-value is intentionally simple; a magnitude-based
  t-test would need per-group return std persisted in the evaluator summary (a follow-on if desired).
- **Why:** No FDR/Bonferroni correction across variants×horizons×families;
  uncorrected, several false "candidate_helpful" rows appear by chance.
- **Files:** new `advisory/multiple_testing_correction.py`; apply in
  `advisory/signal_quality_promotion.py` (family candidate generation ~1072-1096)
  and `advisory/signal_quality_split_evaluator.py` (stability classification).
- **Do:** Implement Benjamini-Hochberg (`fdr_corrected(rows, p_or_confidence_field,
  target_fdr=0.05)`) returning `fdr_adjusted_*` and `fdr_rejected_by_control`
  flags; gate promotion recommendations on surviving correction.
- **Guardrail:** research_only; correction only tightens, never loosens.
- **Validate:** Unit test on synthetic p-values vs a reference BH implementation;
  assert rejected rows cannot become promote recommendations.
- **Confidence:** verified (absent).

### [P2.5] Benchmark attribution (or explicit non-applicability) for TS forecast eval  ✅ DONE (net-new)
- **Outcome (2026-06-23):** Genuinely missing — the TS path compared only against a momentum
  baseline (`ts_forecast_paper_portfolio` `lift_vs_momentum`), never against the market index, so
  beta could pass as forecast value. Brought the evaluator in line with every other evaluator:
  follow-on ALTER migration (`20260623_*_benchmark_attribution`) adds `benchmark_forward_return` +
  `excess_cost_adjusted_return` (rows) and `avg_benchmark_forward_return` + `avg_excess_cost_adjusted_return`
  (summary); `build_forecast_evaluations` loads NIFTY via `return_attribution.load_benchmark_history_for_attribution`
  + `attach_benchmark_forward_returns` and computes `excess = cost_adjusted_return - benchmark_forward_return`
  (asset-vs-market, after cost). `direction_hit` left unattributed by design (direction accuracy is
  vs a coin flip). attribution-only/research-only; fails open with telemetry if benchmark history is
  unavailable. 1 test added + schema-registry test updated.
- **Note (future):** the paper-portfolio/promotion gate still uses momentum lift only; gating TS
  promotion on benchmark-excess (now that the evaluator carries it) is a natural follow-on.
- **Why:** `ts_forecast_evaluator` has no benchmark schema; forecast quality is not
  comparable to passive exposure.
- **Files:** `advisory/ts_forecast_evaluator.py` (schema ~48-73, eval build).
- **Do:** Either attach benchmark forward returns + excess fields, OR, if
  forecasts are intentionally direction-only, document that explicitly in the
  summary contract and in `docs/`.
- **Guardrail:** research_only; TS forecasts stay experimental.
- **Validate:** Sample run shows excess fields or a documented contract reason.
- **Confidence:** verified (missing).

### [P2.6] Sector-concentration detector in promotions  ✅ DONE (revised to attribution-only per operator)
- **Operator steer (2026-06-23):** sector concentration is acceptable when a sector is genuinely
  performing (and liquid enough to exit on sector bad news), so a hard concentration *block* would
  be wrong. Two concepts were conflated: portfolio sector exposure (a risk-layer policy — see new
  P-card below) vs research-evidence concentration (false-generalization risk). Stockey already
  handles the latter the right way via benchmark-excess sector splits + (now) FDR.
- **Outcome:** Implemented **A (attribution-only, no block)**. Added
  `_sector_concentration_attribution` in `signal_quality_promotion.py`, sourced from the candidate's
  `fast_reliability_family.sector_diagnostics`, and attached it to each family promotion review and
  FDR-skip row as `sector_concentration` {dominant_sector, dominant_sector_matured_share,
  sector_concentrated, note}. When share >=
  `SIGNAL_QUALITY_PROMOTION_SECTOR_CONCENTRATION_NOTE_THRESHOLD` (0.60) the note routes the reviewer
  to the benchmark-excess sector split (so a strong sector becomes a sector rule, not a discarded
  one). `policy_effect=attribution_only_no_promotion_block` — never changes the recommendation.
  3 tests added.
- **Follow-on (separate layer): [P-RISK] portfolio sector-exposure + liquidity policy.** Capture the
  operator's liquid-strong-sector intent where it belongs: position-sizing sector caps + exit-ability
  + the existing de-risk/negative-pressure overlays for sector bad news. Risk/portfolio layer, not
  research promotion.
- **Why:** A "helpful" event-policy/negative-pressure group may derive all lift
  from one sector.
- **Files:** `advisory/event_policy_promotion.py`,
  `advisory/negative_pressure_evaluator.py`.
- **Do:** Add `_check_sector_concentration(group, max_pct=0.6)`; >0.6 adds a risk
  note, >0.8 downgrades to `needs_more_data`.
- **Guardrail:** research_only.
- **Validate:** Test: a group 70% in one sector flags the risk; 85% blocks.
- **Confidence:** verify-first.

### [P2.7] Deterministic promotion review for TS forecasts  ✅ DONE (already rigorous; benchmark-excess gate added)
- **Why:** `ts_forecast_promotion_check` appears to lack a deterministic review
  (gates) parallel to signal-quality promotion.
- **Outcome (2026-06-23):** NOT a stub — it already gates on evaluated_trades>=50,
  win_rate>=0.52, avg_cost_adjusted_return>=1%, lift_vs_momentum>=0.5%,
  exit_conflict_rate<=5%, >=10 dates, >=20 symbols, and persists
  `manual_config_review_only` / `broker_execution_allowed=false` reviews. The real gap:
  it beat *momentum* but not the *market*. Added NIFTY benchmark-excess to the TS paper
  portfolio (`ts_forecast_paper_portfolio.py`, follow-on ALTER migration +
  `_attach_benchmark_excess`, mirroring the P2.5 evaluator) and a
  `min_excess_cost_adjusted_return` gate (default 0.0, fail-closed on missing/None) in
  `ts_forecast_promotion_check.py`, so a forecast that merely rode the market can no longer
  be promoted. 4 tests added/updated. research_only; no auto-apply; no live consumer.
- **Note:** the P2.5 follow-on (gate TS promotion on benchmark-excess) is now closed here.

### [P2.7E] EPIC — TS forecast "production path" (now governed by [P-LLM-AUTH])
- **Operator intent (2026-06-23):** "make TS-forecast production ready and not just an
  independent/separate thing." Today TS forecasts have rigorous gated promotion but **no live
  consumer**.
- **Updated stance (2026-06-23):** per the operator's LLM-authority decision, eligibility approval
  is **LLM-resolved (no human manual review)**, and live authority is governed by the
  [P-LLM-AUTH] epic's default-OFF master flag + paper graduation — not a separate boundary. TS
  forecasts are one input the LLM decision policy (P-LLM-AUTH.2) can weigh; their own promotion
  gates (P2.7, incl. benchmark-excess) decide live-eligibility evidence.
- **Do (phased, each verify-first):**
  1. Build a `signal_refresh --from-ts-forecast` bridge (mirror `--from-causal-memory`) that emits
     review-only WATCH rows for model/horizons that PASSED the promotion gates (matured paper
     evidence, beats momentum AND benchmark-excess, breadth, low exit-conflict). Until the
     P-LLM-AUTH master flag is on, rows stay `broker_execution_allowed=false`,
     `full_advisory_required=true`.
  2. Those rows flow through the same gates as every other source; live authority only via
     P-LLM-AUTH.4 (master flag on + graduation passed).
  3. Operator Health: surface "promoted TS model/horizon active as input" with provenance
     (gates/thresholds passed, evidence).
  4. Keep the P-LLM-AUTH master flag (default off) governing any live effect.
- **Open decisions for the operator:** fold into [P-LLM-AUTH] open decisions (graduation
  thresholds, whether/when to enable live).

### [P2.8] Assert + audit research-only boundary at trusted-overlay load
- **Why:** `action_recommender` loads trusted signal-quality overlay rules; there
  is no explicit assertion that every loaded rule has
  `policy_auto_promotion_allowed=false`, and no audit when a rule transitions to
  trusted.
- **Files:** `advisory/action_recommender.py` (trusted-overlay loader ~3695-3707).
- **Do:** Assert/skip any loaded rule with `policy_auto_promotion_allowed=true` or
  `broker_execution_allowed=true` (with a visible fallback event), and audit-log
  any rule that becomes runtime-eligible.
- **Guardrail:** Fail-closed; a misconfigured rule must be ignored + logged, not
  trusted.
- **Validate:** Test: a rule with `policy_auto_promotion_allowed=true` is excluded
  and logged.
- **Confidence:** verify-first.

### [P2.9] Corporate-action parse-failure telemetry + adjustment sanity bounds
- **Why:** `adjusted_prices.py` returns `type="other"` / `price_factor=None` on
  non-standard or compound subjects with no telemetry, and never validates that
  cumulative factors are sane — a parse error can silently produce absurd
  adjustment factors. (Ingest side of P1.3.)
- **Files:** `data/nseindia/adjusted_prices.py` (parse ~13-87, cumulative build
  ~158-220), `data/nseindia/corporate_actions.py`.
- **Do:** When a split/bonus subject fails to yield a usable factor, record a
  `corporate_action_parse_ambiguous` fallback. After building cumulative factors,
  validate `0.01 ≤ factor ≤ 100` per symbol; outside that, record
  `adjusted_price_factor_suspicious` and clamp.
- **Guardrail:** Suspicious adjustments are visible, not silently applied.
- **Validate:** Tests for a hand-split subject (telemetry, no crash) and a
  malformed factor of 1000 (clamped + telemetry).
- **Confidence:** verify-first.

### [P2.10] macro_features main query must not fail silently
- **Why:** `macro_features.load_macro_daily` wraps `table_exists` in try/except
  but the main `sql_to_df` query has no fallback; a source outage raises raw to
  the caller with no telemetry.
- **Files:** `advisory/macro_features.py` (main load ~145-160).
- **Do:** Wrap the main query; on error record `macro_features_load_failed`
  fallback and re-raise with operator context (don't return empty silently).
- **Guardrail:** Source failure visible, never empty-as-success.
- **Validate:** Test: simulated query failure records the fallback and re-raises.
- **Confidence:** verify-first.

### [P2.11] Bounds check intraday features for sparse groups
- **Why:** `intraday_features` indexes `.iloc[-last_60_bars]` without checking
  group length; sparse symbols can silently pick the wrong bar.
- **Files:** `advisory/intraday_features.py` (~480-520).
- **Do:** Guard `len(group) >= last_60_bars`; otherwise record
  `intraday_sparse_bars` telemetry and fall back to session open.
- **Validate:** Test: a 30-bar group records telemetry and computes from open.
- **Confidence:** verify-first.

### [P2.12] Telemetry for partial company-master mapping
- **Why:** `fundamental_snapshot` calls `map_company_master_ids` and silently
  proceeds with the mapped subset; unmapped symbols vanish without record.
- **Files:** `advisory/fundamental_snapshot.py` (~85-100).
- **Do:** Log unmapped symbols as `company_master_mapping_partial` fallback and
  write `advisory_identity_issues` rows with a suggested action.
- **Guardrail:** Dropped candidates become visible identity issues.
- **Validate:** Test: 2 of 10 unmapped → telemetry + 2 identity rows.
- **Confidence:** verify-first.

### [P2.13] Record Dhan NSE→BSE identity fallback as degraded
- **Why:** `resolve_dhan_identity` falls back NSE→BSE but logs BSE success as a
  clean resolution with no degradation signal.
- **Files:** `data/dhanlive/dhan_db.py` (`resolve_dhan_identity` ~100),
  `advisory/identity_issues.py` (~646-705).
- **Do:** When BSE is used, record `dhan_identity_fallback_to_bse` telemetry and
  mark resolution context `bse_fallback`.
- **Validate:** Test: NSE miss + BSE hit → telemetry + `bse_fallback` context.
- **Confidence:** verify-first.

### [P2.14] Detect Dhan OHLCV continuity gaps
- **Why:** `sync_daily_ohlcv` computes `from_date = latest+1` with no check for a
  large gap after outages/holidays, so multi-day holes can pass unnoticed.
- **Files:** `data/dhanlive/ohlcv.py` (`sync_daily_ohlcv` ~200+).
- **Do:** After sync, if the trading-day gap (excluding weekends/holidays) exceeds
  ~5 days, record `dhan_ohlcv_gap_detected` with `gap_days`.
- **Validate:** Test: a 7-trading-day gap records telemetry; a weekend gap does not.
- **Confidence:** verify-first.

### [P2.15] Upstream data-lineage check in Operator Health
- **Why:** Operator Health surfaces fallback events but not upstream
  downloader/parser failures, so a failed download → skipped parser → stale
  advisory chain is invisible until a downstream fallback happens.
- **Files:** `advisory/operator_health.py` (add `check_upstream_lineage`).
- **Do:** Scan recent `logs/cron/all_downloaders*.log`, `all_parsers.log`,
  `all_external_workers.log` for non-zero exits in the last 24h, cross-reference
  with sync-state `error_text`, and report broken chains with a fix hint.
- **Guardrail:** Read-only; no behavior change.
- **Validate:** Test/fixture: a failed bhavcopy download surfaces a stale-chain
  warning.
- **Confidence:** verify-first.

---

## P3 - Frontend Trust Surfacing

Per the UI rule below, do these only because they expose backend truth or prevent
operator mistakes — not polish.

### [P3.1] Surface `why_not_executable` on action/recommendation rows
- **Why:** Recommendations can render as executable BUYs even when blocked by
  manual review, a market gate, or conflict resolution;
  `operator_action_disabled_reason` only reflects portfolio applicability.
- **Files:** `advisory/api/app.py` (action queue prep ~1789),
  `advisory/operator_portfolio.py` (recommendations ~259-296),
  `apps/operator-web/pages/recommendations.vue` (~65), `pages/index.vue`.
- **Do:** Add `why_not_executable: str | None` derived from
  `reason_contract_status != complete`, transition/market gates, and
  `advisory_action_conflicts` losers. Disable the button and show the reason.
- **Guardrail:** Never present review-only rows as approved executable BUYs.
- **Validate:** `npm --prefix apps/operator-web run typecheck`; a manual-review
  row shows the reason and a disabled action.
- **Confidence:** verify-first.

### [P3.2] Show feature-freshness blockers on action rows
- **Why:** Symbol feature blockers render only on the symbol page, so the home /
  recommendations pages can offer actions for symbols whose stage gates fail.
- **Files:** `advisory/api/app.py` (`/api/actions`),
  `apps/operator-web/pages/index.vue`, `pages/recommendations.vue`.
- **Do:** Add opt-in `include_symbol_blockers=true`; attach `feature_blockers`
  per row; disable the action and badge it when non-empty.
- **Validate:** typecheck; a symbol with failing gates shows a "gates blocked"
  badge and disabled action.
- **Confidence:** verify-first.

### [P3.3] Normalize `snapshot`/`snapshot_warning` across API response models
- **Why:** Response models are inconsistent (some omit `snapshot`), risking
  frontend crashes on `None`.
- **Files:** `advisory/api/app.py` (response models ~326-1118).
- **Do:** Add `snapshot: dict = Field(default_factory=dict)` and
  `snapshot_warning: dict | None = None` to the base model; inherit everywhere.
- **Validate:** typecheck; a response previously lacking `snapshot` now returns `{}`.
- **Confidence:** verify-first.

### [P3.4] Cache-first trace summary lookup
- **Why:** `/api/symbols/{symbol}/trace/summary` rebuilds from `load_symbol_trace`
  every call instead of reading `advisory_trace_summaries`.
- **Files:** `advisory/api/app.py` (~11013), `advisory/trace_summary_store.py`
  (~131-149).
- **Do:** Add `load_summary(entity_type, entity_key)`; serve cached if <1h old;
  rebuild+persist on miss.
- **Validate:** `python scripts/api_performance_report.py --limit 20`; repeated
  requests return cached results fast.
- **Confidence:** verify-first.

### [P3.5] Compact mode for heavy endpoints
- **Why:** `/api/execution/approvals`, `/api/actions`, `/api/events` return large
  payloads (full contracts/context) by default.
- **Files:** `advisory/api/app.py` (those handlers).
- **Do:** Add `compact=true`: omit `safety_contract`/`raw_context_json`, return
  `blocker_count` + `readiness_summary` counts instead of full arrays; add
  `total_count` to events.
- **Validate:** api_performance_report shows reduced payloads; typecheck.
- **Confidence:** verify-first.

### [P3.6] Freshness tiers (fresh / aging / stale / critical)
- **Why:** Freshness is binary; operators cannot tell "24h but ok" from "critical".
- **Files:** `advisory/operator_snapshot.py` (~26, 80-92), `advisory/api/app.py`
  (`_snapshot_payload`), `apps/operator-web/components/PayloadFreshnessStrip.vue`.
- **Do:** Add `warning_age_seconds`; compute a 4-tier state; render 4 pill colors.
- **Validate:** typecheck; an 18h snapshot (24h max) shows "aging".
- **Confidence:** verify-first.

### [P3.7] Contract-freshness check on execution approvals
- **Why:** Readiness checks pass off a contract loaded at request time with no
  staleness validation vs the execution row's evidence.
- **Files:** `advisory/api/app.py` (`_execution_approval_row` ~1972-2010).
- **Do:** Compute `contract_age_seconds`; if >1h add `contract_freshness_warning`;
  render a banner before approval.
- **Guardrail:** Reinforces that live approval needs fresh evidence.
- **Validate:** typecheck; a >1h contract shows the warning.
- **Confidence:** verify-first.

### [P3.8] Require + validate operator id; audit execution decisions
- **Why:** `operator_id` is recorded but not required/validated on approve/reject,
  weakening the live-execution audit trail.
- **Files:** `advisory/api/app.py` (decision endpoint ~2123-2220),
  `apps/operator-web/pages/execution-approvals.vue` (~77-82).
- **Do:** Validate `operator_id` against a known list (config/DB); 400 if
  missing/invalid; persist `operator_id + decided_at + rationale`; add a
  `/api/execution/decision-audit` read endpoint.
- **Validate:** typecheck; missing operator id is rejected; decisions appear in
  the audit endpoint.
- **Confidence:** verify-first.

---

## P4 - Performance And Operations (evidence-first)

1. Use `python scripts/api_performance_report.py --limit 20` and
   `logs/performance/latest_api_latency_probe.json`; add snapshots/caching/
   pagination/indexes only for proven slow paths (P3.4/P3.5 are the current
   evidence-backed candidates).
2. Keep bounded repair paths (`all_context_to_entry_repair.sh`, targeted
   technical refresh, rules-through-actions) so full advisory is not the only
   fast path.
3. Respect single-client external sources: serialize NSE/Dhan/Screener via the
   queue/workers; avoid parallel Chrome sessions that trigger blocking.
4. Continue hot/cold retention for trace, intraday, large text, and slowlog data.

## P5 - UI Only When It Improves Trust

Compatibility phrase for docs audit: Highest Priority: UI-First Operations.

The UI is good enough; do UI work only when it exposes critical backend truth or
reduces operator mistakes (action authority, staleness, source failure, technical
blockers, fix guidance). Avoid visual polish, ambiguous tags that hide backend
state, and anything that turns review-only evidence into apparently executable
recommendations.

Nuxt operator frontend replaces the old static HTML dashboard path. The active
operator surface is the Nuxt frontend backed by the FastAPI operator API.

## Completed Foundations (preserve)

- Dhan daily/intraday OHLCV + technical features; Screener.in production/ad hoc;
  Sharpely fundamentals/peers.
- Macro, regime, announcement, bhavcopy, exchange-event, theme/news context
  overlays with review-only authority; context-watchlist + signal-refresh path
  with no-portfolio/no-broker boundaries.
- Layered no-BUY diagnostics (breadth, macro stress, sector/symbol leadership,
  technical confirmation, source-family + exact-class reliability,
  benchmark-excess), context-to-entry funnel, and positive-context entry-blocker
  attribution.
- Technical setup explainability, technical threshold calibration (research-only),
  reason contracts + single consolidated action path,
  `reason_contract_status` producer + execution gate.
- Event-policy, adversarial review, company-memory, causal-event-memory,
  hypothesis, wait-signal, playbook scaffolding; identity validation; provenance
  graphs (source→memory→outcome, final-action→evidence).
- Research-only evaluators (signal-quality + splits, event-policy, causal-memory,
  adversarial-review, action-transition, context-watch, negative-pressure, TS
  forecast) with reviewed-diff (manual, disabled-by-default) promotion.
- Operator Health, fallback telemetry, sync state, source-degradation visibility;
  Nuxt/FastAPI operator frontend, compact API paths, trace summaries; cron
  wrappers, preflight, generated go-crond flow.

## How To Pick The Next Slice

1. P0 active failures (scripts/diagnostics/health/advisory/API breakage).
2. P1 decision correctness + authority safety.
3. P2 research rigor + source/failure visibility that affects operator trust.
4. P3 UI that surfaces critical backend state.
5. P4 performance fixes backed by current probe/slowlog evidence.

Every slice: one narrow code change, a focused test or script validation, docs/
todo update only if project state changes, `py_compile` of changed modules,
focused `pytest`, `python scripts/docs_state_audit.py --strict`, and
`git diff --check`.
