// Response shapes for the paper-trading, strategies and paradigms pages. Imported explicitly
// (app/types is not auto-imported), so these names cannot collide with other pages' types.

// systrader GET /api/paper, /api/paper/{name} (systrade/internal/paperapi): the forward
// paper-trading record. A record, not a recommendation.
export interface PtSummary {
  book: string
  nav: number
  total_return: number
  day_return: number
  days: number
  holdings: number
  max_drawdown: number
  ann_cost: number
}

export interface PtNavPoint {
  date: string
  values: Record<string, number>
  rebalanced: boolean
}

export interface PtHolding {
  symbol: string
  weight: number
  shares: number | null // shares the Rs 1 crore paper account holds
  entry_date: string | null
  entry_price: number | null
  last_price: number | null
  return: number | null // since the position was first opened
}

export interface PtOrder {
  date: string
  symbol: string
  side: 'BUY' | 'SELL' | 'EXIT'
  from_weight: number
  to_weight: number
  fill_price: number | null
  from_shares: number | null
  to_shares: number | null
  value_rs: number | null
  cost_rs: number | null
}

export interface PtPending {
  computed_at: string
  based_on: string
  due: boolean
  days_to_due: number
  orders: PtOrder[] | null
}

export interface PtSpec {
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

export interface PtStrategy {
  name: string
  is_forward: boolean
  start: string
  days: number
  as_of: string
  summaries: PtSummary[] | null
  nav: PtNavPoint[] | null
  holdings: PtHolding[] | null
  winners: number
  losers: number
  holdings_as_of: string
  pending: PtPending | null
  last_orders: PtOrder[] | null
  last_orders_date: string
  spec: PtSpec
  reference?: PtStrategy | null
}

export interface PtQualColumn { strategy: string, variant: string, label: string }
export interface PtQualRow { symbol: string, marks: boolean[], count: number }
export interface PtQualMatrix { as_of: string, columns: PtQualColumn[] | null, rows: PtQualRow[] | null }

export interface PtOptionSession {
  date: string
  strike: number | null
  spot: number | null
  basket_bps: number | null
  members: Record<string, number | null>
  missing_legs: number
  cum_bps: number
}

export interface PtOptionTrack {
  name: string
  rule: string
  costs: string
  doc: string
  start: string
  judged: string
  check_bps: number
  count: number
  mean_bps: number | null
  positive: number
  as_of: string
  sessions: PtOptionSession[]
}

// stockey GET /api/paper-books: the portfolio ruleset's replayed paper books.
export interface PtBookPosition {
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

export interface PtBook {
  book: string
  label: string
  description: string
  open_positions: number
  entered: number
  closed: number
  pnl_rs: number
  return_on_capital: number
  return_on_deployed: number | null
  series: { as_of: string, open_positions: number, deployed_rs: number, market_value_rs: number, realized_rs: number, unrealized_rs: number }[]
  positions: PtBookPosition[]
}

export interface PtBooksResponse { books: PtBook[], as_of: string | null, since: string | null }

// stockey GET /api/strategies, /api/strategies/{trigger_type}
export interface StratSummary { trigger_type: string, company_count: number, last_alert_date: string | null }
export interface StratCompany {
  company_master_id: string
  alert_date: string | null
  reasoning: string | null
  origin: string
  company_name: string | null
  current_price: number | null
}
export interface StratDetail { trigger_type: string, companies: StratCompany[] }

// stockey GET /api/paradigms/tracks: one fundamental replica's forward track.
export interface ParadigmStockeyTrackData {
  track: string
  version: number
  start: string
  rebalance: string
  paradigm: string
  approximations: string
  spec: Record<string, unknown>
  as_of: string
  status: 'waiting' | 'live'
  holdings_as_of: string | null
  cash_weight: number | null
  periods: { start: string, end: string, names: number, return_pct: number | null }[]
  return_since_start_pct: number | null
  benchmark: { name: string, return_since_start_pct: number | null, names: number, from?: string }
  holdings: { symbol: string, rank: number | null, weight: number, entry_date: string, return_since_entry_pct: number | null }[]
}
