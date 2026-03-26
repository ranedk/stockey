# Investment Advisory TODO

This file tracks the next advisory redesign as of 2026-03-26.

The current system already has:

- screener normalization into `advisory_screener_constituents`
- macro snapshot and base regime classification
- technical and fundamental snapshots
- rule engine, watchlist, announcement watch, and ET RSS watch
- LLM event evaluation
- risk, portfolio, lifecycle, and trace/dashboard tools

The next weakness is not missing infrastructure. It is setup activation logic:

- setups are still too tightly tied to single screeners
- regime handling is still too flat for temporary shock contexts
- news context is not first-class in setup activation
- traces do not clearly show which screener pool and overlay drove candidate generation

## Objective

Add a stable two-layer activation model:

- one base regime from the existing `advisory_market_regime`
- one lightweight daily news overlay

Then use that pair to:

- activate or suppress setups
- choose multiple screeners per setup
- add or remove screeners under overlays
- improve candidate generation without changing data sources

## Product Model

### Base regime

Keep the existing regime model:

- `BULL_RISK_ON`
- `BULL_NARROW`
- `STABLE`
- `STABLE_BUT_TARIFF_RISING`
- `RISK_OFF`
- `SHOCK`

### News overlay

Add a second daily layer:

- `NONE`
- `GEOPOLITICAL_RISK`
- `OIL_SHOCK`
- `TARIFF_PRESSURE`
- `SECTOR_POLICY_SHOCK`
- `EVENT_CLUSTER`

This is not a replacement for base regime. It is a context overlay.

## Scope

In scope:

- multiple screeners per setup
- overlay-aware setup activation
- overlay-aware screener expansion or suppression
- diagnostics for active screeners and overlays
- provenance fields on candidates and watchlists

Out of scope:

- new vendors
- broker/feed expansion
- macro engine rewrite
- full market-wide news-first discovery
- turning every headline into a new regime

## Current Reality

Already implemented from the previous redesign:

- ranked setup scoring
- reduced hard rejects
- watch states and entry-zone metadata
- event taxonomy and state-transition hints
- aggregated event effect handling in watch, risk, and portfolio
- improved symbol/setup trace visibility

Main gaps now:

- config still defaults to a single screener mental model
- setups do not read a market overlay
- rule engine does not expose active screener provenance cleanly
- dashboard and setup trace do not show overlay-driven activation

## Delivery Plan

### Phase 1: Multi-Screener Setup Mapping

Goal:

- let setups consume multiple screeners deterministically

Tasks:

1. Update [`config/advisory_setups.yaml`](/home/rane/code/stockey/config/advisory_setups.yaml)
   - replace single-screener assumptions with:
     - `screeners`
     - `screener_mode`
     - `overlay_screeners`
     - `allowed_overlays`
     - `blocked_overlays`
2. Update [`advisory/setup_registry.py`](/home/rane/code/stockey/advisory/setup_registry.py)
   - normalize old and new setup config shapes
   - keep backward compatibility where practical
3. Update [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
   - resolve active screeners per setup
   - support `union` and `intersection`
   - preserve source screener provenance

Done when:

- at least one setup runs against more than one screener
- trace output shows which screener pool was active
- existing setups do not break

### Phase 2: Add Market News Overlay

Goal:

- classify one daily overlay from ET RSS and existing regime context

Tasks:

1. Add [`advisory/news_overlay_engine.py`](/home/rane/code/stockey/advisory/news_overlay_engine.py)
   - read recent ET RSS / matched news context
   - combine with existing regime flags
   - write:
     - `asof_date`
     - `base_regime`
     - `overlay_name`
     - `overlay_intensity`
     - `overlay_reason`
     - `source_count`
2. Keep [`advisory/regime_engine.py`](/home/rane/code/stockey/advisory/regime_engine.py) unchanged in principle
   - it remains the stable base layer

Done when:

- one overlay row is available per date
- `NONE` is valid
- dashboard and traces can display the overlay

### Phase 3: Overlay-Aware Setup Activation

Goal:

- use base regime plus overlay to decide whether setups run and which screeners they use

Tasks:

1. Extend [`advisory/rule_engine.py`](/home/rane/code/stockey/advisory/rule_engine.py)
   - apply:
     - `allowed_regimes`
     - `blocked_regimes`
     - `allowed_overlays`
     - `blocked_overlays`
   - reject with:
     - `regime_not_allowed`
     - `overlay_not_allowed`
   - add overlay-driven screener expansion/removal
2. Preserve candidate metadata:
   - `source_screener_slug`
   - `source_screener_list`
   - `base_regime`
   - `news_overlay`

Done when:

- setup activation clearly depends on both layers
- overlay can add screeners without code changes
- inclusion and rejection reasons are traceable

### Phase 4: Diagnostics and Downstream Provenance

Goal:

- show setup activation path clearly

Tasks:

1. Update [`advisory/watchlist_builder.py`](/home/rane/code/stockey/advisory/watchlist_builder.py)
   - preserve screener and overlay provenance
2. Update [`advisory/setup_trace.py`](/home/rane/code/stockey/advisory/setup_trace.py)
   - show:
     - base regime
     - active overlay
     - overlay reason
     - active screeners
     - candidate counts by screener
3. Update [`advisory/dashboard.py`](/home/rane/code/stockey/advisory/dashboard.py)
   - expose:
     - regime
     - overlay
     - active screeners
     - top rejection reason

Done when:

- one command can explain why a setup ran
- one command can explain which screener pool drove candidate generation

## Minimum Schema Changes

### New table

`advisory_market_overlay_daily`

- `asof_date`
- `base_regime`
- `overlay_name`
- `overlay_intensity`
- `overlay_reason`
- `source_count`
- `load_ts`

### `advisory_candidates`

- `source_screener_slug`
- `source_screener_list`
- `base_regime`
- `news_overlay`

### `advisory_watchlist`

- `source_screener_slug`
- `source_screener_list`
- `base_regime`
- `news_overlay`

## Operating Workflow

1. Run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.news_overlay_engine --dry-run
```

2. Run:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.rule_engine --dry-run
```

3. Inspect:

```sh
/home/rane/code/stockey/.xstockey/bin/python -m advisory.setup_trace <SETUP_ID> --format text
```

4. Check:

- active regime
- active overlay
- active screeners
- candidate counts by screener
- top rejection reasons

## Blunt Rule

Do not build a new “regime” every time the market narrative changes.

Build:

- one stable base regime
- one lightweight market/news overlay
- one flexible multi-screener setup mapping layer

That is the right level of reactivity for this stack.
