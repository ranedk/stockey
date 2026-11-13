// Shapes of stockey's fundamentals API (stockey/fundamentals/api/app.py) read by /watchlist,
// /portfolio and /investors. Copied from legacy/app/types/api.ts.
export type WatchlistStatus = 'active' | 'stale' | 'invalidated' | 'price_flagged' | 'no_thesis' | 'flawed' | 'faded'

export interface WatchlistItem {
  company_master_id: string
  first_seen_at: string | null
  first_seen_price: number | null
  last_alert_at: string | null
  alert_count: number
  narrative_text: string | null
  suggested_watch_until: string | null
  narrative_generated_at: string | null
  narrative_model?: string | null
  company_name: string | null
  current_price: number | null
  // 2026-08-26: current_price can be stale (frozen, no fresh bar in a while) or --
  // a narrower, sneakier case -- technically "fresh" but dated the SAME day as
  // first_seen_at, which reads as a real 0.0% move when it's actually just "no
  // comparison data yet" (the DBCORP bug). Both computed server-side
  // (fundamentals/api/queries.py's get_watchlist) so the frontend never
  // re-derives this date logic itself.
  price_data_stale: boolean
  current_price_as_of: string | null
  no_fresh_price_yet: boolean
  // 2026-08-29 (PRD §12 todo #6): fundamentals/screens/confluence_score.py's
  // mechanical axis count -- null when this company hasn't been scored yet
  // (distinct from a real 0, where every axis was unclear/contradicting).
  confluence_count: number | null
  contradicting_count: number | null
  evaluable_count: number | null
  // 2026-08-13: distinct trigger_type values this company currently satisfies --
  // was previously only visible on the detail page's signal_pointers.
  strategies: string[]
  status: WatchlistStatus
  status_reason: string | null
  // 2026-09-29 (reevaluation PRD step 6): membership on the story score. entry_basis is
  // 'story' (in the band, no flaw), 'event' (recent positive event, score above median),
  // 'story+event', 'none' (no longer qualifies), or null on rows the new rule has not seen.
  story_score: number | null
  primary_dimension: string | null
  entry_basis: string | null
  flaws: string | null
  checks: ScoresAndChecks
}

// 2026-10-19: the re-evaluation work's checks, per company (queries.load_scores_and_checks)
export interface ScoresAndChecks {
  in_portfolio: boolean
  score_7d_ago?: number | null
  score_change_7d?: number | null
  valuation_ratio?: number | null
  results?: {
    latest_period_end: string | null
    sales_yoy_pct: number | null
    profit_yoy_pct: number | null
    margin_change_pp: number | null
    one_off_share: number | null
  }
}

// GET /api/watchlist/summary -- equal-weight average return across the current
// watchlist bucket, since each company's own first_seen_at. Directional gut-check
// only: no time-weighting, no rebalancing, no correction for companies that have
// since left this status bucket. See fundamentals/api/queries.py's
// get_watchlist_return_summary for the exact exclusion rules.
export interface WatchlistReturnSummary {
  status: string | null
  net_return_pct: number | null
  included_count: number
  excluded_count: number
  total_count: number
}

export interface AlertItem {
  source: string
  news_id: string
  trigger_type: string
  origin: string
  alert_date: string | null
  reasoning: string | null
  status: string | null
  evidence_bundle: Record<string, unknown> | null
}

export interface PortfolioThesis {
  position_id: string
  ticker: string
  company_master_id: string
  kind: 'real' | 'shadow'
  status: 'open' | 'closed'
  entry_decision: 'accept' | 'reject' | null
  opened_at: string
  stop_pct: number | null
  prediction_text: string | null
  target_date: string | null
  invalidation_criteria: string | null
  metric_name: string | null
  metric_operator: string | null
  metric_threshold: number | null
  adjudicator_reason: string | null
  resolved_true: boolean | null
  resolution_date: string | null
  resolution_method: string | null
  failure_attribution: string | null
}

// Structured, queryable per-stock pointers (2026-08-13) -- fundamentals/screens/
// signal_pointers.py. Deliberately typed fields, not free-text tags: signal_type is
// one of promoter_holding / institutional_holding / institutional_first_entry /
// rating_action / investor_entry / sector_growth / strategy_satisfied.
export interface SignalPointer {
  signal_type: string
  label: string
  value: string | number | null
  direction: string | null
  as_of_date: string | null
  source: string | null
}

export interface WatchlistDetail {
  confluence?: ConfluenceDetail | null
  watchlist: WatchlistItem
  alerts: AlertItem[]
  l2_state: Record<string, unknown> | null
  technicals: Record<string, unknown> | null
  sector_context: Record<string, unknown> | null
  portfolio: PortfolioThesis[]
  signal_pointers: SignalPointer[]
  checks?: ScoresAndChecks
}

export interface SectorWatchedCompany {
  sector_code: string
  company_master_id: string
  alert_count: number
  narrative_snippet: string | null
}

export interface SectorInfo {
  sector_code: string
  sector_name: string | null
  // Medians over the same companies (2026-09-24): gross-block growth vs annual sales growth.
  capacity_growth_pct: number | null
  demand_growth_pct: number | null
  // Median per-company gap (capacity minus sales growth, points) -- what phase is read from.
  capacity_minus_demand_pts: number | null
  n_companies_with_demand_data: number | null
  // null for asset-light sectors (Financial Services, IT, Realty) and for no data.
  phase: string | null
  // High/medium/low growth or no_pattern (2026-08-13) -- a separate axis from phase:
  // phase reads capacity vs demand (cyclical positioning), this reads demand alone
  // (is underlying demand actually growing fast). See sector_cycle.py's classify_growth.
  growth_classification: 'high_growth' | 'medium_growth' | 'low_growth' | 'no_pattern' | null
  sample_size_confidence: string | null
  n_companies_in_l1: number
  watched_companies: SectorWatchedCompany[]
}

// Per-group hit rate: hit_rate reads null below fundamentals/screens/l4_thesis.py's
// MIN_SAMPLE_SIZE_FOR_BREAKDOWN (5) -- the count is still shown even then, so a
// thin breakdown reads as visibly thin rather than absent.
export interface HitRateGroup {
  hit_rate: number | null
  count: number
}

export interface PortfolioScoring {
  as_of_date: string
  total_forecasts: number
  open: number
  resolved: number
  hit_rate: number | null
  hit_rate_by_resolution_method: Record<string, HitRateGroup>
  hit_rate_by_entry_decision: Record<string, HitRateGroup>
  // Keyed by the count as a string ("0".."5"), or "none" for a company confluence_
  // score.py never scored. Every breakdown is split by resolution_method upstream so a
  // judged hit rate can never be blended into a mechanical one.
  hit_rate_by_confluence_count: Record<string, HitRateGroup>
  failure_attribution_breakdown: Record<string, number>
  time_to_confirmation_days: { median_days?: number, mean_days?: number, count?: number }
  // Accepted vs vetoed on PRICE, over closed positions (2026-09-24). Means read null below 5
  // closed positions; the count always shows.
  price_return_by_entry_decision: Record<string, PriceArm>
}

export interface PriceArm {
  closed: number
  mean_return_pct: number | null
  median_return_pct: number | null
  share_positive_pct: number | null
}

export interface InvestorClassification {
  investor_key: string
  investor_name_display: string
  llm_tier: 'marquee' | 'recognized' | 'unknown' | null
  llm_reasoning: string | null
  llm_model: string | null
  llm_classified_at: string | null
  override_tier: 'marquee' | 'recognized' | 'unknown' | null
  override_notes: string | null
  override_at: string | null
  first_seen_source: string | null
  first_seen_news_id: string | null
}

export type BindingConstraint = 'flat_allocation' | 'adv_liquidity_cap' | 'flat_allocation_no_adv_data'

export interface PositionSizing {
  company_master_id: string
  position_id: string
  capital_per_position_rs: number
  max_positions: number
  max_pct_of_adv: number
  avg_vol_1mth: number | null
  cmp_rs: number | null
  target_capital_rs: number
  adv_cap_rs: number | null
  recommended_size_rs: number
  binding_constraint: BindingConstraint
}

// --- Collector health (stockey /api/collectors) -------------------------------
// classification is reconciled against data/download_runner.py's live registry, NOT
// taken from `status` alone: advisory_sync_state is never pruned, so deleted/renamed
// modules keep a stale error row forever. Rendering those as failures is how a health
export interface DraftThesis {
  company_master_id: string
  prediction_text: string
  invalidation_criteria: string
  target_date: string | null
  confidence_score: number | null
  rationale: string | null
  source_alert_trigger_type: string | null
  model: string | null
  prompt_version: string | null
  generated_at: string | null
}

// --- Confluence (PRD §12) -----------------------------------------------------
// evaluable_count === confluence_count + contradicting_count. That identity is why a
// bare "1/4" badge misleads: the denominator is only the axes that COULD be judged, so
// everything in it that is not supportive is actively against. verdict is
// true = supports, false = contradicts, null = not enough data (never coerced).
export interface ConfluenceAxis {
  key: string
  description: string
  verdict: boolean | null
}

export interface ConfluenceDetail {
  run_date: string | null
  score_version: number | null
  confluence_count: number | null
  contradicting_count: number | null
  evaluable_count: number | null
  axes: ConfluenceAxis[]
}

// --- Ruleset portfolio (stockey /api/ruleset-portfolio) -----------------------
// kind='shadow' rows are candidates the LLM adjudicator REJECTED, tracked as if taken.
// They are the adjudicator's own scorecard -- a paired taken-vs-rejected comparison
// under one ruleset. Never blend them into real P&L.
export interface RulesetPosition {
  position_id: string
  ticker: string
  company_master_id: string
  ruleset_version: number
  kind: 'real' | 'shadow'
  status: 'open' | 'closed'
  opened_at: string
  entry_price: number | null
  stop_pct: number | null
  stop_basis: string | null
  confluence_count: number | null
  contradicting_count: number | null
  evaluable_count: number | null
  stage_at_entry: number | null
  adjudicator_model: string | null
  adjudicator_reason: string | null
  adjudicator_prompt_version: number | null
  entry_decision: 'accept' | 'reject' | null
  deferral_count: number
  close_reason: string | null
  closed_at: string | null
  exit_price: number | null
  last_price: number | null
  entry_price_date: string | null
  score_version: number | null
  prediction_text: string | null
  target_date: string | null
  invalidation_criteria: string | null
  target_date_basis: string | null
  position_size_rs: number | null
  adv_cap_rs: number | null
  sizing_basis: string | null
}

export interface RulesetPortfolioResponse {
  ruleset_version: number | null
  real_count: number
  shadow_count: number
  mode: 'live' | 'record-only'
  positions: RulesetPosition[]
  accepted_count: number
  rejected_count: number
  capital_committed_rs: number
  capital_per_position_rs: number
  book_used: number
  book_capacity: number
  legacy_open: number
}

// /api/paper-books -- the replayed paper books beside v3 (stockey portfolio_replay.py, LEDGER row 59)
export interface PaperBookPosition {
  ticker: string
  opened_on: string
  entry_price: number | null
  size_rs: number | null
  weight: number | null
  story_score_at_entry: number | null
  status: 'open' | 'closed'
  closed_on: string | null
  exit_price: number | null
  exit_reason: string | null
  realized_rs: number | null
  trim_count: number
  last_price: number | null
}

export interface PaperBookNavRow {
  as_of: string
  open_positions: number
  deployed_rs: number
  market_value_rs: number
  realized_rs: number
  unrealized_rs: number
}

export interface PaperBook {
  book: 'v3_replay' | 'uncapped' | 'v4'
  label: string
  description: string
  open_positions: number
  entered: number
  closed: number
  pnl_rs: number
  return_on_capital: number
  return_on_deployed: number | null
  series: PaperBookNavRow[]
  positions: PaperBookPosition[]
}

export interface PaperBooksResponse {
  books: PaperBook[]
  as_of: string | null
  since: string | null
}
