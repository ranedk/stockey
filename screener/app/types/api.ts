// Mirrors fundamentals/api/queries.py's response shapes in stockey. Kept as one
// hand-written file (no codegen) since the API is small and both sides are owned by
// the same person -- revisit with an OpenAPI-generated client if the surface grows.

// 'active' is the only status the API returns by default -- pass status=all (or a
// specific value) to /api/watchlist to see the others. See fundamentals/screens/
// watchlist_exit.py: nothing is ever deleted, this is a soft-status/visibility
// filter, not a destructive removal.
export type WatchlistStatus = 'active' | 'stale' | 'invalidated' | 'price_flagged'

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
  // 2026-08-13: distinct trigger_type values this company currently satisfies --
  // was previously only visible on the detail page's signal_pointers.
  strategies: string[]
  status: WatchlistStatus
  status_reason: string | null
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
  thesis_id: string
  company_master_id: string
  origin_tag: string
  prediction_text: string
  target_date: string
  invalidation_criteria: string
  created_date: string
  status: string
  resolved_true: boolean | null
  resolution_date: string | null
  resolution_notes: string | null
  failure_attribution: string | null
  source_alert_source: string | null
  source_alert_news_id: string | null
  source_alert_trigger_type: string | null
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
  capacity_growth_pct: number | null
  demand_growth_pct: number | null
  phase: string | null
  // High/medium/low growth or no_pattern (2026-08-13) -- a separate axis from phase:
  // phase reads capacity vs demand (cyclical positioning), this reads demand alone
  // (is underlying demand actually growing fast). See sector_cycle.py's classify_growth.
  growth_classification: 'high_growth' | 'medium_growth' | 'low_growth' | 'no_pattern' | null
  sample_size_confidence: string | null
  n_companies_in_l1: number
  watched_companies: SectorWatchedCompany[]
}

export interface PortfolioScoring {
  as_of_date: string
  total_theses: number
  open: number
  resolved: number
  hit_rate: number | null
  failure_attribution_breakdown: Record<string, number>
  time_to_confirmation_days: { median_days?: number; mean_days?: number; count?: number }
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
}

export interface UniverseResponse {
  query_text: string
  query_version: number
  run_date: string | null
  companies: UniverseCompany[]
}

export interface PortfolioCreatePayload {
  company_master_id: string
  prediction_text: string
  target_date: string
  invalidation_criteria: string
  origin_tag: 'systematic_screen' | 'ad_hoc'
  source_alert_source?: string | null
  source_alert_news_id?: string | null
  source_alert_trigger_type?: string | null
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
