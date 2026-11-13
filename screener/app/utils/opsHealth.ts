// Operator pages (/data-health, /todos): response shapes of stockey's fundamentals API
// (stockey/fundamentals/api/app.py) plus the wording and status styling they share.
// Every export is prefixed "ops"/"Ops" because utils are auto-imported app-wide.

export type OpsLevel = 'ok' | 'warn' | 'error'

export interface OpsDataHealthFinding { level: OpsLevel, check: string, message: string, metrics?: Record<string, string | number | null> }
export interface OpsDataHealth {
  status: OpsLevel, checked_at: string, window_days: number, error_count: number, warn_count: number,
  findings: OpsDataHealthFinding[], cached?: boolean, age_seconds?: number
}

export interface OpsSchedulerFinding { level: OpsLevel, section: string, message: string }
export interface OpsScheduler { status: OpsLevel, error_count: number, warn_count: number, checked_at: string, findings: OpsSchedulerFinding[] }

export type OpsCollectorClass = 'failing' | 'frozen' | 'orphaned' | 'ok'
export interface OpsCollector {
  source_name: string, scope_key: string | null, status: string, error_text: string | null,
  last_success_at: string | null, last_item_ts: string | null, updated_at: string | null,
  module: string, classification: OpsCollectorClass
}
export interface OpsCollectors { counts: Record<string, number>, failing_count: number, registry_module_count: number, collectors: OpsCollector[] }

export interface OpsIdentityIssue {
  issue_key: string, issue_type: string, symbol: string | null, requested_exchange: string | null,
  status: string, first_seen_at: string | null, last_seen_at: string | null, error_text: string | null
  attempt_count?: number | null, suggested_action?: string | null, source?: string | null
}
export interface OpsFallbackEvent {
  event_id: string, observed_at: string | null, severity: string, fallback_type: string, module: string | null
  source: string | null, reason: string | null, error_message: string | null, symbol: string | null
}
export interface OpsPlatformIssues {
  identity_issues: { open_count: number, by_type: Record<string, number>, issues: OpsIdentityIssue[], limit: number }
  fallback_events: {
    window_hours: number, active_count?: number, error_count?: number, warn_count?: number,
    counts_by_type?: Record<string, number>, counts_by_module?: Record<string, number>, message?: string
    rows?: OpsFallbackEvent[]
  }
}

export interface OpsCoverageTable {
  table_name: string, category: string | null, check_kind: string | null, status: string,
  rows: number | null, symbols: number | null, min_date: string | null, max_date: string | null,
  staleness_days: number | null, detail: string | null, report_date: string
}
export interface OpsCoverage {
  report_date: string | null, not_ok_count: number, tables: OpsCoverageTable[]
  staleness_history: Record<string, { report_date: string, staleness_days: number | null }[]>
}

export interface OpsRatingAgency {
  agency_name: string, occurrence_count: number, first_seen_at: string | null, last_seen_at: string | null
  example_headline: string | null, example_company_master_id: string | null
}
export interface OpsTodos { rating_agencies: OpsRatingAgency[] }

type Color = 'success' | 'warning' | 'error' | 'neutral' | 'info'
export const OPS_LEVEL: Record<string, { label: string, color: Color, icon: string, rank: number }> = {
  error: { label: 'Failing', color: 'error', icon: 'i-lucide-circle-x', rank: 0 },
  warn: { label: 'Warning', color: 'warning', icon: 'i-lucide-triangle-alert', rank: 1 },
  ok: { label: 'OK', color: 'success', icon: 'i-lucide-circle-check', rank: 2 },
}
export const opsLevel = (l?: string | null) => OPS_LEVEL[l || ''] ?? { label: l || 'Unknown', color: 'neutral' as Color, icon: 'i-lucide-circle-help', rank: 3 }

// Plain names for the check ids. The API keeps the ids as its machine contract (the nightly
// gate reads the same payload); wording lives here.
export const OPS_CHECKS: Record<string, { label: string, about: string }> = {
  bhavcopy: { label: 'NSE daily prices', about: 'The exchange\'s end-of-day price file (bhavcopy) arrived and was loaded for the latest session.' },
  adjustment_join: { label: 'Split and bonus adjustments', about: 'Every daily price bar has a matching adjustment factor, so adjusted prices are complete.' },
  dhan_daily: { label: 'Broker daily prices: breadth', about: 'How many stocks got a daily bar from Dhan in the latest session, against the recent normal.' },
  dhan_daily_freshness: { label: 'Broker daily prices: freshness', about: 'How recent the newest Dhan daily bar is.' },
  intraday_breadth: { label: 'Minute prices: breadth', about: 'How many stocks got 1-minute bars from Dhan in the latest session, against the recent normal.' },
  intraday_freshness: { label: 'Minute prices: freshness', about: 'How recent the newest 1-minute bar is.' },
  intraday_truncated: { label: 'Full trading sessions', about: 'Each session\'s minute data runs all the way to the market close, not cut off part way.' },
  universe_coverage: { label: 'Every stock covered', about: 'Active stocks that have had no minute data at all in the window.' },
  trading_days: { label: 'Trading calendar', about: 'How far ahead the calendar of trading days is filled in. It is extended once NSE publishes next year\'s holidays.' },
}
export const opsCheck = (id: string) => OPS_CHECKS[id] ?? { label: id.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()), about: '' }

export const OPS_COLLECTOR_CLASS: Record<OpsCollectorClass, { label: string, color: Color, icon: string, about: string }> = {
  failing: { label: 'Failing', color: 'error', icon: 'i-lucide-circle-x', about: 'Live collector, currently erroring.' },
  ok: { label: 'OK', color: 'success', icon: 'i-lucide-circle-check', about: 'Live collector, last run succeeded.' },
  frozen: { label: 'Paused on purpose', color: 'neutral', icon: 'i-lucide-snowflake', about: 'Deliberately unscheduled. Any error shown is old history, not a live failure.' },
  orphaned: { label: 'Left-over row', color: 'neutral', icon: 'i-lucide-ghost', about: 'The module was deleted or renamed. Its last status stays in the table forever; it is not a failure.' },
}

// "download_runner:data.nseindia.bhavcopy_parser" -> "NSE · bhavcopy parser"
const SOURCE_NAMES: Record<string, string> = { nseindia: 'NSE', bseindia: 'BSE', dhanlive: 'Dhan', rbi: 'RBI', sharpelydata: 'Sharpely' }
export function opsCollectorName(c: Pick<OpsCollector, 'module' | 'source_name'>): { source: string, name: string } {
  const parts = (c.module || c.source_name).replace(/^data\./, '').split('.')
  const name = (parts.pop() || '').replaceAll('_', ' ')
  const src = parts.map(p => SOURCE_NAMES[p] ?? p).join(' · ')
  return { source: src || 'Platform', name: name.replace(/^./, ch => ch.toUpperCase()) }
}

const IST = 'Asia/Kolkata'
/** "13 Nov 2026, 4:12 pm" in IST, whatever the browser's clock zone. */
export function opsDateTime(v: unknown): string {
  if (!v) return '—'
  const d = new Date(String(v))
  if (Number.isNaN(+d)) return '—'
  return d.toLocaleString('en-IN', { timeZone: IST, day: 'numeric', month: 'short', year: 'numeric', hour: 'numeric', minute: '2-digit' })
}
export function opsDate(v: unknown): string {
  if (!v) return '—'
  const d = new Date(String(v))
  if (Number.isNaN(+d)) return '—'
  return d.toLocaleDateString('en-IN', { timeZone: IST, day: 'numeric', month: 'short', year: 'numeric' })
}
/** "5 min ago", "3 h ago", "12 days ago". */
export function opsAgo(v: unknown, now = Date.now()): string {
  if (!v) return 'never'
  const t = +new Date(String(v))
  if (Number.isNaN(t)) return '—'
  const s = Math.max(0, (now - t) / 1000)
  if (s < 90) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} days ago`
}

// Literal class names (Tailwind only generates classes it can find spelled out in source).
const OPS_TEXT: Record<string, string> = { success: 'text-success', warning: 'text-warning', error: 'text-error', neutral: 'text-muted', info: 'text-info' }
export const opsText = (level?: string | null) => OPS_TEXT[opsLevel(level).color] ?? 'text-muted'
