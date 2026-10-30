// stockey GET /api/themes and /api/themes/{id} (fundamentals/api/themes_queries.py)

export interface ThemeRow {
  theme_id: string
  name?: string
  area?: string
  definition?: string
  min_exposure?: number
  as_of_date: string
  status: 'shown' | 'next' | 'out' | 'late' | 'unranked'
  position: number | null
  kept_by_buffer: boolean
  n_ranked: number
  n_unquantified: number
  n_excluded: number
  strength: number | null
  strength_order: number | null
  late: boolean | null
  late_reasons: string | null
  policy_rank: number | null
  capacity_rank: number | null
  fundamentals_rank: number | null
  breadth_rank: number | null
  sales_yoy_pct: number | null
  margin_change_pp: number | null
  breadth_stage2_pct: number | null
  index_rs26_pct: number | null
  n_members: number | null
  reasoning: string | null
  conclusion: string | null
}

export interface ThemeStock {
  theme_id: string
  company_master_id: string
  ticker: string | null
  company_name: string | null
  status: 'ranked' | 'unquantified' | 'excluded'
  reasons: string | null
  share_pct: number | null
  share_source: string | null
  median_tv_cr: number | null
  exposure_rank: number | null
  trend_rank: number | null
  business_rank: number | null
  capacity_rank: number | null
  combined: number | null
  position: number | null
  rating: string | null
  on_watchlist: boolean
  stage: number | null
  weeks_in_stage: number | null
  tier: number | null
  rs26_pct: number | null
  from_52w_high_pct: number | null
  from_breakout_pct: number | null
  breakout_level: number | null
  price_week: string | null
  sales_yoy_pct: number | null
  sales_vs_trend_pp: number | null
  margin_change_pp: number | null
  roce_pct: number | null
  cwip_ratio: number | null
  reasoning: string | null
  conclusion: string | null
}

export interface ThemesResponse { as_of: string | null, themes: ThemeRow[], stocks: ThemeStock[] }

export interface ThemePolicy {
  name: string, kind: string, direction: 'support' | 'headwind', authority: string | null, announced: string | null
  outlay_cr: number | null, status: string, latest_figure: string | null, source_url: string
}

export interface ThemeDetail { theme: ThemeRow, stocks: ThemeStock[], policy: ThemePolicy[] }
