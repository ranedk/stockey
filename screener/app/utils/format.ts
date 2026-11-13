// Indian number formatting: crores and lakhs, as Indian investors read them.
const CR = 1e7

export const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export function inr(v: unknown, digits = 2): string {
  if (!isNum(v)) return '—'
  return '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: v < 100 ? Math.min(2, digits) : 0 })
}

/** Rupee amounts in crores: "₹1,23,456 Cr", "₹12.3 Cr", "₹45 L" under a crore. */
export function crore(v: unknown, opts: { unit?: boolean } = {}): string {
  if (!isNum(v)) return '—'
  const unit = opts.unit === false ? '' : ' Cr'
  const c = v / CR
  const a = Math.abs(c)
  if (a >= 1000) return `₹${Math.round(c).toLocaleString('en-IN')}${unit}`
  if (a >= 10) return `₹${c.toLocaleString('en-IN', { maximumFractionDigits: 0 })}${unit}`
  if (a >= 1) return `₹${c.toLocaleString('en-IN', { maximumFractionDigits: 1 })}${unit}`
  if (a > 0) return `₹${(v / 1e5).toLocaleString('en-IN', { maximumFractionDigits: 1 })} L`
  return `₹0${unit}`
}

/** Compact crores for chart axes: 1.2L Cr, 45K Cr, 850 Cr. */
export function croreShort(v: unknown): string {
  if (!isNum(v)) return ''
  if (v === 0) return '0'
  const c = v / CR
  const a = Math.abs(c)
  if (a >= 1e5) return `${(c / 1e5).toFixed(a >= 1e6 ? 0 : 1)}L Cr`
  if (a >= 1e3) return `${(c / 1e3).toFixed(a >= 1e4 ? 0 : 1)}K Cr`
  if (a >= 1) return `${c.toFixed(a >= 100 ? 0 : 1)} Cr`
  return `${(v / 1e5).toFixed(1)} L`
}

export function pct(v: unknown, digits = 1, signed = false): string {
  if (!isNum(v)) return '—'
  const s = (v * 100).toFixed(digits)
  return (signed && v > 0 ? '+' : '') + s + '%'
}

export function mult(v: unknown, digits = 1): string {
  if (!isNum(v)) return '—'
  if (Math.abs(v) >= 1000) return '>999x'
  return v.toFixed(digits) + 'x'
}

export function num(v: unknown, digits = 0): string {
  if (!isNum(v)) return '—'
  return v.toLocaleString('en-IN', { maximumFractionDigits: digits })
}

export function dateShort(v: unknown): string {
  if (!v) return '—'
  const d = new Date(String(v))
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })
}

/** Indian fiscal labels: period ending 2026-06-30 -> "Jun '26"; fy ending 2026-03-31 -> "FY26". */
export function periodLabel(end: string, period: string): string {
  const d = new Date(end)
  const yy = String(d.getFullYear()).slice(2)
  if (period === 'fy') return d.getMonth() === 2 ? `FY${yy}` : `${d.toLocaleString('en', { month: 'short' })} '${yy}`
  return `${d.toLocaleString('en', { month: 'short' })} '${yy}`
}

export function signClass(v: unknown): string {
  if (!isNum(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-up' : 'text-down'
}

export function arrow(v: unknown): string {
  if (!isNum(v) || v === 0) return ''
  return v > 0 ? '▲' : '▼'
}

/** Format any catalogue field by its unit. */
export function byUnit(unit: string, v: unknown): string {
  switch (unit) {
    case 'inr': return Math.abs(Number(v)) >= 1e7 ? crore(v) : inr(v)
    case 'pct': return pct(v)
    case 'x': return mult(v)
    case 'days': return isNum(v) ? `${Math.round(v)} d` : '—'
    case 'num': return num(v, 1)
    case 'date': return dateShort(v)
    default: return v == null || v === '' ? '—' : String(v)
  }
}
