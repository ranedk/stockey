// Plain, fixed rules for the company scorecard. Each verdict carries the rule that produced it.
type Verdict = { label: string, color: 'success' | 'warning' | 'error' | 'neutral' | 'info', rule: string }
const n = (v: any) => (isNum(v) ? v : null)

export function growthVerdict(c: any): Verdict {
  const s = n(c.sales_yoy), p = n(c.profit_yoy)
  if (s == null) return { label: 'No data', color: 'neutral', rule: 'Needs four quarters of filed results' }
  if (s >= 0.15 && (p == null || p >= 0.15)) return { label: 'Growing fast', color: 'success', rule: 'Sales and profit up 15% or more over the last 12 months' }
  if (s < 0) return { label: 'Shrinking', color: 'error', rule: 'Sales lower than a year ago' }
  return { label: 'Steady', color: 'warning', rule: 'Sales growing, under 15% a year' }
}

export function profitVerdict(c: any): Verdict {
  const fin = ['bank', 'nbfc', 'insurer'].includes(c.family)
  const r = n(fin ? c.roe : c.roce)
  const name = fin ? 'ROE' : 'ROCE'
  if (n(c.profit) != null && c.profit < 0) return { label: 'Loss-making', color: 'error', rule: 'Net loss over the last 12 months' }
  if (r == null) return { label: 'No data', color: 'neutral', rule: 'Needs a filed balance sheet' }
  if (r >= 0.18) return { label: 'High returns', color: 'success', rule: `${name} of 18% or more` }
  if (r < 0.08) return { label: 'Low returns', color: 'error', rule: `${name} under 8%` }
  return { label: 'Moderate', color: 'warning', rule: `${name} between 8% and 18%` }
}

export function balanceVerdict(c: any): Verdict {
  if (['bank', 'nbfc', 'insurer'].includes(c.family)) return { label: 'Lender', color: 'neutral', rule: 'Debt is the raw material of a lender; judge by asset quality instead' }
  const de = n(c.debt_equity), ic = n(c.interest_cover)
  if (de == null) return { label: 'No data', color: 'neutral', rule: 'Needs a filed balance sheet' }
  if (de <= 0.3 && (ic == null || ic >= 5)) return { label: 'Strong', color: 'success', rule: 'Debt at most 0.3x equity, interest covered 5x or more' }
  if (de > 1.5 || (ic != null && ic < 2)) return { label: 'Stretched', color: 'error', rule: 'Debt above 1.5x equity, or interest covered less than 2x' }
  return { label: 'Manageable', color: 'warning', rule: 'Debt between 0.3x and 1.5x equity' }
}

export function valueVerdict(c: any, m: any): Verdict {
  const pe = n(c.pe), mp = n(m?.pe)
  if (pe == null) return { label: 'No P/E', color: 'neutral', rule: 'No profit over the last 12 months' }
  if (mp == null) return { label: `P/E ${mult(pe)}`, color: 'neutral', rule: 'No industry comparison' }
  if (pe < mp * 0.8) return { label: 'Below peers', color: 'info', rule: 'P/E more than 20% below the industry median' }
  if (pe > mp * 1.25) return { label: 'Above peers', color: 'info', rule: 'P/E more than 25% above the industry median' }
  return { label: 'In line', color: 'neutral', rule: 'P/E within the industry range' }
}

export function trendVerdict(c: any): Verdict {
  const s = c.stage
  if (s === 2) return { label: 'Uptrend', color: 'success', rule: STAGES[2]!.desc }
  if (s === 4) return { label: 'Downtrend', color: 'error', rule: STAGES[4]!.desc }
  if (s === 3) return { label: 'Topping', color: 'warning', rule: STAGES[3]!.desc }
  if (s === 1) return { label: 'Basing', color: 'neutral', rule: STAGES[1]!.desc }
  return { label: 'No data', color: 'neutral', rule: 'Not enough price history' }
}
