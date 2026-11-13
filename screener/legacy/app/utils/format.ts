export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `₹${value.toFixed(2)}`
}

export function formatPct(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(1)}%`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '—'
  return value.slice(0, 10)
}

export function priceChangePct(from: number | null | undefined, to: number | null | undefined): number | null {
  if (from === null || from === undefined || to === null || to === undefined || from === 0) return null
  return ((to - from) / from) * 100
}
