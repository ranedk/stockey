# Authoring Investor Hypotheses

A hypothesis is a **named, testable, event-driven playbook**: *when this kind of event happens, the
stock tends to move this way over this horizon — and here is how we confirm or kill it.* Hypotheses
are how your investing experience enters the system as the **alpha channel** (beta participation does
not need them; see `todo.md`). They are authored in `config/hypotheses.yaml`, imported into
`advisory_hypotheses`, matched against news/announcements, and — only after they earn it — promoted to
`trusted_overlay`, at which point they can ground an LLM trade decision.

This guide is the right way to author one and the pointers that decide whether it helps or just adds
noise.

## The shape of a good hypothesis

```yaml
hypotheses:
  - hypothesis_id: EARNINGS_BEAT_DRIFT_V1        # stable, UPPER_SNAKE, versioned (_V1)
    title: Post-earnings upside drift on a clear beat
    description: >
      A clear earnings beat (record/standout profit, margin expansion) tends to drift up over the
      following weeks as estimates are revised. Confirm it is a genuine beat, not just "results out".
    source: investor_interview                    # where the idea came from (audit trail)
    status: active_review                         # ALWAYS start here -- never self-promote
    trigger_scope: symbol                         # symbol (per-stock) or market (regime-level)
    trigger_patterns:
      keywords:
        include: [beat, record profit, highest ever, margin expansion, profit jump]
        exclude: [profit decline, loss, miss, weak]
    expected_effect:
      effect: increase_exposure                   # DIRECTION: long vs reduce -- see below
      market_direction: positive
      action_bias: buy_watch
    holding_window:
      horizons_days: [5, 10, 20]                  # over what windows the effect is expected
    decision_policy:
      min_terms: 1                                # min trigger terms that must appear to match
      confirmation_required: true
    validation_protocol:
      method: point_in_time_forward_return        # how it will be validated (vs NIFTY, after cost)
      benchmark: NIFTY
      costs_bps: 25
    promotion_rule: Promote to trusted_overlay only after a promotion-audit over >= 10 historical matches.
```

## Field reference

- **`hypothesis_id`** — stable unique key, UPPER_SNAKE, versioned (`_V1`). Bump the version rather than
  silently changing a live hypothesis.
- **`status`** — author at `active_review`. The engine normalizes authored labels (`validated` ->
  `active_review`, `production` -> `trusted_overlay`), so the only statuses that confer decision
  authority are **`trusted_overlay`** and `production`. A hypothesis does **nothing** to live decisions
  until promoted. Do not hand-set `trusted_overlay`; earn it (see Validation).
- **`trigger_scope`** — `symbol` for per-stock playbooks (order win, earnings beat); `market` for
  regime-level de-risk signals (austerity, macro stress). Market-scope hypotheses inform the regime,
  not a single name's alpha.
- **`trigger_patterns.keywords.include` / `.exclude`** — the matcher does **keyword overlap** against
  `subject + concise_summary_text` of news/announcement events. This is the single most important
  field to get right (see Pointers).
- **`expected_effect`** — must encode **direction**. This now decides whether the hypothesis can ground
  a BUY vs a SELL: `increase_exposure` / `market_direction: positive` / a buy-ish `action_bias` ->
  **long** (grounds BUY/BUY_MORE); `reduce_exposure` / `market_direction: negative` / a reduce/exit
  bias -> **reduce** (grounds SELL/PARTIAL_SELL). A direction the system can't read is treated as
  `unknown` and will not ground anything.
- **`holding_window.horizons_days`** — the windows the forward-return validation evaluates.
- **`decision_policy.min_terms`** — how many include-terms must appear for a match; raise it to tighten.
- **`validation_protocol`** — point-in-time forward return vs a benchmark, after cost. Keep
  `benchmark: NIFTY`, `costs_bps: 25` unless you have reason otherwise.

## Pointers — what separates a useful hypothesis from noise

1. **Match the REAL vocabulary, not idealized words.** The announcement `subject` is an exchange
   *category* ("Scheme of Arrangement", "Record Date", "Disclosure under SEBI Takeover Regulations");
   the substance is in `concise_summary_text`. Before trusting a keyword, measure how often it actually
   appears (a few `... WHERE concise_summary_text ILIKE '%term%'` counts). A keyword that never appears
   matches nothing; this is the most common authoring failure.

2. **Precision over recall.** A hypothesis that matches everything is worthless. Use specific include
   terms plus **exclude** terms to strip near-misses (e.g. an earnings-beat hypothesis must exclude
   `miss`, `loss`, `decline`). Fewer, cleaner matches beat many noisy ones — the promotion-audit needs
   the matches to actually be the event you mean.

3. **One mechanism per hypothesis.** Don't fold "order win" and "margin expansion" into one. Separate
   hypotheses are separately validatable and separately promotable.

4. **Direction must fit the mechanism.** Positive catalysts (order win, buyback, rating upgrade,
   capacity expansion, value-unlock) are `long`. Risk events (governance red flags, rating downgrade,
   earnings miss, insider selling) are `reduce`. Getting this wrong means the hypothesis grounds the
   wrong side — the system now blocks a direction-mismatched hypothesis, so it just won't fire.

5. **Stay point-in-time.** The trigger must be knowable *at* the event time, from the event text. No
   "the stock that later went up" framing, no future data.

6. **Event-driven only — not price anomalies.** Matching is news/announcement keyword based, so
   cross-sectional/price playbooks (momentum, 52-week-high breakout, low-volatility) will **not**
   trigger here; those belong in the technical/screener layers. Author hypotheses around *events*.

7. **Material vs routine.** High-signal events (order win, buyback, rating change, demerger, capacity)
   are worth a hypothesis; routine filings (dividend record dates, concall invites) are high-volume and
   low-signal — they will dominate matches and teach nothing. Prefer the material ones.

## Validation workflow (how a hypothesis earns authority)

```sh
# 1. Dry-run the import (no DB writes) to check normalization
python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml --dry-run

# 2. Import into advisory_hypotheses (status stays active_review)
python -m advisory.hypothesis_engine --import-config config/hypotheses.yaml

# 3. Scan news/announcements -> advisory_hypothesis_matches
python -m advisory.hypothesis_engine --run-scan

# 4. Promotion-audit: point-in-time forward excess vs NIFTY over historical matches (evidence, not auto-promote)
#    via the API: POST /api/hypotheses/{id}/promotion-audit
```

Promotion to `trusted_overlay` is a deliberate operator decision made **after** the promotion-audit
shows the pattern actually beat the benchmark after cost on enough matured matches. This is the same
"earn it from outcomes" discipline as decision graduation — your experience proposes the pattern, the
data decides whether it gets alpha authority. Until then it is review-only and grounds nothing.

## Anti-patterns

- Hand-setting `trusted_overlay`/`production` without a promotion-audit (skips the evidence gate).
- Vague includes (`growth`, `update`, `positive`) that match everything.
- Missing `exclude` terms, so a "beat" hypothesis catches "missed estimates".
- Direction left implicit, so the hypothesis can't ground either side.
- Encoding a price/momentum screen as a keyword hypothesis (it will never match).
