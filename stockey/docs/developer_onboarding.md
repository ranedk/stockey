# Developer Onboarding -- the decision system

A technical map of the LLM-decision-authority subsystem and the research feeds around it (hypotheses,
event classification, the factor model). For the investor-level concept see `docs/investor_overview.md`;
for the broader operator pipeline see `README.md`, `docs/operators_manual.md`, `docs/scripts.md`.

## Read first

1. `CLAUDE.md` -- the operating rules (authority boundaries, point-in-time discipline, what not to do).
2. `docs/investor_overview.md` -- the concept in plain language.
3. This file -- the module map and conventions.
4. `docs/llm_decision_authority.md` -- the authoritative architecture of the decision subsystem.

## The decision pipeline (and where each piece lives)

```
evidence packet (.4a)  ->  decision contract (.1)  ->  risk bounds (.2)  ->  decision policy (.4b)
   point-in-time,           grounded? which mode?      survivable size       LLM proposes; graded+sized
   per-dimension verdicts    (alpha/participation)      + ATR stop            review-only result
        |                                                                         |
        v                                                                         v
   persistence (.4c) advisory_llm_decisions  -->  outcome labeler  -->  monitor (.3)  -->  graduation
        |
        v
   broker bridge (.5)  ->  broker-capable action contract (gated; never submits)
```

| Module | What it does |
|---|---|
| `advisory/llm_evidence_packet.py` | `.4a` Build the point-in-time packet: per-dimension verdicts (direction/confidence/components) + hypothesis match. Pure assembly + thin defensive DB loaders. |
| `advisory/llm_decision_contract.py` | `.1` Deterministic grounding: complete? not contradicted? alpha vs participation? Verdict-based, not a magnitude score. |
| `advisory/llm_decision_risk_bounds.py` | `.2` Size a grounded decision within position/sector/total caps + ATR stop; regime size multiplier. |
| `advisory/llm_decision_policy.py` | `.4b` Prompt the LLM, grade via `.1`, size via `.2`, deterministic WATCH fallback. Review-only. |
| `advisory/llm_decision_store.py` | `.4c` Persist decisions (append-only migration) + the monitor adapter. |
| `advisory/llm_decision_monitor.py` | `.3` Systematic-error detector over matured outcomes; alerts, never blocks. |
| `advisory/llm_decision_outcome_labeler.py` | Realized after-cost benchmark-excess labels + `evaluate_policy_graduation`. |
| `advisory/llm_broker_bridge.py` | `.5` Gate + translate a graduated decision into a broker-capable contract (no submit). |
| `advisory/announcement_event_classifier.py` | Proximity event classifier -> event-class set (relevance gate for hypothesis matching). |
| `advisory/price_factors.py` | ~22 point-in-time factors + the cross-sectional confidence score; feeds the `fundamental` and timing dimensions. |
| `advisory/hypothesis_engine.py` | Investor-playbook lifecycle: import, match (news/announcements), promotion-audit. |

## Key conventions (non-negotiable -- they are why the system is trustworthy)

- **Point-in-time, no lookahead.** Every signal uses only data available on the as-of date. Forward
  returns anchor strictly after the as-of date.
- **Verdicts, not magnitude scores.** A calibration showed a per-dimension strength score had ~zero
  correlation with forward excess. Dimensions carry a verdict (supportive / neutral / contradicting +
  confidence) encoding logical consistency, which is sound regardless of a feature's predictive power.
- **Earn-it-from-outcomes.** A hypothesis, factor, or policy only grounds a live decision after a
  forward-excess audit / graduation. Reputation never substitutes for the audit.
- **Two default-off master flags.** `LLM_DIRECT_AUTHORITY_ENABLED` (LLM authority) and
  `STOCKEY_LIVE_TRADING_ENABLED` (global live trading) must BOTH be on for any capital movement, plus
  the execution engine's own gates. Nothing in this subsystem submits to a broker.
- **Append-only migrations.** Never edit an applied migration (changing its checksum breaks
  `apply_schema_migration`); add a follow-on `ALTER` migration.
- **Match real data vocabulary.** Repeatedly, the bug was code keyed on category tokens that do not
  occur in the real tables (status `validated` vs `trusted_overlay`; `RISK_ON` vs `NORMAL`; substring
  vs word-boundary matching). Verify column values against the live DB before keying on them.
- **A narrow test per behavioural change** in `tests/test_advisory_regression.py`.

## Data the decision system reads (point-in-time)

- `advisory_technical_daily` -- technical features + raw OHLCV (the factor model computes from the raw
  series since the pre-computed columns are sparse in history).
- `advisory_fundamentals_daily` -- growth / leverage / cash (the independent fundamental axis).
- `advisory_allocations` -- risk / event-class / setup state.
- `advisory_market_context_summary_daily` -- regime (NORMAL / WATCH / ELEVATED / STRESS).
- `advisory_context_watch_eval_summary` -- sector / exact-class reliability + benchmark-excess.
- `advisory_hypotheses` + `advisory_hypothesis_matches` -- investor playbooks (authority status
  `trusted_overlay`/`production`).
- `dhan_ohlcv_daily` -- deep+wide price history (used for factor validation; keyed by company_master_id).

## Run / validate

```sh
# tests (the decision subsystem is in the regression suite)
python -m pytest -q tests/test_advisory_regression.py -k "llm_decision or evidence_packet or price_factors or hypothesis"

# audits the agents run before committing
python scripts/docs_state_audit.py --strict
python scripts/env_example_audit.py --strict

# hypotheses: author in config/hypotheses.yaml, then
python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml --dry-run
python -m advisory.hypothesis_engine --lint-coverage           # flag dead keywords
python -m advisory.hypothesis_engine --run-scan --skip-actions # produce matches

# factor model validation (wide universe, reproducible)
python scripts/validate_price_factors.py
```

## Where the seams / open caveats are (so you do not trip on them)

- The cross-sectional `confidence` score needs the universe snapshot to percentile-rank, so it is a
  research/ranking tool; the per-symbol `fundamental` and timing VERDICTS are what ground decisions.
- Non-technical verdict signs (market/event/risk/reliability) and the bound thresholds are reasoned
  and vocabulary-correct but not all separately outcome-calibrated -- the data is thin in places.
- Hypothesis matching is keyword + event-class relevance; incidental same-phrase mentions in the wrong
  context are the residual a semantic (LLM) event layer would address (future).
- `todo.md` carries the live status, the constraint scorecard, and the remaining work.

## Related docs

- `docs/llm_decision_authority.md` -- the decision subsystem architecture (the index of this area).
- `docs/hypothesis_authoring.md` -- how to author + validate investor hypotheses.
- `docs/announcement_event_taxonomy.md` -- event-class taxonomy + extraction-field reference.
- `docs/price_factor_model.md` -- the multi-factor model, validation, and confidence score.
