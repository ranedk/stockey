<script setup lang="ts">
import type { Dict, TraceSummary } from '~/types/api'

const api = useOperatorApi()
const actionLimit = ref(25)
const actionOffset = ref(0)
const actionSymbol = ref('')
const actionType = ref('ALL')
const actionStatus = ref('all')
const actionSearch = ref('')
const portfolioLimit = ref(25)
const portfolioOffset = ref(0)
const portfolioSymbol = ref('')
const portfolioStatus = ref('all')
const portfolioSearch = ref('')
const portfolioBucket = ref('today_recommendations')

const [{ data: marketContext }, { data: technicalCalibration }, { data: signalRefresh, error: signalRefreshError }] = await Promise.all([
  useAsyncData('market-context', () => api.getMarketContext(20)),
  useAsyncData('technical-calibration-home', () => api.getTechnicalCalibration(3)),
  useAsyncData('signal-refresh-home', () => api.getSignalRefresh({ limit: 12, compact: true }))
])

const { data: home } = useAsyncData('home', () => api.getHome(), {
  lazy: true,
  server: false
})
const { data: actionsData, refresh: refreshActions, error: actionsError } = useAsyncData('home-actions-paged', () => api.getActions({
  limit: actionLimit.value,
  offset: actionOffset.value,
  symbol: actionSymbol.value.trim().toUpperCase(),
  action: actionType.value,
  status: actionStatus.value,
  search: actionSearch.value.trim(),
  compact: true
}), {
  lazy: true,
  server: false,
  watch: [actionLimit, actionOffset, actionType, actionStatus]
})
const { data: portfolioData, refresh: refreshPortfolio, error: portfolioError } = useAsyncData('home-portfolio-paged', () => api.getPortfolio({
  limit: portfolioLimit.value,
  offset: portfolioOffset.value,
  symbol: portfolioSymbol.value.trim().toUpperCase(),
  status: portfolioStatus.value,
  search: portfolioSearch.value.trim(),
  compact: true
}), {
  lazy: true,
  server: false,
  watch: [portfolioLimit, portfolioOffset, portfolioStatus]
})

const summaryValues = computed(() => home.value?.summary || {})
const liveSignals = computed(() => signalRefresh.value?.signals || [])
const signalMeta = computed(() => asDict(signalRefresh.value?.meta?.signals))
const topActions = computed(() => [
  ...(actionsData.value?.top_action_recommendations || []),
  ...(actionsData.value?.action_recommendations || []),
  ...(actionsData.value?.alerts || [])
])
const actionMeta = computed(() => asDict(actionsData.value?.pagination?.action_recommendations || actionsData.value?.meta?.action_recommendations))
const actionQueueMeta = computed(() => {
  const topMeta = asDict(actionsData.value?.pagination?.top_action_recommendations || actionsData.value?.meta?.top_action_recommendations)
  const rowMeta = asDict(actionsData.value?.pagination?.action_recommendations || actionsData.value?.meta?.action_recommendations)
  const alertMeta = asDict(actionsData.value?.pagination?.alerts || actionsData.value?.meta?.alerts)
  const returned = Number(topMeta.returned_count ?? topMeta.returned ?? 0) + Number(rowMeta.returned_count ?? rowMeta.returned ?? 0) + Number(alertMeta.returned_count ?? alertMeta.returned ?? 0)
  const total = Number(topMeta.total_count ?? topMeta.total ?? 0) + Number(rowMeta.total_count ?? rowMeta.total ?? 0) + Number(alertMeta.total_count ?? alertMeta.total ?? 0)
  return { returned, total }
})
const portfolioMeta = computed(() => asDict(portfolioData.value?.pagination?.portfolio || portfolioData.value?.meta?.portfolio))
const today = computed(() => portfolioRowsForBucket(portfolioBucket.value))
const snapshotMeta = computed(() => asDict(home.value?.snapshot || actionsData.value?.snapshot || portfolioData.value?.snapshot))
const snapshotWarning = computed(() => asDict(home.value?.snapshot_warning || actionsData.value?.snapshot_warning || portfolioData.value?.snapshot_warning))
const marketSummary = computed(() => marketContext.value?.summary || {})
const marketLeaders = computed(() => marketContext.value?.top_universe || [])
const calibrationSummary = computed(() => technicalCalibration.value?.summary || [])
const symbolTraces = reactive<Record<string, TraceSummary>>({})
const loadingSymbolTrace = reactive<Record<string, boolean>>({})
const portfolioBuckets = [
  { key: 'today_recommendations', label: 'Today' },
  { key: 'current_recommendations', label: 'Current' },
  { key: 'portfolio', label: 'Portfolio' },
  { key: 'lifecycle', label: 'Lifecycle' },
  { key: 'exited_recommendations', label: 'Exited' }
]

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function portfolioRowsForBucket(bucket: string): Dict[] {
  const payload = portfolioData.value as Record<string, unknown> | null | undefined
  const rows = payload?.[bucket]
  return Array.isArray(rows) ? rows.filter((row) => typeof row === 'object' && row !== null) as Dict[] : []
}

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 10) / 10}%`
}

function numberText(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return String(value || '-')
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 }).format(num)
}

function priceText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function actionLabel(row: Record<string, unknown>) {
  return String(
    row.action_code
    || row.action
    || row.next_action
    || row.reason
    || row.status
    || row.action_status
    || row.alert_type
    || row.source_type
    || (row.last_price || row.attractive_price_low || row.invalidation_price ? 'ALERT' : null)
    || 'NO_ACTION'
  ).toUpperCase()
}

function actionLane(row: Record<string, unknown>) {
  const label = actionLabel(row).toLowerCase()
  const detail = String([
    row.reason_detail,
    row.action_reason,
    row.next_action_reason,
    row.exit_strategy,
    row.action_summary
  ].filter(Boolean).join(' ')).toLowerCase()
  if (label.includes('sell') || label.includes('exit') || label.includes('trim') || label.includes('partial')) {
    return 'Executable exit / profit action'
  }
  if (label.includes('buy') || label.includes('add')) {
    return 'Executable entry action'
  }
  if (label.includes('alert') || label.includes('hit') || label.includes('breakout')) {
    return 'Watcher alert'
  }
  if (label.includes('manual') || label.includes('review')) {
    if (detail.includes('market context') || detail.includes('risk-off') || detail.includes('risk_off') || detail.includes('broad market')) {
      return 'Manual review: market regime gate'
    }
    if (detail.includes('missing entry/current price') || detail.includes('missing price') || detail.includes('missing data')) {
      return 'Manual review: data/price issue'
    }
    return 'Manual review: event/operator judgment'
  }
  if (label.includes('watch') || label.includes('hold')) {
    return 'Watch / hold'
  }
  return 'Other'
}

const actionLaneCounts = computed(() => {
  const counts: Record<string, number> = {}
  for (const row of topActions.value) {
    const lane = actionLane(row)
    counts[lane] = (counts[lane] || 0) + 1
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1])
})

const actionEmptyHint = computed(() => {
  if (topActions.value.length) return ''
  const action = actionType.value.toUpperCase()
  const status = actionStatus.value.toLowerCase()
  if (action === 'BUY' && status === 'approved') {
    return 'No approved BUY rows are available right now. The current market-regime gate is blocking positive broker actions, so buy candidates remain manual review or watch until breadth/regime improves.'
  }
  if (status === 'approved') {
    return 'Approved means executable or broker-order style rows. If this is empty, the current filtered lane has no executable action.'
  }
  return 'Try clearing filters or inspect Manual / Watch lanes; the queue may be blocked by market regime, event-policy review, or data/price issues.'
})

async function loadSymbolTrace(row: Record<string, unknown>) {
  const symbol = String(row.symbol || '').toUpperCase()
  if (!symbol || symbolTraces[symbol] || loadingSymbolTrace[symbol]) return
  loadingSymbolTrace[symbol] = true
  try {
    symbolTraces[symbol] = await api.getSymbolTraceSummary(symbol, 100)
  } finally {
    loadingSymbolTrace[symbol] = false
  }
}

async function applyActionFilters() {
  actionOffset.value = 0
  await refreshActions()
}

async function applyPortfolioFilters() {
  portfolioOffset.value = 0
  await refreshPortfolio()
}

function loadNextActions() {
  const next = Number(actionMeta.value.next_offset)
  if (Number.isFinite(next)) actionOffset.value = next
}

function loadNextPortfolio() {
  const next = Number(portfolioMeta.value.next_offset)
  if (Number.isFinite(next)) portfolioOffset.value = next
}

function bucketCount(bucket: string) {
  const meta = asDict(portfolioData.value?.pagination?.[bucket] || portfolioData.value?.meta?.[bucket])
  return Number(meta.total_count ?? meta.total ?? portfolioRowsForBucket(bucket).length)
}

function actionDetailPath(row: Record<string, unknown>) {
  const params = new URLSearchParams()
  if (row.symbol) params.set('symbol', String(row.symbol))
  if (row.unique_id) params.set('unique_id', String(row.unique_id))
  if (row.setup_id) params.set('setup_id', String(row.setup_id))
  const query = params.toString()
  return `/api/actions/detail${query ? `?${query}` : ''}`
}

function nestedValue(source: unknown, path: string[]): unknown {
  let current = source
  for (const key of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return null
    current = (current as Dict)[key]
  }
  return current
}

function firstActionValue(row: Record<string, unknown>, keys: string[], nestedPaths: string[][] = []) {
  for (const key of keys) {
    const value = row[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  for (const path of nestedPaths) {
    const value = nestedValue(row, path)
    if (value !== null && value !== undefined && value !== '') return value
  }
  return null
}

function actionPriceFacts(row: Record<string, unknown>) {
  const latest = firstActionValue(row, ['current_price', 'last_price', 'reference_price'], [
    ['recommendation_reason', 'evidence', 'risk', 'reference_price']
  ])
  const trigger = firstActionValue(row, ['trigger_price', 'pivot_price'], [
    ['recommendation_reason', 'evidence', 'technical', 'trigger_price'],
    ['recommendation_reason', 'evidence', 'technical', 'pivot_price']
  ])
  const zoneLow = firstActionValue(row, ['attractive_price_low'])
  const zoneHigh = firstActionValue(row, ['attractive_price_high'])
  const entry = firstActionValue(row, ['entry_price'])
  const stop = firstActionValue(row, ['stop_price', 'recommended_stop_price', 'invalidation_price'], [
    ['recommendation_reason', 'evidence', 'risk', 'stop_price'],
    ['recommendation_reason', 'evidence', 'risk', 'recommended_stop_price'],
    ['recommendation_reason', 'evidence', 'risk', 'invalidation_price']
  ])
  const target = firstActionValue(row, ['target_price', 'recommended_target_price'])
  const facts = [
    { label: 'Latest', value: latest, tone: 'bg-ink text-paper' },
    { label: 'Trigger', value: trigger, tone: 'bg-white text-ink/75' },
    { label: 'Entry', value: entry, tone: 'bg-white text-ink/75' },
    { label: 'Stop', value: stop, tone: 'bg-rust/10 text-rust' },
    { label: 'Target', value: target, tone: 'bg-moss/10 text-moss' }
  ].filter((item) => item.value !== null && item.value !== undefined && item.value !== '')
  if (zoneLow !== null && zoneLow !== undefined && zoneLow !== '' && zoneHigh !== null && zoneHigh !== undefined && zoneHigh !== '') {
    facts.splice(1, 0, { label: 'Zone', value: `${priceText(zoneLow)} - ${priceText(zoneHigh)}`, tone: 'bg-sun/20 text-ink' })
  }
  return facts
}

function technicalEvidence(row: Record<string, unknown>) {
  const technical = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'technical']))
  const risk = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'risk']))
  const screener = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'screener']))
  return {
    state: firstActionValue(row, ['technical_state'], [['recommendation_reason', 'evidence', 'technical', 'technical_state']]),
    trigger: firstActionValue(row, ['technical_trigger_type', 'entry_type', 'trigger_type'], [['recommendation_reason', 'evidence', 'technical', 'technical_trigger_type']]),
    score: firstActionValue(row, ['technical_score', 'setup_score'], [['recommendation_reason', 'evidence', 'technical', 'technical_score'], ['recommendation_reason', 'evidence', 'technical', 'setup_score']]),
    pivot: firstActionValue(row, ['pivot_price', 'trigger_price'], [['recommendation_reason', 'evidence', 'technical', 'pivot_price'], ['recommendation_reason', 'evidence', 'technical', 'trigger_price']]),
    stop: firstActionValue(row, ['recommended_stop_price', 'stop_price', 'invalidation_price'], [['recommendation_reason', 'evidence', 'risk', 'recommended_stop_price'], ['recommendation_reason', 'evidence', 'risk', 'stop_price'], ['recommendation_reason', 'evidence', 'risk', 'invalidation_price']]),
    target: firstActionValue(row, ['recommended_target_price', 'target_price'], [['recommendation_reason', 'evidence', 'risk', 'recommended_target_price']]),
    setup: firstActionValue(row, ['setup_name', 'setup_id'], [['recommendation_reason', 'setup_id']]),
    screener: firstActionValue(row, ['source_screener_slug', 'screener_slug'], [['recommendation_reason', 'evidence', 'screener', 'source_screener_slug']]),
    note: firstActionValue(row, ['technical_trigger_note', 'entry_note', 'watch_reason_detail', 'action_detail', 'reason_detail']),
    hasData: Object.keys(technical).length > 0 || Object.keys(risk).length > 0 || Object.keys(screener).length > 0 || Boolean(row.technical_state || row.technical_score || row.setup_score)
  }
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item || '').trim()).filter(Boolean)
}

function manualRevisionPointers(row: Record<string, unknown>) {
  return asDict(row.manual_revision_pointers)
}

function actionExplanation(row: Record<string, unknown>) {
  const pointers = manualRevisionPointers(row)
  const contract = asDict(row.recommendation_reason)
  const status = String(row.reason_contract_status || contract.status || '').toLowerCase()
  const action = actionLabel(row)
  const original = String(contract.original_action_code || contract.original_action || '').toUpperCase()
  const isFinalApproved = ['BUY', 'BUY_MORE', 'SELL', 'PARTIAL_SELL'].includes(action) && !status.includes('incomplete') && !status.includes('downgrade')
  const missingFields = stringArray(contract.missing_fields)
  const keyReasons = stringArray(pointers.key_reasons)
  const checks = stringArray(pointers.manual_checks)
  const riskFlags = stringArray(pointers.risk_flags)
  const questions = stringArray(pointers.operator_questions)
  const summary = String(row.manual_revision_summary || pointers.revision_summary || '').trim()
  let title = 'Operator checks before action'
  if (!isFinalApproved) title = original && original !== action ? `Why not approved ${original}` : 'Why not approved'
  if (action.includes('MANUAL') || action.includes('REVIEW')) title = original && original !== action ? `Why manual review instead of ${original}` : 'Why manual review'
  return {
    title,
    summary,
    missingFields,
    keyReasons,
    checks,
    riskFlags,
    questions,
    hasData: Boolean(summary || missingFields.length || keyReasons.length || checks.length || riskFlags.length || questions.length)
  }
}

function companyMemoryReview(row: Record<string, unknown>) {
  const review = asDict(row.company_memory_review || nestedValue(row, ['recommendation_reason', 'evidence', 'company_memory']))
  const riskFlags = stringArray(review.risk_flags)
  const evidenceUsed = stringArray(review.evidence_used)
  const waitFor = stringArray(review.wait_for)
  return {
    hasData: Object.keys(review).length > 0,
    signal: String(review.recommended_signal || '').toUpperCase(),
    confidence: review.confidence,
    conviction: review.conviction_score,
    summary: String(review.summary || '').trim(),
    thesis: String(review.thesis || '').trim(),
    authority: String(review.authority_scope || 'review_input_only'),
    status: String(review.review_status || '').trim(),
    reviewDate: String(review.review_date || '').trim(),
    riskFlags,
    evidenceUsed,
    waitFor
  }
}

function actionSourceLabel(row: Record<string, unknown>) {
  return firstActionValue(row, ['action_source', 'source'], [['recommendation_reason', 'action_source']])
}

function actionSourceAction(row: Record<string, unknown>) {
  return firstActionValue(row, ['source_action'], [['recommendation_reason', 'source_action']])
}

function actionPillTitle(row: Record<string, unknown>) {
  const action = actionLabel(row)
  const execution = firstActionValue(row, ['execution_mode'], [['recommendation_reason', 'execution_mode']]) || 'unknown'
  const transaction = firstActionValue(row, ['transaction_type'], [['recommendation_reason', 'transaction_type']]) || 'none'
  return `Final consolidated action: ${action}. Execution mode: ${execution}. Transaction type: ${transaction}. This is the action queue decision after portfolio, lifecycle, watchlist, event, and conflict rules are consolidated.`
}

function lanePillTitle(row: Record<string, unknown>) {
  return `Queue lane: ${actionLane(row)}. This explains why the row is grouped here; it is not the same as broker approval or portfolio membership.`
}

function sourceTitle(row: Record<string, unknown>) {
  const source = actionSourceLabel(row) || 'unknown'
  if (source === 'portfolio') return 'Source: portfolio engine. The row originated from an approved/deferred portfolio candidate, but later gates may still downgrade it to manual review.'
  if (source === 'rebalance') return 'Source: rebalance/lifecycle engine. Usually an exit, trim, stop, or manual-review action for an existing or previously approved idea; it is not a new portfolio approval.'
  if (source === 'watchlist') return 'Source: watchlist. This is monitoring only; it is not an approved trade.'
  if (source === 'event_policy') return 'Source: event policy. This is an event-risk or event-opportunity overlay, usually requiring manual review.'
  if (String(source).startsWith('playbook')) return 'Source: hypothesis/playbook. This is an LLM/playbook overlay, not direct broker execution.'
  return `Source: ${source}. This identifies the subsystem that produced the winning action candidate.`
}

function sourceActionTitle(row: Record<string, unknown>) {
  const source = actionSourceLabel(row) || 'unknown'
  const sourceAction = actionSourceAction(row) || 'unknown'
  if (sourceAction === 'approved' && source !== 'portfolio') {
    return `Source action: ${sourceAction}. This label came from the source subsystem, but because source is ${source}, do not read it as portfolio-engine approval.`
  }
  if (sourceAction === 'approved') {
    return 'Source action: approved by the portfolio engine before action consolidation. Market/regime gates or conflict rules may still turn the final action into manual review.'
  }
  return `Source action: ${sourceAction}. This is the original action emitted by ${source} before final consolidation.`
}

function setupTitle(row: Record<string, unknown>) {
  const setup = row.setup_id || nestedValue(row, ['recommendation_reason', 'setup_id']) || 'unknown'
  return `Setup/strategy id: ${setup}. Multiple setups can nominate the same symbol; the action queue keeps one final action per symbol after conflict resolution.`
}

function reasonContractTitle(row: Record<string, unknown>) {
  const status = row.reason_contract_status || nestedValue(row, ['recommendation_reason', 'status']) || 'unknown'
  return `Reason contract status: ${status}. Complete means the explanation has required fields; it does not mean the trade is approved.`
}

function executionSafetyGate(row: Record<string, unknown>) {
  const contract = asDict(row.execution_safety_contract)
  const issues = Array.isArray(contract.issues) ? contract.issues.map((item) => String(item || '').trim()).filter(Boolean) : []
  return {
    hasData: Object.keys(contract).length > 0,
    approvalRequired: contract.operator_approval_required === true,
    approvalStatus: String(contract.operator_approval_status || 'missing'),
    reconciliationRequired: contract.broker_reconciliation_required === true,
    reconciliationStatus: String(contract.broker_reconciliation_status || 'not_run'),
    liveAllowed: contract.live_submission_allowed === true,
    source: String(contract.source || 'execution_plan'),
    issues
  }
}

function executionGateTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const gate = executionSafetyGate(row)
  if (!gate.hasData) return 'neutral'
  if (gate.liveAllowed) return 'success'
  if (gate.approvalRequired || gate.reconciliationRequired || gate.issues.length) return 'warning'
  return 'info'
}

function scoreBadge(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return num <= 1 ? `${numberText(num * 100)} / 100` : `${numberText(num)} / 100`
}

function actionPillTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const label = actionLabel(row).toLowerCase()
  if (label.includes('sell') || label.includes('exit') || label.includes('reduce')) return 'danger'
  if (label.includes('buy') || label.includes('add')) return 'success'
  if (label.includes('manual') || label.includes('review')) return 'warning'
  if (label.includes('watch') || label.includes('alert')) return 'info'
  if (label.includes('hold')) return 'dark'
  return 'neutral'
}

function lanePillTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const lane = actionLane(row).toLowerCase()
  if (lane.includes('exit')) return 'danger'
  if (lane.includes('entry')) return 'success'
  if (lane.includes('market') || lane.includes('manual')) return 'warning'
  if (lane.includes('alert') || lane.includes('watch')) return 'info'
  if (lane.includes('data')) return 'danger'
  return 'neutral'
}

function portfolioDetailPath(row: Record<string, unknown>) {
  const symbol = String(row.symbol || row.ticker || '').toUpperCase()
  return symbol ? `/api/portfolio/${encodeURIComponent(symbol)}/detail` : ''
}

function signalTone(row: Record<string, unknown>) {
  const status = String(row.signal_status || row.signal_action || '').toLowerCase()
  if (status.includes('exit') || status.includes('reduce') || status.includes('sell')) return 'bg-rust text-white'
  if (status.includes('entry') || status.includes('buy')) return 'bg-moss text-white'
  if (status.includes('review') || status.includes('watch')) return 'bg-sun text-ink'
  return 'bg-white text-ink/70'
}

function effectTone(row: Record<string, unknown>) {
  const effect = String(row.effect_type || '').toLowerCase()
  if (effect.includes('wait_match')) return 'bg-sun text-ink'
  if (effect.includes('action_changed')) return 'bg-moss text-white'
  return 'bg-paper text-ink/65'
}

function effectLabel(row: Record<string, unknown>) {
  const effect = String(row.effect_type || '').toLowerCase()
  if (effect === 'wait_match_created') return 'Wait match'
  if (effect === 'action_changed') return 'Action changed'
  if (effect === 'evidence_only') return 'Evidence only'
  return 'Effect unknown'
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Read-only operator view</p>
    <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">See actions first. Expand the evidence when something changes.</h1>
    <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
      Generated {{ home?.generated_at || '-' }} for {{ home?.asof_date || 'latest available date' }}.
    </p>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="snapshotMeta" :warning="snapshotWarning" :generated-at="home?.generated_at" />

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Actions" :value="String(summaryValues.action_count || 0)" note="Resolved buy/sell/review queue" />
    <MetricTile label="Today" :value="String(summaryValues.today_count || 0)" note="Latest recommendation date" />
    <MetricTile label="Watch" :value="String(summaryValues.watch_count || 0)" note="Waiting for trigger or confirmation" />
    <MetricTile label="Alerts" :value="String(summaryValues.alert_count || 0)" note="Live watcher alerts" />
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Live Signal Refresh</p>
        <h2 class="mt-2 text-2xl font-black">Watcher-triggered symbol decisions</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          These are fast symbol-scoped signals from OHLCV/news/announcement watcher routes. The daily advisory remains the authoritative reconciliation.
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ signalMeta.total || 0 }} recent
      </span>
    </div>
    <ApiErrorBanner v-if="signalRefreshError" class="mt-4" title="Live signal refresh failed" :error="signalRefreshError" />
    <div v-if="liveSignals.length" class="mt-5 grid gap-3 xl:grid-cols-3">
      <article v-for="row in liveSignals" :key="String(row.refresh_id || `${row.symbol}-${row.refreshed_at}`)" class="rounded-3xl bg-white/75 p-4 shadow-soft">
        <div class="flex items-start justify-between gap-3">
          <div>
            <SymbolLink :symbol="row.symbol" />
            <p class="mt-1 text-xs font-semibold text-ink/45">{{ row.refreshed_at || row.load_ts || '-' }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="signalTone(row)">
            {{ row.signal_action || 'NO_CHANGE' }}
          </span>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/70">{{ row.action_reason || 'No reason captured.' }}</p>
        <div class="mt-3 rounded-2xl bg-paper/80 p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="effectTone(row)">
              {{ effectLabel(row) }}
            </span>
            <span class="text-xs font-bold text-ink/45">watcher effect</span>
          </div>
          <p class="mt-2 text-xs font-semibold leading-5 text-ink/60">
            {{ row.effect_summary || 'No watcher effect summary captured.' }}
          </p>
        </div>
        <div class="mt-3 flex flex-wrap gap-2 text-xs font-bold text-ink/50">
          <span class="rounded-full bg-paper px-3 py-1">status: {{ row.signal_status || '-' }}</span>
          <span class="rounded-full bg-paper px-3 py-1">source: {{ row.signal_source || '-' }}</span>
          <span class="rounded-full bg-paper px-3 py-1">reason: {{ row.reason || '-' }}</span>
        </div>
      </article>
    </div>
    <p v-else class="mt-5 rounded-2xl bg-white/70 p-4 text-sm font-semibold text-ink/55">
      No live signal-refresh rows yet. Run `./all_watchers.sh` or `python -m advisory.signal_refresh --symbol RELIANCE --reason manual`.
    </p>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Market Context: Top 50%</p>
        <h2 class="mt-2 text-2xl font-black">Broad tape before stock-specific conviction</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          {{ marketSummary.summary_text || 'No market-context summary has been generated yet. Run the advisory pipeline through the market_context stage.' }}
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ marketSummary.regime_name || 'UNKNOWN' }}
      </span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-5">
      <MetricTile label="Top Names" :value="String(marketSummary.top_context_count || 0)" note="Tracked context universe" />
      <MetricTile label="Above 50DMA" :value="pct(marketSummary.breadth_above_dma50_pct)" note="Breadth inside top context" />
      <MetricTile label="Trend Aligned" :value="pct(marketSummary.breadth_trend_alignment_pct)" note="Healthy leadership share" />
      <MetricTile label="RS Positive" :value="pct(marketSummary.breadth_rs_positive_pct)" note="Outperforming benchmark" />
      <MetricTile label="Events" :value="String((Number(marketSummary.news_event_count_7d || 0) + Number(marketSummary.announcement_event_count_7d || 0)) || 0)" note="News + announcements, 7d" />
    </div>
    <details class="mt-5">
      <summary class="cursor-pointer text-sm font-black text-moss">Show top market-context leaders</summary>
      <div class="mt-3 grid gap-3 lg:grid-cols-2">
        <article v-for="row in marketLeaders.slice(0, 10)" :key="String(row.symbol)" class="rounded-2xl bg-white/70 p-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <SymbolLink :symbol="row.symbol" />
              <p class="text-xs text-ink/50">{{ row.sector_code || 'sector n/a' }} · rank {{ row.context_rank || '-' }}</p>
            </div>
            <span class="rounded-full bg-moss px-3 py-1 text-xs font-bold text-white">
              {{ numberText(Number(row.context_rank_score || 0) * 100) }}
            </span>
          </div>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Liquidity:</b> {{ numberText(row.avg_traded_value_20d) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>RS:</b> {{ numberText(Number(row.rs_vs_benchmark || 0) * 100) }}%</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Events:</b> {{ Number(row.news_event_count_7d || 0) + Number(row.announcement_event_count_7d || 0) }}</p>
          </div>
        </article>
      </div>
    </details>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
    <div>
      <div class="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 class="text-xl font-black">Action Queue</h2>
          <p class="mt-1 text-sm font-semibold text-ink/50">
            Showing {{ actionQueueMeta.returned || topActions.length }} of {{ actionQueueMeta.total || topActions.length }} action rows.
          </p>
        </div>
      </div>
      <ApiErrorBanner v-if="actionsError" class="mb-3" title="Action queue failed" :error="actionsError" />
      <div class="mb-4 glass-panel rounded-3xl p-4">
        <div class="grid gap-3 md:grid-cols-[0.75fr_0.75fr_0.75fr_1fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Symbol
            <input v-model="actionSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyActionFilters" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Action
            <select v-model="actionType" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="ALL">All</option>
              <option value="BUY">Buy</option>
              <option value="SELL">Sell</option>
              <option value="EXIT">Exit</option>
              <option value="HOLD">Hold</option>
              <option value="WATCH">Watch</option>
              <option value="MANUAL">Manual</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Status
            <select v-model="actionStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="all">All</option>
              <option value="error">Error</option>
              <option value="manual">Manual</option>
              <option value="approved">Approved</option>
              <option value="blocked">Blocked</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Search
            <input v-model="actionSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="reason, setup, event..." @keyup.enter="applyActionFilters" />
          </label>
          <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="applyActionFilters">
            Apply
          </button>
        </div>
      </div>
      <div class="space-y-3">
        <div v-if="actionLaneCounts.length" class="flex flex-wrap gap-2">
          <MetaChip
            v-for="[lane, count] in actionLaneCounts"
            :key="lane"
            label="lane"
            tone="blue"
          >
            {{ lane }} · {{ count }}
          </MetaChip>
        </div>
        <RecordCard
          v-for="(row, idx) in topActions"
          :key="idx"
          :title="`${String(row.symbol || row.ticker || 'UNKNOWN').toUpperCase()} · Final action: ${actionLabel(row)}`"
          :subtitle="String(row.action_reason || row.next_action_reason || row.reason || '')"
          :record="row"
          :detail-path="actionDetailPath(row)"
          :show-symbol="false"
        >
          <template #badge>
            <div class="flex flex-wrap gap-2">
              <StatusPill :tone="actionPillTone(row)" :title="actionPillTitle(row)">{{ actionLabel(row) }}</StatusPill>
              <StatusPill :tone="lanePillTone(row)" :title="lanePillTitle(row)">{{ actionLane(row) }}</StatusPill>
            </div>
          </template>
          <div class="mt-4 flex flex-wrap gap-2">
            <MetaChip v-if="actionSourceLabel(row)" label="source" tone="blue" :title="sourceTitle(row)">{{ actionSourceLabel(row) }}</MetaChip>
            <MetaChip v-if="actionSourceAction(row)" label="source action" tone="yellow" :title="sourceActionTitle(row)">{{ actionSourceAction(row) }}</MetaChip>
            <MetaChip v-if="row.setup_id" label="setup" :title="setupTitle(row)">{{ row.setup_id }}</MetaChip>
            <MetaChip v-if="row.reason_contract_status" label="reason" :title="reasonContractTitle(row)" :tone="String(row.reason_contract_status).includes('complete') ? 'green' : 'yellow'">{{ row.reason_contract_status }}</MetaChip>
            <MetaChip v-if="executionSafetyGate(row).hasData" label="execution gate" :tone="executionSafetyGate(row).liveAllowed ? 'green' : 'yellow'">
              {{ executionSafetyGate(row).liveAllowed ? 'live allowed' : 'approval/reconcile required' }}
            </MetaChip>
          </div>
          <section
            v-if="executionSafetyGate(row).hasData"
            class="mt-4 rounded-2xl border border-rust/20 bg-rust/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">Execution approval gate</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  Dry-run preview only. This panel does not approve, reconcile, or submit broker orders.
                </p>
              </div>
              <StatusPill :tone="executionGateTone(row)">
                {{ executionSafetyGate(row).liveAllowed ? 'LIVE ALLOWED' : 'LIVE BLOCKED' }}
              </StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Approval:</b> {{ executionSafetyGate(row).approvalStatus }}
                <span class="text-ink/45">({{ executionSafetyGate(row).approvalRequired ? 'required' : 'not required' }})</span>
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Reconciliation:</b> {{ executionSafetyGate(row).reconciliationStatus }}
                <span class="text-ink/45">({{ executionSafetyGate(row).reconciliationRequired ? 'required' : 'not required' }})</span>
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Source:</b> {{ executionSafetyGate(row).source }}</p>
            </div>
            <ul v-if="executionSafetyGate(row).issues.length" class="mt-3 space-y-1 text-sm leading-6 text-ink/70">
              <li v-for="issue in executionSafetyGate(row).issues.slice(0, 3)" :key="issue">{{ issue }}</li>
            </ul>
          </section>
          <div class="mt-4 grid gap-2 text-xs font-black sm:grid-cols-2 xl:grid-cols-3">
            <p
              v-for="fact in actionPriceFacts(row)"
              :key="`${fact.label}-${String(fact.value)}`"
              class="rounded-2xl px-3 py-2"
              :class="fact.tone"
            >
              <span class="block uppercase tracking-[0.18em] opacity-60">{{ fact.label }}</span>
              <span class="mt-1 block text-sm">{{ fact.label === 'Zone' ? fact.value : priceText(fact.value) }}</span>
            </p>
            <p v-if="row.pnl_pct !== null && row.pnl_pct !== undefined && row.pnl_pct !== ''" class="rounded-2xl bg-white px-3 py-2 text-ink/75">
              <span class="block uppercase tracking-[0.18em] opacity-60">P&L</span>
              <span class="mt-1 block text-sm">{{ pct(row.pnl_pct) }}</span>
            </p>
          </div>
          <section
            v-if="technicalEvidence(row).hasData"
            class="mt-4 rounded-2xl border border-sky/20 bg-sky/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-sky">Why technical</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  {{ technicalEvidence(row).note || 'Technical evidence contributed to this action queue item.' }}
                </p>
              </div>
              <StatusPill tone="info">{{ technicalEvidence(row).state || technicalEvidence(row).trigger || 'TECHNICAL' }}</StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Trigger:</b> {{ technicalEvidence(row).trigger || '-' }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Score:</b> {{ scoreBadge(technicalEvidence(row).score) }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Setup:</b> {{ technicalEvidence(row).setup || '-' }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Screener:</b> {{ technicalEvidence(row).screener || '-' }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Pivot:</b> {{ priceText(technicalEvidence(row).pivot) }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Stop:</b> {{ priceText(technicalEvidence(row).stop) }}</p>
            </div>
          </section>
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <section
            v-if="companyMemoryReview(row).hasData"
            class="mt-4 rounded-2xl border border-moss/25 bg-moss/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Company memory review</p>
                <p v-if="companyMemoryReview(row).summary" class="mt-2 text-sm font-semibold leading-6 text-ink/75">
                  {{ companyMemoryReview(row).summary }}
                </p>
                <p v-if="companyMemoryReview(row).thesis" class="mt-1 text-sm leading-6 text-ink/60">
                  {{ companyMemoryReview(row).thesis }}
                </p>
              </div>
              <StatusPill tone="info">{{ companyMemoryReview(row).signal || 'REVIEW' }}</StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Confidence:</b> {{ pct(companyMemoryReview(row).confidence) }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Conviction:</b> {{ scoreBadge(companyMemoryReview(row).conviction) }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Status:</b> {{ companyMemoryReview(row).status || '-' }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Authority:</b> {{ companyMemoryReview(row).authority }}</p>
            </div>
            <div class="mt-3 grid gap-3 md:grid-cols-3">
              <div v-if="companyMemoryReview(row).evidenceUsed.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Evidence used</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in companyMemoryReview(row).evidenceUsed.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="companyMemoryReview(row).riskFlags.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Risk flags</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in companyMemoryReview(row).riskFlags.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="companyMemoryReview(row).waitFor.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Wait for</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in companyMemoryReview(row).waitFor.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
            </div>
          </section>
          <section
            v-if="actionExplanation(row).hasData"
            class="mt-4 rounded-2xl border border-sun/40 bg-sun/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">{{ actionExplanation(row).title }}</p>
                <p v-if="actionExplanation(row).summary" class="mt-2 text-sm font-semibold leading-6 text-ink/75">
                  {{ actionExplanation(row).summary }}
                </p>
              </div>
              <StatusPill v-if="row.manual_revision_status" tone="warning">{{ row.manual_revision_status }}</StatusPill>
            </div>
            <div v-if="actionExplanation(row).missingFields.length" class="mt-3 flex flex-wrap gap-2">
              <MetaChip v-for="field in actionExplanation(row).missingFields" :key="field" label="missing" tone="yellow">
                {{ field }}
              </MetaChip>
            </div>
            <div class="mt-3 grid gap-3 md:grid-cols-2">
              <div v-if="actionExplanation(row).keyReasons.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Reasons</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in actionExplanation(row).keyReasons.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="actionExplanation(row).riskFlags.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Blockers / conflicts</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in actionExplanation(row).riskFlags.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="actionExplanation(row).checks.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Operator checks</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in actionExplanation(row).checks.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
              <div v-if="actionExplanation(row).questions.length" class="rounded-xl bg-white/80 p-3">
                <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Questions</p>
                <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
                  <li v-for="item in actionExplanation(row).questions.slice(0, 3)" :key="item">{{ item }}</li>
                </ul>
              </div>
            </div>
          </section>
          <TechnicalDecisionPanel class="mt-4" :record="row" compact />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <LinkButton
            v-if="row.symbol"
            class="ml-2"
            variant="secondary"
            :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open symbol page
          </LinkButton>
          <TraceTimeline
            v-if="symbolTraces[String(row.symbol || '').toUpperCase()]"
            class="mt-4"
            :trace="symbolTraces[String(row.symbol || '').toUpperCase()]"
            title="Symbol trace"
          />
        </RecordCard>
        <p v-if="!topActions.length" class="glass-panel rounded-3xl p-6 text-ink/60">
          {{ actionEmptyHint || 'No current action rows.' }}
        </p>
      </div>
      <button v-if="actionMeta.has_more" class="mt-4 rounded-full bg-moss px-5 py-3 text-sm font-black text-paper" type="button" @click="loadNextActions">
        Show next {{ actionLimit }} actions
      </button>
    </div>
    <div>
      <div class="mb-3">
        <h2 class="text-xl font-black">Portfolio & Recommendations</h2>
        <p class="mt-1 text-sm font-semibold text-ink/50">
          {{ portfolioBuckets.find((item) => item.key === portfolioBucket)?.label || 'Selected' }}: {{ today.length }} shown.
        </p>
      </div>
      <ApiErrorBanner v-if="portfolioError" class="mb-3" title="Portfolio payload failed" :error="portfolioError" />
      <div class="mb-4 glass-panel rounded-3xl p-4">
        <div class="flex flex-wrap gap-2">
          <button
            v-for="bucket in portfolioBuckets"
            :key="bucket.key"
            class="rounded-full px-3 py-2 text-xs font-black"
            :class="portfolioBucket === bucket.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60'"
            type="button"
            @click="portfolioBucket = bucket.key"
          >
            {{ bucket.label }} · {{ bucketCount(bucket.key) }}
          </button>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[0.75fr_0.75fr_1fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Symbol
            <input v-model="portfolioSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyPortfolioFilters" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Status
            <select v-model="portfolioStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="all">All</option>
              <option value="current">Current</option>
              <option value="exit">Exit</option>
              <option value="manual">Manual</option>
              <option value="watch">Watch</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Search
            <input v-model="portfolioSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="reason, bucket, exit..." @keyup.enter="applyPortfolioFilters" />
          </label>
          <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="applyPortfolioFilters">
            Apply
          </button>
        </div>
      </div>
      <div class="space-y-3">
        <RecordCard
          v-for="(row, idx) in today"
          :key="idx"
          :title="String(row.action_summary || row.kind || 'Recommendation')"
          :subtitle="String(row.reason || row.action_summary || row.bucket_summary || '')"
          :record="row"
          :detail-path="portfolioDetailPath(row)"
        >
          <template #badge>
            <SymbolLink :symbol="row.symbol" subtle />
          </template>
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <TechnicalDecisionPanel class="mt-4" :record="row" compact />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <LinkButton
            v-if="row.symbol"
            class="ml-2"
            variant="secondary"
            :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open symbol page
          </LinkButton>
          <TraceTimeline
            v-if="symbolTraces[String(row.symbol || '').toUpperCase()]"
            class="mt-4"
            :trace="symbolTraces[String(row.symbol || '').toUpperCase()]"
            title="Symbol trace"
          />
        </RecordCard>
        <p v-if="!today.length" class="glass-panel rounded-3xl p-6 text-ink/60">No rows for this portfolio bucket and filter.</p>
      </div>
      <button v-if="portfolioBucket === 'portfolio' && portfolioMeta.has_more" class="mt-4 rounded-full bg-moss px-5 py-3 text-sm font-black text-paper" type="button" @click="loadNextPortfolio">
        Show next {{ portfolioLimit }} portfolio rows
      </button>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Technical Calibration</p>
        <h2 class="mt-2 text-2xl font-black">Thresholds are research evidence, not auto-promoted</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Latest realized-outcome checks for the swing technical engine. Review sample size, hit rate after costs, and average return before changing setup thresholds.
        </p>
      </div>
      <LinkButton to="/technical-calibration">
        Open calibration
      </LinkButton>
      <LinkButton variant="ghost" to="/events">
        Open event policy
      </LinkButton>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-3">
      <article v-for="row in calibrationSummary.slice(0, 3)" :key="String(row.horizon_days)" class="rounded-2xl bg-white/70 p-4">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ row.horizon_days }} day horizon</p>
        <p class="mt-2 font-black text-ink">{{ row.best_config_id || 'No config yet' }}</p>
        <div class="mt-3 grid gap-2 text-sm">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Eligible:</b> {{ numberText(row.best_eligible_count) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Hit:</b> {{ pct(Number(row.best_hit_rate_after_cost || 0)) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Avg return:</b> {{ pct(Number(row.best_avg_forward_return_after_cost || 0)) }}</p>
        </div>
      </article>
      <p v-if="!calibrationSummary.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
        No calibration rows yet. Run the technical threshold calibration script.
      </p>
    </div>
  </section>
</template>
