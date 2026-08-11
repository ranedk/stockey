// Mirrors fundamentals/api/queries.py's response shapes in stockey. Kept as one
// hand-written file (no codegen) since the API is small and both sides are owned by
// the same person -- revisit with an OpenAPI-generated client if the surface grows.

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

export interface WatchlistDetail {
  watchlist: WatchlistItem
  alerts: AlertItem[]
  l2_state: Record<string, unknown> | null
  technicals: Record<string, unknown> | null
  sector_context: Record<string, unknown> | null
  portfolio: PortfolioThesis[]
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
  sample_size_confidence: string | null
  n_companies_in_l1: number
  watched_companies: SectorWatchedCompany[]
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
