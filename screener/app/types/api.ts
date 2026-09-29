// Mirrors fundamentals/api/queries.py's response shapes in stockey. Kept as one
// hand-written file (no codegen) since the API is small and both sides are owned by
// the same person -- revisit with an OpenAPI-generated client if the surface grows.

// 'active' is the only status the API returns by default -- pass status=all (or a
// specific value) to /api/watchlist to see the others. See fundamentals/screens/
// watchlist_exit.py: nothing is ever deleted, this is a soft-status/visibility
// filter, not a destructive removal.
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

export interface UniverseCompany {
  ticker: string
  company_name: string
  cmp_rs: number | null
  p_e: number | null
  mar_cap_rscr: number | null
  div_yld_pct: number | null
  roce_pct: number | null
  qtr_sales_var_pct: number | null
  avg_vol_1mth: number | null
  group: UniverseGroup | null
  allowed_by: 'turnaround' | 'scaling_growth' | null
}

export type UniverseGroup = 'operating' | 'lender' | 'other_financial' | 'realty_holding' | 'unlabelled'

export interface UniverseExclusion {
  ticker: string
  group: UniverseGroup | null
  reasons: string[]
}

export interface UniverseResponse {
  query_text: string | null
  query_version: number | null
  run_date: string | null
  companies: UniverseCompany[]
  excluded: UniverseExclusion[]
}


export interface PortfolioResolvePayload {
  resolved_true: boolean
  resolution_notes?: string | null
  failure_attribution?: 'thesis_wrong' | 'thesis_right_market_hasnt_paid' | null
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

export interface UnsupportedRatingAgency {
  agency_name: string
  occurrence_count: number
  first_seen_at: string
  last_seen_at: string
  example_headline: string | null
  example_company_master_id: string | null
}

export interface Todos {
  rating_agencies: UnsupportedRatingAgency[]
}

// Strategy registry (2026-08-13) -- trigger_type in fundamentals_l3_alerts grouped
// as "strategies". No new backend concept: a company can satisfy several at once.
export interface Strategy {
  trigger_type: string
  company_count: number
  last_alert_date: string | null
}

export interface StrategyCompany {
  company_master_id: string
  alert_date: string | null
  reasoning: string | null
  origin: string
  company_name: string | null
  current_price: number | null
}

export interface StrategyDetail {
  trigger_type: string
  companies: StrategyCompany[]
}

// --- Data-platform health (stockey /api/data-health) -------------------------
// Mirrors scripts/data_completeness.py's Findings payload. Same implementation
// backs the nightly cron gate, so this page and the gate cannot disagree.
export interface DataHealthFinding {
  level: 'ok' | 'warn' | 'error'
  check: string
  message: string
  metrics: Record<string, string | number>
}

export interface DataHealthResponse {
  status: 'ok' | 'warn' | 'error'
  checked_at: string
  window_days: number
  error_count: number
  warn_count: number
  findings: DataHealthFinding[]
  cached: boolean
  age_seconds: number
}

// --- L5 position sizing (stockey /api/watchlist/{id}/sizing) ------------------
// PRD §12 todo #8. A CALCULATOR downstream of the L4 human gate, never a decision:
// the endpoint 404s unless the company already has an OPEN thesis. recommended_size_rs
// is min(equal_weight, adv_cap) -- ADV is a liquidity CEILING that can only pull the
// size down, never up.
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
// page teaches you to ignore it.
export type CollectorClassification = 'failing' | 'frozen' | 'orphaned' | 'ok'

export interface CollectorRow {
  source_name: string
  module: string
  scope_key: string | null
  status: string | null
  error_text: string | null
  last_success_at: string | null
  last_item_ts: string | null
  updated_at: string | null
  classification: CollectorClassification
}

export interface CollectorsResponse {
  counts: Partial<Record<CollectorClassification, number>>
  failing_count: number
  registry_module_count: number
  collectors: CollectorRow[]
}

// --- Scheduler health (stockey /api/scheduler-health) -------------------------
// Backed by scripts/is_cron_running.sh --json, not a Python reimplementation, so the
// browser and the operator's shell answer the same question from one definition.
export interface SchedulerFinding {
  level: 'ok' | 'warn' | 'error'
  section: string
  message: string
}

export interface SchedulerHealthResponse {
  status: 'ok' | 'warn' | 'error'
  error_count: number
  warn_count: number
  checked_at: string
  findings: SchedulerFinding[]
}

// --- Platform issues (stockey /api/platform-issues) ---------------------------
// Read-only. The nightly issue_digest rechecks and CLOSES rows as a side effect;
// this endpoint deliberately does not, so a page load never mutates pipeline state.
export interface IdentityIssue {
  issue_key: string
  issue_type: string
  symbol: string | null
  requested_exchange: string | null
  status: string
  first_seen_at: string | null
  last_seen_at: string | null
  error_text: string | null
}

export interface PlatformIssuesResponse {
  identity_issues: {
    open_count: number
    by_type: Record<string, number>
    issues: IdentityIssue[]
    limit: number
  }
  fallback_events: {
    window_hours: number
    active_count?: number
    // error_count/warn_count come straight from summarize_fallback_events, which merges
    // DB rows with the local spool -- do NOT derive warn as active - error, the spool
    // makes that coincidental rather than correct.
    error_count?: number
    warn_count?: number
    counts_by_type?: Record<string, number>
    counts_by_module?: Record<string, number>
    message?: string
  }
}

// --- Coverage report (stockey /api/coverage-report) ---------------------------
export interface CoverageTable {
  table_name: string
  category: string | null
  check_kind: string | null
  status: string
  rows: number | null
  symbols: number | null
  min_date: string | null
  max_date: string | null
  staleness_days: number | null
  detail: string | null
  report_date: string
}

export interface CoverageReportResponse {
  report_date: string | null
  not_ok_count: number
  tables: CoverageTable[]
  staleness_history: Record<string, { report_date: string; staleness_days: number | null }[]>
}

// --- Draft theses (stockey /api/drafts) ---------------------------------------
// LLM-GENERATED CANDIDATES, never decisions. PRD §1/§3.3: no LLM makes a capital
// call; L4 is the only gate capital passes through. A draft is a proposal a human
// accepts or rejects, which is why nothing here can create a thesis on its own.
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
}
