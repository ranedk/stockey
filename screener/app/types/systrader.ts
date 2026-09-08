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
}

export interface PaperOrder {
  date: string
  symbol: string
  side: 'BUY' | 'SELL' | 'EXIT'
  from_weight: number
  to_weight: number
  fill_price: number | null
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
  holdings_as_of: string
  pending: PaperPending
  last_orders: PaperOrder[] | null
  last_orders_date: string
  spec: PaperSpec
}

export interface PaperListResponse {
  strategies: PaperStrategy[]
}
