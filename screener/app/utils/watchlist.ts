// Labels and small formatters for the watchlist, portfolio and investors pages. Every export is
// prefixed `wl` because utils are auto-imported app-wide and names must not collide.
import type { WatchlistStatus } from '~/types/watchlist'
import { isNum } from './format'

type Color = 'success' | 'error' | 'warning' | 'info' | 'neutral' | 'primary'

// Mirrors fundamentals/screens/watchlist_exit.py's status values. Only 'active' is the normal
// state; the others are exit signals that move a company out of the default view, never delete it.
export const WL_STATUS: Record<WatchlistStatus, { label: string, color: Color, desc: string }> = {
  active: { label: 'Active', color: 'success', desc: 'On the watchlist now.' },
  stale: { label: 'Stale', color: 'neutral', desc: 'Nothing new has happened for a long time.' },
  invalidated: { label: 'Invalidated', color: 'error', desc: 'A later event contradicted the reason it was added.' },
  price_flagged: { label: 'Price flagged', color: 'warning', desc: 'The price moved a lot since it was added.' },
  no_thesis: { label: 'No thesis', color: 'neutral', desc: 'Only negative events ever put it here; nothing positive justified watching it.' },
  flawed: { label: 'Flawed', color: 'error', desc: 'A deal-breaker flaw appeared, or it no longer qualifies.' },
  faded: { label: 'Story faded', color: 'neutral', desc: 'The story score fell below 65.' },
}

// Readable names for the event types (trigger_type) fundamentals/screens/l3_triggers.py writes.
const WL_EVENT_LABELS: Record<string, string> = {
  rating_downgrade: 'Rating downgrade',
  rating_confirms_deleveraging: 'Rating confirms debt reduction',
  insider_buy: 'Insider buy',
  insider_sell_surprise: 'Insider sell (surprise)',
  capital_raise: 'Capital raise',
  institutional_first_entry: 'First institutional entry',
  llm_flagged: 'Flagged by AI reader',
  results_decline: 'Results decline',
  results_confirm_turnaround: 'Results confirm turnaround',
  results_delayed: 'Results delayed',
  auditor_change: 'Auditor change',
  related_party_transaction: 'Related-party transaction',
  pledge_increase: 'Promoter pledge increase',
  bulk_deal_buy: 'Bulk/block deal buy (net)',
  bulk_deal_sell: 'Bulk/block deal sell (net)',
  story_read_positive: 'Story read: positive',
  story_read_negative: 'Story read: negative',
}
// Events that count against a company, so their tags render in a warning colour.
const WL_NEGATIVE_EVENTS = new Set(['rating_downgrade', 'insider_sell_surprise', 'results_decline', 'results_delayed',
  'auditor_change', 'related_party_transaction', 'pledge_increase', 'bulk_deal_sell', 'story_read_negative'])
export const wlEventColor = (t: string): Color => (WL_NEGATIVE_EVENTS.has(t) ? 'warning' : 'neutral')
export const wlEventLabel = (t: string | null | undefined) => (t ? WL_EVENT_LABELS[t] || t.replace(/_/g, ' ') : '—')

/** Symbol from a company_master_id like "nse:TCS"; null when it is not an NSE id. */
export const wlSymbol = (id: string) => (id.startsWith('nse:') ? id.slice(4) : null)
export const wlTicker = (id: string) => id.replace(/^nse:/, '')

/** Percentage change between two prices, as a fraction (0.05 = +5%). */
export function wlChange(from: number | null | undefined, to: number | null | undefined): number | null {
  if (!isNum(from) || !isNum(to) || from === 0) return null
  return to / from - 1
}

/** A value already in percent units (12.3 means 12.3%) to a fraction for pct()/Change. */
export const wlFrac = (v: number | null | undefined) => (isNum(v) ? v / 100 : null)

/** Whole-rupee amount with Indian grouping: ₹1,00,000. */
export const wlRupees = (v: number | null | undefined) => (isNum(v) ? `₹${Math.round(v).toLocaleString('en-IN')}` : '—')

/** Rupees in lakh: ₹3.55 L. */
export const wlLakh = (v: number | null | undefined) => (isNum(v) ? `₹${(v / 1e5).toLocaleString('en-IN', { maximumFractionDigits: 2, minimumFractionDigits: 2 })} L` : '—')

export function wlDaysFromNow(d: string | null | undefined): number | null {
  if (!d) return null
  return Math.round((new Date(d).getTime() - Date.now()) / 86_400_000)
}

export const WL_DIMENSION: Record<string, string> = {
  value: 'Value',
  growth: 'Growth',
  balance_sheet: 'Balance sheet',
  profitability: 'Profitability',
  margins: 'Margins',
  events: 'Events',
  ownership: 'Ownership',
  sector: 'Sector',
}
export const wlDimension = (d: string | null | undefined) => (d ? WL_DIMENSION[d] || d.replace(/_/g, ' ') : null)

/** How a company got onto the watchlist (entry_basis). */
export const WL_ENTRY_BASIS: Record<string, { label: string, desc: string }> = {
  'story': { label: 'Strong story', desc: 'Story score 80 or more and no deal-breaker flaw.' },
  'event': { label: 'Recent event', desc: 'A recent positive event while its story score is above the median.' },
  'story+event': { label: 'Story + event', desc: 'Strong story score and a recent positive event.' },
}

const WL_CLOSE: Record<string, string> = {
  thesis_invalidation: 'forecast proved wrong', thesis_broken: 'reason broke', stop_loss: 'stop-loss hit',
  story_fading: 'story faded', superseded_by_accept: 'replaced by a newer entry',
}
export const wlCloseReason = (r: string | null | undefined) => (r ? WL_CLOSE[r] || r.replace(/_/g, ' ') : '')
