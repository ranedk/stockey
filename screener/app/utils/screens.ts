// Ready-made screens. Each is a plain list of conditions (field:op:value), shown to the user so
// they can see and edit exactly what it does. None is a recommendation.
export type Cond = { field: string, op: string, value: string }

export const PRESETS: { id: string, label: string, icon: string, desc: string, conds: Cond[], sort?: string }[] = [
  { id: 'quality', label: 'Quality compounders', icon: 'i-lucide-gem', desc: 'High returns on capital, low debt, steady growth',
    conds: [{ field: 'roce', op: 'gte', value: '0.2' }, { field: 'debt_equity', op: 'lte', value: '0.5' }, { field: 'sales_yoy', op: 'gte', value: '0.1' }], sort: 'roce' },
  { id: 'growth', label: 'Fast growers', icon: 'i-lucide-rocket', desc: 'Sales and profit up 25%+ over 12 months',
    conds: [{ field: 'sales_yoy', op: 'gte', value: '0.25' }, { field: 'profit_yoy', op: 'gte', value: '0.25' }], sort: 'profit_yoy' },
  { id: 'leaders', label: 'Uptrend leaders', icon: 'i-lucide-trending-up', desc: 'Stage 2, within 10% of the 52-week high',
    conds: [{ field: 'stage', op: 'eq', value: '2' }, { field: 'from_high_52w', op: 'gte', value: '-0.1' }], sort: 'ret_6m' },
  { id: 'value', label: 'Cheap and profitable', icon: 'i-lucide-badge-percent', desc: 'P/E under 15 with ROCE of 15%+',
    conds: [{ field: 'pe', op: 'lte', value: '15' }, { field: 'roce', op: 'gte', value: '0.15' }], sort: 'pe' },
  { id: 'debtfree', label: 'Debt-free', icon: 'i-lucide-shield-check', desc: 'No meaningful borrowings',
    conds: [{ field: 'tags', op: 'has', value: 'Debt-free' }], sort: 'mcap' },
  { id: 'dividend', label: 'Dividend payers', icon: 'i-lucide-coins', desc: 'Yield of 3% or more',
    conds: [{ field: 'dividend_yield', op: 'gte', value: '0.03' }], sort: 'dividend_yield' },
  { id: 'turnaround', label: 'Profit turnarounds', icon: 'i-lucide-refresh-ccw', desc: 'Last quarter profit up 50%+ a year on',
    conds: [{ field: 'q_profit_yoy', op: 'gte', value: '0.5' }, { field: 'profit', op: 'gt', value: '0' }], sort: 'q_profit_yoy' },
]

export const OP_LABEL: Record<string, string> = { gt: '>', gte: '≥', lt: '<', lte: '≤', eq: '=', has: 'is', in: 'in' }

export const COLUMN_SETS: Record<string, { label: string, cols: string[] }> = {
  overview: { label: 'Overview', cols: ['mcap', 'pe', 'roce', 'sales_yoy', 'ret_1y', 'stage'] },
  growth: { label: 'Growth', cols: ['sales', 'sales_yoy', 'profit_yoy', 'q_sales_yoy', 'q_profit_yoy', 'sales_cagr3'] },
  profit: { label: 'Profitability', cols: ['roce', 'roe', 'opm', 'npm', 'cash_conversion', 'debt_equity'] },
  value: { label: 'Valuation', cols: ['mcap', 'pe', 'pb', 'ev_ebitda', 'ps', 'dividend_yield'] },
  technical: { label: 'Price & trend', cols: ['ret_1m', 'ret_3m', 'ret_1y', 'from_high_52w', 'dma200_gap', 'stage'] },
}

/** A condition's value as the user reads it: percentages as %, multiples as x. */
export function condValue(unit: string | undefined, v: string): string {
  const n = Number(v)
  if (unit === 'pct' && Number.isFinite(n)) return `${+(n * 100).toFixed(2)}%`
  if (unit === 'x' && Number.isFinite(n)) return `${n}x`
  if (unit === 'inr' && Number.isFinite(n)) return crore(n)
  return v
}
