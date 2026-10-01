// Mirrors systrade/internal/stageapi's response shapes -- systrader's own API
// (cmd/api), a separate backend from stockey's fundamentals API (see api.ts).

// GET /api/stage -- Stan Weinstein's 4-stage weekly regime classifier
// (systrade/internal/stage), REPORTING ONLY: not a signal, not wired into any
// sizing/trading decision. 1=Basing, 2=Advancing, 3=Topping, 4=Declining.
export interface StageRow {
  ticker: string
  stage: 1 | 2 | 3 | 4
  stage_label: string
  close: number
  ma30: number
  slope_pct: number | null
  volume_ratio: number | null
  as_of: string
}

export interface StageListResponse {
  rows: StageRow[]
  total_universe: number
  included_count: number
  excluded_stale: number
  excluded_short: number
  counts_by_stage: Record<string, number>
  as_of_now: string
}

// GET /api/paper, GET /api/paper/{name} -- the forward paper-trading record
// (systrade/internal/paperapi). A RECORD, not a recommendation: the order sheet
// is what the frozen strategy would place, tracked so that evidence
// accumulates from data nobody chose. See docs/RESEARCH_PROTOCOL.md.
export interface PaperSummary {
  book: string
  nav: number
  total_return: number
  day_return: number
  days: number
  holdings: number
  max_drawdown: number
  ann_cost: number
}

export interface PaperNavPoint {
  date: string
  values: Record<string, number>
  rebalanced: boolean
}

export interface PaperHolding {
  symbol: string
  weight: number
  // Shares the Rs 1 crore paper account holds of the name.
  shares: number | null
  entry_date: string | null
  entry_price: number | null
  last_price: number | null
  // Gain since the position was FIRST opened -- top-ups and trims do not reset it.
  return: number | null
}

export interface PaperOrder {
  date: string
  symbol: string
  side: 'BUY' | 'SELL' | 'EXIT'
  from_weight: number
  to_weight: number
  fill_price: number | null
  // The Rs 1 crore account's side of the order: whole shares, rupees, Dhan charges.
  // null on weight-book orders.
  from_shares: number | null
  to_shares: number | null
  value_rs: number | null
  cost_rs: number | null
}

export interface PaperPending {
  computed_at: string
  based_on: string
  due: boolean
  days_to_due: number
  orders: PaperOrder[] | null
}

export interface PaperSpec {
  universe: string
  signal: string
  selection: string
  weighting: string
  rebalance: string
  execution: string
  costs: string
  min_turnover_cr: number
  doc: string
}

export interface PaperStrategy {
  name: string
  is_forward: boolean
  start: string
  days: number
  as_of: string
  summaries: PaperSummary[] | null
  nav: PaperNavPoint[] | null
  holdings: PaperHolding[] | null
  winners: number
  losers: number
  holdings_as_of: string
  pending: PaperPending
  last_orders: PaperOrder[] | null
  last_orders_date: string
  spec: PaperSpec
  // The same rules run over history. Nested under the strategy rather than
  // listed beside it: it is a backtest, not a second strategy.
  reference?: PaperStrategy | null
}

// One strategy variant a stock can qualify for. A single-signal strategy has
// one variant (its signal); a book-blend has one per variant.
export interface QualColumn {
  strategy: string
  variant: string
  label: string
}

// A stock and which columns it qualifies for, in columns order.
export interface QualRow {
  symbol: string
  marks: boolean[]
  count: number
}

// Every stock in the top fifth of any tracked strategy's variant on its
// latest decision day (GET /api/paper/qualifiers).
export interface QualMatrix {
  as_of: string
  columns: QualColumn[] | null
  rows: QualRow[] | null
}

export interface PaperListResponse {
  strategies: PaperStrategy[]
}

// GET /api/rotation (systrade/docs/SECTOR_ROTATION_PRD.md): industry rotation, relative
// strength and stages on a weekly clock. Reporting only.
export interface RotationMarket {
  stage: number
  stage_label: string
  breadth_pct: number
  eligible_count: number
  return_26w_pct: number | null
}

export interface RotationIndustry {
  code: string
  name: string
  sector_code: string
  sector_name: string
  members: number
  eligible_members: number
  rs26_pct: number | null
  rank: number // 0 = not ranked (fewer than 5 liquid members)
  rank_4w: number
  rank_13w: number
  rank_history: number[]
  stage: number
  stage2_pct: number
  leading: boolean
  weeks_leading: number
}

export interface RotationStock {
  symbol: string
  industry_code: string
  close: number
  stage: number
  weeks_in_stage2: number
  rs26_pct: number | null
  rank_in_industry: number
  above_ma30_pct: number | null
  slope_pct: number | null
  volume_ratio: number | null
  eligible: boolean
  candidate: boolean
}

export interface RotationQuantiles { p50: number | null; p75: number | null; p90: number | null; mean: number | null }

export interface RotationStats {
  stage2_weeks: RotationQuantiles
  stage2_runs: number
  leadership_weeks: RotationQuantiles
  leadership_runs: number
  top_fifth_weeks: RotationQuantiles
  new_leaders_per_quarter: number | null
  from: string
}

export interface RotationResponse {
  as_of: string
  market: RotationMarket
  industries: RotationIndustry[]
  stocks: RotationStock[] | null
  stats: RotationStats
}

export interface RotationIndustryResponse {
  as_of: string
  market: RotationMarket
  industry: RotationIndustry
  stocks: RotationStock[] | null
}

// stockey GET /api/story-scores: the fundamental filter shown beside price candidates.
export interface StoryScoreRow {
  symbol: string
  company_master_id: string
  story_score: number | null
  primary_dimension: string | null
  flaws: string | null
  in_band: boolean | null
  watchlist_status: string | null
}
