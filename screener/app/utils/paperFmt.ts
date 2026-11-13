// Helpers for the paper-trading, strategies and paradigms pages. Every export is prefixed
// (paper*, strat*, PAPER_*, STRAT_*, PARADIGM_*) because Nuxt auto-imports utils by name and
// silently picks one when two files export the same name.
import { PARADIGM_LIST } from './paradigmData'
import type { PtStrategy, PtSummary } from '~/types/paperTracks'

/** The four books every paper strategy is tracked as, with plain-language explanations. */
export const PAPER_BOOK_DEFS = [
  { key: 'strategy', label: 'Strategy', color: 'var(--series-1)',
    hint: 'The frozen rules, on percentage weights.' },
  { key: 'equal-weight', label: 'Equal-weight universe', color: 'var(--chart-axis)',
    hint: 'Every stock the strategy could pick, in equal amounts: what the market alone gave. The strategy has to beat this.' },
  { key: 'random-ranking', label: 'Random picks, same size', color: 'var(--series-2)',
    hint: 'The same number of stocks chosen at random. Tells whether the stock picking added anything.' },
  { key: 'account', label: '₹1 crore account', color: 'var(--series-7)',
    hint: 'The strategy run as a real ₹1 crore account: whole shares only, with Dhan\'s charges.' },
] as const

export const paperBookDef = (key: string) => PAPER_BOOK_DEFS.find(b => b.key === key)

export function paperBook(s: PtStrategy | null | undefined, key = 'strategy'): PtSummary | undefined {
  return s?.summaries?.find(x => x.book === key)
}

/** Strategy return minus the equal-weight book's: a strategy up 12% in a market up 14% has lost. */
export function paperExcess(s: PtStrategy | null | undefined): number | null {
  const a = paperBook(s, 'strategy'), b = paperBook(s, 'equal-weight')
  return a && b ? a.total_return - b.total_return : null
}

/** Annualised from the NAV and the calendar span, so books of different lengths compare. */
export function paperPerYear(s: PtStrategy | null | undefined, key: string): number | null {
  const b = paperBook(s, key)
  if (!s || !b || !s.start || !s.as_of) return null
  const years = (new Date(s.as_of).getTime() - new Date(s.start).getTime()) / (365.25 * 864e5)
  if (years < 0.5) return null
  return Math.pow(b.nav / 100, 1 / years) - 1
}

export const paperIsParadigm = (name: string) => name.startsWith('paradigm-')

const PAPER_WORDS: Record<string, string> = { rs: 'RS', lowvol: 'Low-Vol', top20: 'Top 20', e1: 'E1', stage2: 'Stage 2' }

/** "trend-quintile" -> "Trend Quintile"; a paradigm replica gets its readable label. */
export function paperName(name: string): string {
  for (const p of PARADIGM_LIST) {
    const t = p.tracks.find(x => x.name === name)
    if (t) return t.label
  }
  return name.split('-').map(w => PAPER_WORDS[w] ?? w.charAt(0).toUpperCase() + w.slice(1)).join(' ')
}

/** The manager a paradigm replica copies, e.g. "Alok Jain · WeekendInvesting". */
export function paperParadigmOwner(name: string): string | null {
  const p = PARADIGM_LIST.find(x => x.tracks.some(t => t.name === name))
  return p ? `${p.person} · ${p.firm.split(' — ')[0]}` : null
}

export function paperBps(v: number | null | undefined): string {
  return v == null ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(2)} bps`
}

export function paperRupees(v: number | null | undefined): string {
  return v == null ? '—' : `₹${Math.round(v).toLocaleString('en-IN')}`
}

export function paperLakh(v: number | null | undefined): string {
  return v == null ? '—' : `₹${(v / 1e5).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} L`
}

// ---------------------------------------------------------------- strategies (stockey triggers)

type StratTone = 'good' | 'bad' | 'neutral'
interface StratInfo { label: string, blurb: string, tone: StratTone, icon: string }

// Labels mirror stockey fundamentals/screens/notifications.py STRATEGY_LABELS.
export const STRAT_INFO: Record<string, StratInfo> = {
  rating_downgrade: { label: 'Rating downgrade', tone: 'bad', icon: 'i-lucide-badge-minus',
    blurb: 'A credit rating agency cut the company\'s rating.' },
  rating_confirms_deleveraging: { label: 'Rating confirms debt cut', tone: 'good', icon: 'i-lucide-badge-check',
    blurb: 'A rating action backs up a company that has been paying its debt down.' },
  insider_buy: { label: 'Insider buy', tone: 'good', icon: 'i-lucide-user-plus',
    blurb: 'A promoter or insider bought shares with their own money.' },
  insider_sell_surprise: { label: 'Insider sell (surprise)', tone: 'bad', icon: 'i-lucide-user-minus',
    blurb: 'An insider sold shares when nothing about the company suggested they would.' },
  capital_raise: { label: 'Capital raise', tone: 'neutral', icon: 'i-lucide-landmark',
    blurb: 'The company is raising money by issuing new shares or similar.' },
  institutional_first_entry: { label: 'First institutional entry', tone: 'good', icon: 'i-lucide-building-2',
    blurb: 'A fund or institution showed up among the shareholders for the first time.' },
  llm_flagged: { label: 'Flagged by AI read', tone: 'neutral', icon: 'i-lucide-sparkles',
    blurb: 'An AI reading of a filing flagged something worth a closer look.' },
  story_read_positive: { label: 'Story read: improving', tone: 'good', icon: 'i-lucide-book-open-check',
    blurb: 'An AI read of results, filings and news says the business story is getting better.' },
  story_read_negative: { label: 'Story read: deteriorating', tone: 'bad', icon: 'i-lucide-book-x',
    blurb: 'An AI read of results, filings and news says the business story is getting worse.' },
  results_decline: { label: 'Results decline', tone: 'bad', icon: 'i-lucide-trending-down',
    blurb: 'Quarterly profit fell sharply against the same quarter last year.' },
  results_confirm_turnaround: { label: 'Results confirm turnaround', tone: 'good', icon: 'i-lucide-trending-up',
    blurb: 'New results confirm a recovery the company was already showing signs of.' },
  results_delayed: { label: 'Results delayed', tone: 'bad', icon: 'i-lucide-clock-alert',
    blurb: 'The company filed its quarterly results late.' },
  auditor_change: { label: 'Auditor change', tone: 'bad', icon: 'i-lucide-file-warning',
    blurb: 'The company\'s auditor resigned or was replaced.' },
  related_party_transaction: { label: 'Related-party deal', tone: 'bad', icon: 'i-lucide-handshake',
    blurb: 'A large deal with a party connected to the promoters or management.' },
  pledge_increase: { label: 'Promoter pledge up', tone: 'bad', icon: 'i-lucide-lock',
    blurb: 'Promoters pledged more of their shares as loan collateral.' },
  bulk_deal_buy: { label: 'Bulk / block deal buy', tone: 'good', icon: 'i-lucide-package-plus',
    blurb: 'A large single-day purchase of shares on the exchange (net of sales).' },
  bulk_deal_sell: { label: 'Bulk / block deal sell', tone: 'bad', icon: 'i-lucide-package-minus',
    blurb: 'A large single-day sale of shares on the exchange (net of purchases).' },
}

/** Falls back to the raw trigger name so a new trigger shows up without a frontend change. */
export function stratInfo(t: string): StratInfo {
  return STRAT_INFO[t] ?? { label: t.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()), blurb: '', tone: 'neutral', icon: 'i-lucide-zap' }
}

export const STRAT_TONE_BADGE: Record<StratTone, { color: 'success' | 'error' | 'neutral', label: string }> = {
  good: { color: 'success', label: 'Positive sign' },
  bad: { color: 'error', label: 'Warning sign' },
  neutral: { color: 'neutral', label: 'Neutral' },
}

/** "nse:AFFLE" -> "AFFLE"; "nse:544687-BOM" -> "544687-BOM". */
export const stratSymbol = (id: string) => id.replace(/^nse:/, '')
