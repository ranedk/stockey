# LLM Decision Authority

How an LLM proposes trade decisions in Stockey, how the deterministic layers gate and size them, how
outcomes are monitored, and how (only behind two default-OFF master flags) a decision can become a
broker-capable action. This is the `[P-LLM-AUTH]` subsystem. See `todo.md` for status and the open
constraints, and `docs/hypothesis_authoring.md` for the hypothesis path.

## Principle

The operator decided (2026-06-23) the LLM **may** take trade decisions directly when it has reviewed
the complete, validated evidence and has strong, data-grounded reasons — never on one news / one
announcement / one indicator. The deterministic layers are guardrails, not the decision-maker: the
LLM does the synthesis; the code enforces that the decision is consistent, sized so no single call is
catastrophic, and auditable. Paper-first graduation is intentionally **not** used (too many
non-stationary dimensions); the safety floor is **deterministic risk bounds + live outcome
monitoring** instead.

Two independent master flags, both default **OFF**, must BOTH be on for any LLM capital movement:
`LLM_DIRECT_AUTHORITY_ENABLED` (LLM authority) and `STOCKEY_LIVE_TRADING_ENABLED` (global live
trading) — plus the execution engine's own gates (confirmation token, operator approval,
reconciliation, evidence checklist, Dhan identity hard-fail). Nothing in this subsystem submits to a
broker.

## Pipeline

```
evidence packet (.4a)  ->  decision contract (.1)  ->  risk bounds (.2)  ->  decision policy (.4b)
   point-in-time,           grounded? which mode?      survivable size       LLM proposes; graded+sized
   per-dimension verdicts    (alpha/participation)      + ATR stop            review-only result
        |                                                                         |
        v                                                                         v
   persistence (.4c) advisory_llm_decisions  -->  outcome labeler  -->  monitor (.3)  -->  graduation
        provenance-stamped, review-only          matured benchmark-     systematic-error    (earned, not
                                                  excess, point-in-time   detector (alert)    asserted)
        |
        v
   broker bridge (.5)  ->  broker-capable action contract  (ONLY if both master flags on + graduated +
   gated, no submit         the existing execution-safety contract passes). Gated-contract-only today.
```

Module map:

| Module | Role |
|---|---|
| `advisory/llm_evidence_packet.py` | `.4a` Build the point-in-time evidence packet (7 dimensions + hypothesis_match), each a structured verdict. |
| `advisory/llm_decision_contract.py` | `.1` Deterministic grounding: complete? not contradicted? which mode (alpha/participation)? |
| `advisory/llm_decision_risk_bounds.py` | `.2` Size a grounded decision within position/sector/total caps + ATR stop; regime size multiplier. |
| `advisory/llm_decision_policy.py` | `.4b` Prompt the LLM, grade the proposal through .1, size with .2, deterministic WATCH fallback. |
| `advisory/llm_decision_store.py` | `.4c` Persist decisions (`advisory_llm_decisions`), provenance-stamped; monitor adapter. |
| `advisory/llm_decision_monitor.py` | `.3` Systematic-error detector over matured outcomes; alerts, never blocks. |
| `advisory/llm_decision_outcome_labeler.py` | Realized after-cost benchmark-excess labels + `evaluate_policy_graduation`. |
| `advisory/llm_broker_bridge.py` | `.5` Gate + translate a graduated decision into a broker-capable action contract (no submit). |
| `advisory/announcement_event_classifier.py` | Proximity event classifier (relevance gate for hypothesis matching). |
| `advisory/price_factors.py` | Point-in-time price/fundamental factor signals + the confidence score (feeds the `fundamental` and timing dimensions). |

## Evidence packet (.4a) — verdicts, not scores

Each of the 7 dimensions carries a structured **verdict**: `direction` (supportive / neutral /
contradicting) + `confidence` + `components`, NOT a single magnitude score. (A calibration over 3.6k
matured stock-days showed a per-dimension strength score had ~zero correlation with forward excess;
verdicts instead encode *logical consistency* with the thesis, which is sound regardless of a
feature's predictive power.) `strength` is kept as a descriptive field for the LLM/audit only.

Dimension **roles** (the dimensions are not a flat vote):

- **Thesis (the filter):** `event_provenance`, `fundamental`, `sector_reliability`,
  `exact_class_reliability`, and a matched **hypothesis**. Non-technical evidence is *why* a name is
  interesting. `fundamental` (earnings growth / leverage / cash, point-in-time from
  `advisory_fundamentals_daily`) is the genuinely INDEPENDENT axis -- the price/factor validation
  showed confluence beats single factors precisely when technical AND fundamental agree (earnings
  growth is the strongest single factor). See `docs/price_factor_model.md`.
- **Timing:** `technical_confirmation`. Technicals time the entry (room to grow vs played-out /
  lagging), they are not the thesis. Only a real downtrend or material underperformance contradicts;
  a mild lag in an uptrend is neutral (quiet basing = room).
- **Regime gate:** `market_context`. A conviction/size factor, not a blanket veto.
- **Mode classifier:** `benchmark_excess`. Classifies alpha vs beta; it does not block.
- **Risk guardrail:** `risk`. A confident contradiction (broken risk profile) vetoes.

Verdict signs are calibrated for technical; for the others they are reasoned + vocabulary-correct but
not yet outcome-calibrated (the source tables are too sparse — see Constraints). The verdict
vocabularies match the REAL stored values (`macro_risk_state` ∈ NORMAL/WATCH/ELEVATED/STRESS;
`setup_effect` ∈ strengthens/weakens/neutral; hypothesis authority status = `trusted_overlay`).

## Grounding (.1) — two modes, contradiction veto

A directional decision is **data-grounded** when the packet is COMPLETE on its core dimensions
(`technical_confirmation`, `market_context` — a missing enriching dimension degrades confidence but
does not block) and it grounds in one of two modes, with no confident contradiction in any required
dimension:

- **ALPHA** — a CORROBORATED non-technical thesis: >= 2 confidently-supportive INDEPENDENT
  non-technical dimensions (event / fundamental / sector / exact-class / benchmark -- e.g. fundamental
  + event, or event + reliability) OR a valid hypothesis; plus technical timing not contradicting and
  support not market-beta-only. A lone signal is not enough. Beating the market.
- **PARTICIPATION (beta)** — no thesis required, but a supportive LIQUID trend in a constructive
  regime. Labeled beta so the system can capture a rally instead of sitting in cash; it does not
  pretend to be alpha.

Guards that apply across both:

- **Contradiction veto** — a confident `contradicting` verdict in any required dimension blocks both
  modes (a broken risk profile, a weakening event, an underperforming tape).
- **Regime** — only extreme `STRESS` hard-blocks; a de-risk headwind sizes alpha down (×0.5) and
  forbids participation, but does not veto a real idiosyncratic thesis (no broad regime label as the
  sole gate).
- **Beta guard** — classifies the mode; beta-only support disqualifies alpha but still allows
  participation.
- **Hypothesis** — a `trusted_overlay`/`production` hypothesis whose conditions are met grounds a
  decision ONLY when its expected direction matches the action (a de-risk playbook grounds a SELL,
  not a BUY). If trusted hypotheses disagree on direction for a symbol, authority is **withheld** and
  the conflict recorded (it does not silently pick the higher score).

`meets_data_grounding_for_live` = grounds in either mode over a complete packet. This is the DATA bar
— separate from the master flag and from graduation.

## Risk bounds (.2) and policy (.4b)

The decision policy prompts the LLM for a structured proposal (action, conviction, cited dimensions,
hypothesis claim, rationale), then grades it through `.1` and sizes it with `.2`. The LLM only
proposes; the deterministic layers decide whether it is grounded and how large it may be (position /
sector / total-exposure caps, conviction-scaled, ATR stop, regime size multiplier). Any LLM failure
degrades to a deterministic WATCH (never a default BUY) with fallback telemetry. Output is review-only
(`broker_execution_allowed=False`) and provenance-stamped (prompt id/version/schema/model + as-of).

## Outcome monitoring (.3) + graduation

The labeler computes each matured decision's realized after-cost benchmark-excess point-in-time
(entry = first close strictly after the as-of date; no lookahead). The monitor flags systematic error
patterns (low excess-hit-rate, negative mean excess, systematic beta tilt, per-event-class or
per-sufficiency-path underperformance) — it ALERTS, it never blocks. `evaluate_policy_graduation` is
the only legitimate source of `graduation_passed`: a policy graduates only with enough matured
outcomes, positive mean after-cost excess, and zero systematic-error alerts. Reputation is never
enough; outcomes earn it.

## Broker bridge (.5)

`evaluate_llm_broker_authority` grants `broker_execution_allowed=True` ONLY when ALL hold: the master
flag is on (default OFF), the decision is data-grounded (.1), graduated (.3), sized (.2), `llm_status`
ok, and the action is broker-capable. `build_broker_action_contract` then translates it into the
`advisory_action_recommendations`-shaped contract the existing execution engine consumes — it NEVER
calls the broker and does NOT auto-write the execution table (gated-contract-only). Any real
submission still requires `STOCKEY_LIVE_TRADING_ENABLED` + the execution engine's full safety contract.

## Price/factor model (the fundamental + technical axes)

`advisory/price_factors.py` computes ~22 point-in-time factors (momentum/trend/volume/volatility/
liquidity + fundamental growth/leverage/cash), grouped by INDEPENDENT component, and a cross-sectional
`confidence` score that averages percentile ranks within each component (so correlated trend factors
count once) weighted robustly. Validated on 178 securities (`scripts/validate_price_factors.py`):
fundamentals are the strongest single factor (earnings-growth IC ~+0.10), and confluence beats single
factors ONLY across independent axes (technical AND fundamental). That finding is wired into the
decision packet as the **`fundamental` dimension** (an independent thesis axis) -- a fundamentally
strong name + a supporting event/reliability is a corroborated alpha thesis, exactly the technical+
fundamental confluence the validation rewards. Full design + findings: `docs/price_factor_model.md`.

## Related documents

- `docs/price_factor_model.md` -- the multi-factor model (factors, validation, confidence score).
- `docs/hypothesis_authoring.md` -- how to author investor hypotheses (the operator alpha channel).
- `docs/announcement_event_taxonomy.md` -- the event-class taxonomy + extraction-field reference.

## Hypotheses

Hypotheses are the operator-authored alpha channel (see `docs/hypothesis_authoring.md`). Authored in
`config/hypotheses.yaml` at `active_review`; matched against news/announcements; promoted to
`trusted_overlay` only after a promotion-audit shows after-cost excess vs NIFTY on real matches. Only
then do they ground an LLM alpha decision. Without hypotheses the system still does beta participation
in a constructive tape; hypotheses are what turn "ride the index" into "beat it".

## Known constraints (see `todo.md` for status)

1. Matching is news-keyword-only — price/factor anomalies (momentum, 52w-high, low-vol, value,
   quality) cannot be expressed as hypotheses; they belong in the technical/screener layer.
2. Direction conflicts among trusted hypotheses now withhold authority (handled). 
3. Market-scope hypotheses no longer leak into a symbol's grounding (handled); the matcher still
   attaches a symbol to market-scope matches in the table (residual, harmless to grounding).
4. Generic-keyword noise: largely handled -- matching is now word-boundary phrase matching with
   `exclude_keywords` applied (so "war" no longer matches "award"; cut raw matches ~28%, worst
   offenders 80-98%). Residual: genuinely high-frequency terms (e.g. "acquisition") and incidental
   real-phrase mentions still match; relevance/min_terms and a semantic event-class layer are future.
5. Brittle keyword coverage: handled by a coverage linter (`hypothesis_coverage_report` /
   `--lint-coverage`) that flags zero/low-coverage hypotheses at authoring time.
6. Non-technical verdict signs and the bound thresholds are reasoned, not yet outcome-calibrated.

## Safety guarantees (kept regardless of who decides)

- Every LLM decision persists a typed evidence/provenance + grounding contract.
- Behavior is point-in-time (no future data / lookahead).
- A master enable flag gates any live LLM->broker authority and DEFAULTS OFF.
- Every live decision is bounded by deterministic position/exposure/stop limits.
- Live decisions + outcomes + provenance are monitored to catch systematic error (alert, never block).
