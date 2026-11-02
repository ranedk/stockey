// Helpers for the /themes pages (stockey docs/THEMATIC_WATCHLIST_PRD.md).

export const RATING_TONE: Record<string, 'good' | 'neutral' | 'warn' | 'bad'> = { A: 'good', B: 'good', C: 'neutral', D: 'warn', E: 'bad' }

// a 0..1 percentile rank shown as 0..100
export function score(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : String(Math.round(v * 100))
}

export function signed(v: number | null | undefined, unit = '%'): string {
  if (v === null || v === undefined) return '—'
  const r = Math.round(v)
  return `${r > 0 ? '+' : ''}${r}${unit}`
}

export function stageText(stage: number | null, weeks: number | null, tier: number | null): string {
  if (!stage) return 'no stage'
  const w = weeks ? ` · ${weeks}w` : ''
  return `${stageShort(stage)}${w}${tier === 4 ? ' · early' : ''}`
}

export const WEB_TONE: Record<string, 'good' | 'warn' | 'bad'> = { clear: 'good', watch: 'warn', red_flag: 'bad' }
export const WEB_LABEL: Record<string, string> = { clear: 'web: clear', watch: 'web: watch', red_flag: 'web: red flag' }
export function host(u: string): string { try { return new URL(u).hostname.replace(/^www\./, '') } catch { return u } }
