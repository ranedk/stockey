// Visual weight for each tag (serving/company.py tags()). Colour is a hint; the text always says it.
const TONE: Record<string, { color: 'success' | 'error' | 'warning' | 'info' | 'neutral' | 'primary', icon: string }> = {
  'High ROCE': { color: 'success', icon: 'i-lucide-gauge' },
  'Fast growth': { color: 'success', icon: 'i-lucide-rocket' },
  'Debt-free': { color: 'success', icon: 'i-lucide-shield-check' },
  'Uptrend': { color: 'success', icon: 'i-lucide-trending-up' },
  'Near 52w high': { color: 'info', icon: 'i-lucide-arrow-up-to-line' },
  'High dividend': { color: 'info', icon: 'i-lucide-coins' },
  'SME': { color: 'neutral', icon: 'i-lucide-store' },
  'Loss-making': { color: 'error', icon: 'i-lucide-circle-minus' },
  'High debt': { color: 'warning', icon: 'i-lucide-landmark' },
  'Sales falling': { color: 'warning', icon: 'i-lucide-trending-down' },
  'Cash burn': { color: 'warning', icon: 'i-lucide-flame' },
  'Far below high': { color: 'neutral', icon: 'i-lucide-arrow-down-to-line' },
  'Downtrend': { color: 'error', icon: 'i-lucide-trending-down' },
}
export const tagTone = (t: string) => TONE[t] ?? { color: 'neutral' as const, icon: 'i-lucide-tag' }

export const STAGES: Record<number, { label: string, short: string, color: 'success' | 'error' | 'warning' | 'info' | 'neutral', desc: string }> = {
  1: { label: 'Stage 1 · Basing', short: 'Basing', color: 'neutral', desc: 'Price moving sideways around a flat 30-week average after a fall.' },
  2: { label: 'Stage 2 · Advancing', short: 'Advancing', color: 'success', desc: 'Price above a rising 30-week average: an uptrend.' },
  3: { label: 'Stage 3 · Topping', short: 'Topping', color: 'warning', desc: 'Uptrend losing pace; price chopping around a flattening average.' },
  4: { label: 'Stage 4 · Declining', short: 'Declining', color: 'error', desc: 'Price below a falling 30-week average: a downtrend.' },
}

export const SIZE_DESC: Record<string, string> = {
  Large: 'Top 100 companies by market cap',
  Mid: 'Ranked 101-250 by market cap',
  Small: 'Ranked 251-500 by market cap',
  Micro: 'Ranked below 500 by market cap',
}
