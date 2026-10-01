import type { StoryScoreRow } from '~/types/systrader'

export const STAGE_TONE: Record<number, 'good' | 'bad' | 'neutral' | 'warn'> = { 0: 'neutral', 1: 'neutral', 2: 'good', 3: 'warn', 4: 'bad' }
export const STAGE_SHORT: Record<number, string> = { 0: '—', 1: 'S1 basing', 2: 'S2 advancing', 3: 'S3 topping', 4: 'S4 declining' }
export function stageShort(n: number): string { return STAGE_SHORT[n] ?? '—' }

// Rank movement: positive = climbed (a smaller rank number is stronger).
export function rankMove(now: number, before: number): number | null {
  if (!now || !before) return null
  return before - now
}

// The forward-only fundamental filter (PRD section 2.4): no flaw and a story score at or
// above the median of all scored companies.
export function storyFilter(row: StoryScoreRow | undefined, median: number | null): 'pass' | 'fail' | 'unknown' {
  if (!row || row.story_score === null || median === null) return 'unknown'
  if (row.flaws) return 'fail'
  return row.story_score >= median ? 'pass' : 'fail'
}

export function medianOf(values: number[]): number | null {
  if (!values.length) return null
  const v = [...values].sort((a, b) => a - b)
  return v[Math.floor(v.length / 2)] ?? null
}
