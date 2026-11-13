// Stages (industry rotation) pages: systrader GET /api/rotation, /api/rotation/industry/{code};
// stockey GET /api/story-scores for the fundamentals check. Percent fields arrive in percent.

export interface StagesMarket { stage: number, stage_label: string, breadth_pct: number, eligible_count: number, return_26w_pct: number | null }

export interface StagesIndustry {
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

export interface StagesStock {
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

export interface StagesQuantiles { p50: number | null, p75: number | null, p90: number | null, mean: number | null }
export interface StagesStats {
  stage2_weeks: StagesQuantiles
  stage2_runs: number
  leadership_weeks: StagesQuantiles
  leadership_runs: number
  top_fifth_weeks: StagesQuantiles
  new_leaders_per_quarter: number | null
  from: string
}
export interface StagesResponse { as_of: string, market: StagesMarket, industries: StagesIndustry[], stocks: StagesStock[] | null, stats: StagesStats }
export interface StagesIndustryResponse { as_of: string, market: StagesMarket, industry: StagesIndustry, stocks: StagesStock[] | null }

export interface StagesStoryRow {
  symbol: string
  company_master_id: string
  story_score: number | null
  primary_dimension: string | null
  flaws: string | null
  in_band: boolean | null
  watchlist_status: string | null
}

/** Same colours as the Markets stage mix; 0 = not enough history for a stage. */
export const STAGES_COLOR_VAR: Record<number, string> = { 0: 'var(--chart-grid)', 1: 'var(--series-1)', 2: 'var(--up)', 3: 'var(--series-4)', 4: 'var(--down)' }

/** Rank movement: positive = climbed (a smaller rank number is stronger). */
export function stagesRankMove(now: number, before: number): number | null {
  if (!now || !before) return null
  return before - now
}

export function stagesMedian(values: number[]): number | null {
  if (!values.length) return null
  const v = [...values].sort((a, b) => a - b)
  return v[Math.floor(v.length / 2)] ?? null
}

/** The forward-only fundamentals check: no flaw and a story score at or above the median. */
export function stagesStoryFilter(row: StagesStoryRow | undefined, median: number | null): 'pass' | 'fail' | 'unknown' {
  if (!row || row.story_score === null || median === null) return 'unknown'
  if (row.flaws && row.flaws.trim()) return 'fail'
  return row.story_score >= median ? 'pass' : 'fail'
}

export const STAGES_FILTER: Record<string, { label: string, color: 'success' | 'error' | 'neutral', icon: string }> = {
  pass: { label: 'Passes', color: 'success', icon: 'i-lucide-check' },
  fail: { label: 'Fails', color: 'error', icon: 'i-lucide-x' },
  unknown: { label: 'No score', color: 'neutral', icon: 'i-lucide-minus' },
}

export const STAGES_HELP = {
  rs26: 'Return over the last 26 weeks minus the market\'s return (equal-weight market of stocks trading ₹10 Cr+ a day).',
  rank: 'Rank of the industry by its 26-week return against the market; 1 is strongest. Industries with fewer than 5 liquid members are not ranked.',
  leading: 'Leading: in the top fifth by 26-week return against the market while its own index is in an uptrend (Stage 2).',
  liquid: 'Liquid: trading ₹10 Cr or more a day.',
  aboveMa: 'How far the price is above its 30-week average.',
  slope: 'Change in the 30-week average over the last 4 weeks.',
  volume: 'Volume in the last week against its 10-week average; above 1x = busier than usual.',
  story: 'Story score from the fundamentals platform (0-100), with the strongest part of the story.',
  filter: 'Fundamentals check: no flaw and a story score at or above the median of all scored companies. Story scores exist only from 2026-09-29, so this is tested forward only.',
}
