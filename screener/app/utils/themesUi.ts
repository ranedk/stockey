// Themes pages: stockey GET /api/themes and /api/themes/{id} (fundamentals/api/themes_queries.py).
// Percent fields arrive already in percent (51.0 = 51%); ranks arrive 0..1 and show as 0..100.

export interface ThemesRow {
  theme_id: string
  name?: string
  area?: string
  definition?: string
  as_of_date: string
  status: 'shown' | 'next' | 'out' | 'late' | 'unranked'
  position: number | null
  kept_by_buffer: boolean
  n_ranked: number
  n_unquantified: number
  n_excluded: number
  strength: number | null
  strength_order: number | null
  late: boolean | null
  late_reasons: string | null
  policy_rank: number | null
  capacity_rank: number | null
  fundamentals_rank: number | null
  breadth_rank: number | null
  sales_yoy_pct: number | null
  margin_change_pp: number | null
  breadth_stage2_pct: number | null
  index_rs26_pct: number | null
  n_members: number | null
  reasoning: string | null
  conclusion: string | null
}

export interface ThemesStock {
  theme_id: string
  company_master_id: string
  ticker: string | null
  company_name: string | null
  status: 'ranked' | 'unquantified' | 'excluded'
  reasons: string | null
  share_pct: number | null
  share_source: string | null
  exposure_rank: number | null
  trend_rank: number | null
  business_rank: number | null
  capacity_rank: number | null
  combined: number | null
  position: number | null
  rating: string | null
  on_watchlist: boolean
  also_strong?: boolean
  theme_name?: string | null
  stage: number | null
  weeks_in_stage: number | null
  tier: number | null
  rs26_pct: number | null
  from_breakout_pct: number | null
  sales_yoy_pct: number | null
  roce_pct: number | null
  reasoning: string | null
  conclusion: string | null
  web_verdict: 'clear' | 'watch' | 'red_flag' | null
  web_checked_at: string | null
  web_summary: string | null
  web_recent_changes: string | null
  web_red_flags: string | null
  web_sources: string[] | null
}

export interface ThemesResponse { as_of: string | null, themes: ThemesRow[], stocks: ThemesStock[], also_strong: ThemesStock[] }

export interface ThemesPolicy {
  name: string
  kind: string
  direction: 'support' | 'headwind'
  authority: string | null
  announced: string | null
  outlay_cr: number | null
  status: string
  latest_figure: string | null
  source_url: string
}

export interface ThemesDetail { theme: ThemesRow, stocks: ThemesStock[], policy: ThemesPolicy[] }

type Tone = 'success' | 'error' | 'warning' | 'info' | 'neutral' | 'primary'

/** 0..1 rank as a 0..100 score, or null. */
export const themesScore = (v: number | null | undefined) => (isNum(v) ? Math.round(v * 100) : null)
/** A percent value (51.0) as the fraction Change/pct expect (0.51). */
export const themesFrac = (v: number | null | undefined) => (isNum(v) ? v / 100 : null)
export const themesHost = (u: string) => { try { return new URL(u).hostname.replace(/^www\./, '') } catch { return u } }

export const THEMES_PARTS = [
  { key: 'policy_rank', label: 'Government support', desc: 'Schemes, incentives and spending aimed at this market, against the other themes.' },
  { key: 'capacity_rank', label: 'Capacity being built', desc: 'How fast members (and the country) are adding factories and equipment.' },
  { key: 'fundamentals_rank', label: 'Latest results', desc: "Members' latest quarterly sales growth and margin change." },
  { key: 'breadth_rank', label: 'Prices rising', desc: 'Share of members in an uptrend, and how the theme is doing against the market.' },
] as const

export const THEMES_STOCK_PARTS = [
  { key: 'exposure_rank', short: 'Share', label: 'Revenue share in the theme' },
  { key: 'trend_rank', short: 'Trend', label: 'Price trend (an early breakout ranks above an extended run)' },
  { key: 'business_rank', short: 'Results', label: 'Business results' },
  { key: 'capacity_rank', short: 'Capacity', label: 'Capacity being built' },
] as const

export const THEMES_STATUS: Record<string, { label: string, color: Tone, icon: string, desc: string }> = {
  shown: { label: 'On the list', color: 'success', icon: 'i-lucide-star', desc: 'One of the five strongest themes that are not late.' },
  next: { label: 'Next up', color: 'info', icon: 'i-lucide-arrow-up-right', desc: 'Just below the five on the list.' },
  late: { label: 'Late', color: 'error', icon: 'i-lucide-alarm-clock', desc: 'More capacity is coming than demand: left off the list however strong.' },
  out: { label: 'Not shown', color: 'neutral', icon: 'i-lucide-circle-dashed', desc: 'Ranked, but outside the top of the list.' },
  unranked: { label: 'Not ranked', color: 'neutral', icon: 'i-lucide-circle-off', desc: 'Fewer than 5 listed members with results, so it is not ranked.' },
}

export const THEMES_RATING: Record<string, Tone> = { A: 'success', B: 'success', C: 'neutral', D: 'warning', E: 'error' }
export const THEMES_RATING_DESC = 'Place among the ranked members of its theme, in fifths: A = top fifth, E = bottom fifth.'

export const THEMES_WEB: Record<string, { label: string, color: Tone, icon: string, desc: string }> = {
  clear: { label: 'Web: clear', color: 'success', icon: 'i-lucide-shield-check', desc: 'Overnight web check found no governance or business problem.' },
  watch: { label: 'Web: watch', color: 'warning', icon: 'i-lucide-eye', desc: 'Worth knowing but not disqualifying: a pending case, a tax demand under appeal, a business change, a big deal.' },
  red_flag: { label: 'Web: red flag', color: 'error', icon: 'i-lucide-flag', desc: 'A serious problem in the last 5 years: auditor issue, regulator order, default, fraud finding.' },
}

export const THEMES_EARLY_DESC = 'Early breakout: in an uptrend for 13 weeks or less and no more than 25% above its 30-week average.'
