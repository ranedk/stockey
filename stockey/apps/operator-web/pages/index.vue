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

const [{ data: marketContext }, { data: technicalCalibration }, { data: tsPromotionCheck }, { data: tsReviewRules }, { data: tsPromotionReviews }, { data: configApplications }, { data: signalRefresh, error: signalRefreshError }] = await Promise.all([
  useAsyncData('market-context', () => api.getMarketContext(20)),
  useAsyncData('technical-calibration-home', () => api.getTechnicalCalibration(3)),
  useAsyncData('ts-forecast-promotion-check-home', () => api.getTsForecastPromotionCheck()),
  useAsyncData('ts-forecast-review-rules-home', () => api.getTsForecastReviewRules()),
  useAsyncData('ts-forecast-promotion-reviews-home', () => api.getTsForecastPromotionReviews(6)),
  useAsyncData('config-change-applications-home', () => api.getConfigChangeApplications(6)),
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
  compact: true,
  include_feature_freshness: true
}), {
  lazy: true,
  server: false,
  watch: [actionLimit, actionOffset, actionType, actionStatus]
})
const { data: portfolioData, refresh: refreshPortfolio, error: portfolioError } = useAsyncData('home-portfolio-paged', () => api.getPortfolio({
  limit: portfolioLimit.value,
  offset: portfolioOffset.value,
  bucket: portfolioBucket.value,
  symbol: portfolioSymbol.value.trim().toUpperCase(),
  status: portfolioStatus.value,
  search: portfolioSearch.value.trim(),
  compact: true
}), {
  lazy: true,
  server: false,
  watch: [portfolioLimit, portfolioOffset, portfolioStatus, portfolioBucket]
})
const { data: identityIssuesData, error: identityIssuesError } = useAsyncData('home-identity-issues', () => api.getIdentityIssues({ limit: 200 }), {
  lazy: true,
  server: false
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
const payloadFreshnessItems = computed(() => [
  {
    label: 'Home',
    generated_at: home.value?.generated_at,
    snapshot: home.value?.snapshot,
    warning: home.value?.snapshot_warning,
    source: 'home'
  },
  {
    label: 'Actions',
    generated_at: actionsData.value?.generated_at,
    snapshot: actionsData.value?.snapshot,
    warning: actionsData.value?.snapshot_warning,
    source: 'actions'
  },
  {
    label: 'Portfolio',
    generated_at: portfolioData.value?.generated_at,
    snapshot: portfolioData.value?.snapshot,
    warning: portfolioData.value?.snapshot_warning,
    source: 'portfolio'
  },
  {
    label: 'Signal refresh',
    generated_at: signalRefresh.value?.generated_at,
    status: signalRefresh.value?.status,
    source: 'signal_refresh'
  }
])
const marketSummary = computed(() => marketContext.value?.summary || {})
const marketLeaders = computed(() => marketContext.value?.top_universe || [])
const calibrationSummary = computed(() => technicalCalibration.value?.summary || [])
const tsPaperSummary = computed(() => Array.isArray(home.value?.ts_forecast_paper_summary) ? home.value.ts_forecast_paper_summary : [])
const tsPromotionScorecard = computed(() => asDict(tsPromotionCheck.value?.scorecard))
const tsPromotionBestGroup = computed(() => asDict(tsPromotionScorecard.value.best_group))
const tsReviewRuleSummary = computed(() => asDict(tsReviewRules.value?.summary))
const tsReviewRuleIssues = computed(() => Array.isArray(tsReviewRules.value?.issues) ? tsReviewRules.value.issues : [])
const tsReviewRuleRows = computed(() => Array.isArray(tsReviewRules.value?.rules) ? tsReviewRules.value.rules : [])
const tsPromotionReviewRows = computed(() => Array.isArray(tsPromotionReviews.value?.reviews) ? tsPromotionReviews.value.reviews : [])
const configApplicationRows = computed(() => Array.isArray(configApplications.value?.applications) ? configApplications.value.applications : [])
const identityIssueRows = computed(() => Array.isArray(identityIssuesData.value?.issues) ? identityIssuesData.value.issues.filter((row) => typeof row === 'object' && row !== null) as Dict[] : [])
const identityIssuesBySymbol = computed(() => {
  const grouped: Record<string, Dict[]> = {}
  for (const row of identityIssueRows.value) {
    const symbol = normalizeSymbol(row.symbol)
    if (!symbol) continue
    grouped[symbol] = grouped[symbol] || []
    grouped[symbol].push(row)
  }
  return grouped
})
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

function titleLabel(value: unknown) {
  const text = String(value || '').replaceAll('_', ' ')
  return text ? text.replace(/\b\w/g, (char) => char.toUpperCase()) : '-'
}

function humanRouterLabel(value: unknown) {
  const text = String(value || '').replaceAll('_', ' ').toLowerCase()
  return text ? text.replace(/\b\w/g, (char) => char.toUpperCase()) : '-'
}

function normalizeSymbol(value: unknown) {
  return String(value || '').trim().toUpperCase()
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
  const sourceContract = asDict(review.evidence_source_contract)
  const missingSources = stringArray(sourceContract.missing_required_sources)
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
    waitFor,
    sourceContract,
    missingSources
  }
}

function featureFreshnessSummary(row: Record<string, unknown>) {
  const summary = asDict(row.feature_freshness_summary)
  const counts = asDict(summary.counts)
  const blockers = Array.isArray(summary.blockers) ? summary.blockers.filter((item) => typeof item === 'object' && item !== null) as Record<string, unknown>[] : []
  const requiredInputs = Array.isArray(summary.required_inputs) ? summary.required_inputs.filter((item) => typeof item === 'object' && item !== null) as Record<string, unknown>[] : []
  return {
    hasData: Object.keys(summary).length > 0,
    status: String(summary.status || 'unknown'),
    counts,
    blockers,
    requiredInputs
  }
}

function featureFreshnessTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const status = featureFreshnessSummary(row).status.toLowerCase()
  if (status === 'ok') return 'success'
  if (status === 'blocked') return 'danger'
  if (status === 'warning') return 'warning'
  return 'neutral'
}

function sourceIssueSummary(row: Record<string, unknown>) {
  const symbol = normalizeSymbol(row.symbol || row.ticker)
  const issues = symbol ? identityIssuesBySymbol.value[symbol] || [] : []
  const first = issues[0] || {}
  const issueType = String(first.issue_type || 'identity/source issue')
  const exchange = String(first.requested_exchange || '').trim()
  const error = String(first.error_text || '').trim()
  const suggestedAction = String(first.suggested_action || first.repair_hint || '').trim()
  const source = String(first.source || '').trim()
  const lastSeen = String(first.last_seen_at || first.load_ts || '').trim()
  const blockers = issues.map((issue) => {
    const kind = String(issue.issue_type || 'source issue').replaceAll('_', ' ')
    const issueExchange = String(issue.requested_exchange || '').trim()
    const issueError = String(issue.error_text || '').trim()
    return `${titleLabel(kind)}${issueExchange ? ` (${issueExchange})` : ''}${issueError ? `: ${issueError}` : ''}`
  })
  return {
    hasData: issues.length > 0,
    symbol,
    count: issues.length,
    issueType,
    exchange,
    error,
    suggestedAction,
    source,
    lastSeen,
    blockers
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

function actionQueueContract(row: Record<string, unknown>) {
  return asDict(row.action_queue_contract)
}

function queueStatusLabel(row: Record<string, unknown>) {
  const contract = actionQueueContract(row)
  const status = String(contract.display_status || '').replaceAll('_', ' ')
  return status ? titleLabel(status) : ''
}

function queueStatusTitle(row: Record<string, unknown>) {
  const contract = actionQueueContract(row)
  return String(contract.operator_note || 'Action Queue status is derived from the final consolidated action and action-transition contract, not only from upstream source labels.')
}

function queueStatusTone(row: Record<string, unknown>): 'green' | 'yellow' | 'red' | 'blue' | undefined {
  const status = String(actionQueueContract(row).display_status || '').toLowerCase()
  if (status === 'broker_candidate') return 'green'
  if (status === 'review_only') return 'yellow'
  if (status === 'blocked') return 'red'
  if (status === 'watch_hold') return 'blue'
  return undefined
}

function finalStateTrust(row: Record<string, unknown>) {
  const trust = asDict(row.final_state_trust)
  const checks = Array.isArray(trust.checks)
    ? trust.checks.filter((item) => typeof item === 'object' && item !== null) as Record<string, unknown>[]
    : []
  const blockers = stringArray(trust.blockers)
  const warnings = stringArray(trust.warnings)
  return {
    hasData: Object.keys(trust).length > 0,
    status: String(trust.status || '').trim(),
    finalAction: String(trust.final_action || actionLabel(row)).toUpperCase(),
    displayStatus: String(trust.display_status || '').replaceAll('_', ' '),
    brokerCandidate: trust.broker_order_candidate === true,
    approvedFilterMatch: trust.approved_filter_match === true,
    liveSubmissionAllowed: trust.live_submission_allowed === true,
    checks,
    blockers,
    warnings,
    operatorNote: String(trust.operator_note || '').trim()
  }
}

function trustStatusTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const status = finalStateTrust(row).status.toLowerCase()
  if (status === 'blocked') return 'danger'
  if (status === 'execution_preview_required') return 'info'
  if (status === 'manual_review') return 'warning'
  if (status === 'monitoring') return 'neutral'
  return 'dark'
}

function trustCheckTone(check: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const status = String(check.status || '').toLowerCase()
  if (status === 'passed') return 'success'
  if (status === 'blocked') return 'danger'
  if (status === 'warning') return 'warning'
  return 'neutral'
}

function executionSafetyGate(row: Record<string, unknown>) {
  const contract = asDict(row.execution_safety_contract)
  const issues = Array.isArray(contract.issues) ? contract.issues.map((item) => String(item || '').trim()).filter(Boolean) : []
  const transition = asDict(contract.portfolio_transition_contract || row.state_transition_contract || row.state_transition_contract_json)
  const transitionIssues = issues.filter((issue) => {
    const text = issue.toLowerCase()
    return text.includes('portfolio') || text.includes('position_state') || text.includes('transition contract') || text.includes('entry evidence')
  })
  return {
    hasData: Object.keys(contract).length > 0,
    approvalRequired: contract.operator_approval_required === true,
    approvalStatus: String(contract.operator_approval_status || 'missing'),
    reconciliationRequired: contract.broker_reconciliation_required === true,
    reconciliationStatus: String(contract.broker_reconciliation_status || 'not_run'),
    liveAllowed: contract.live_submission_allowed === true,
    source: String(contract.source || 'execution_plan'),
    transition,
    transitionIssues,
    issues
  }
}

function portfolioTransitionSummary(row: Record<string, unknown>) {
  const gate = executionSafetyGate(row)
  const transition = gate.transition
  const state = String(row.position_state || transition.current_state || '').trim().toUpperCase()
  const entryEvidenceRule = String(transition.entry_evidence_rule || '').trim()
  const entryEvidenceRequired = transition.entry_evidence_required === true
  const brokerExecutionAllowed = transition.broker_execution_allowed === true
  const effectivePublishedOn = String(transition.effective_published_on || transition.portfolio_published_on || row.published_on || '').trim()
  const hasData = Boolean(state || Object.keys(transition).length || gate.transitionIssues.length)
  const isPlannedEntry = state === 'PLANNED_ENTRY'
  const headline = isPlannedEntry
    ? 'Portfolio row is only a planned entry until lifecycle/execution evidence confirms the real entry.'
    : state
      ? `Portfolio handoff state is ${state}; broker execution needs a valid planned-entry contract.`
      : 'Execution needs a portfolio transition contract before broker handoff.'
  const issues = [...gate.transitionIssues]
  if (hasData && !isPlannedEntry && state) issues.unshift(`Portfolio state is ${state}, not PLANNED_ENTRY.`)
  if (hasData && !entryEvidenceRule) issues.push('Missing entry evidence rule.')
  if (hasData && brokerExecutionAllowed) issues.push('Portfolio contract unexpectedly allows direct broker execution.')
  return {
    hasData,
    state: state || 'UNKNOWN',
    isPlannedEntry,
    entryEvidenceRequired,
    entryEvidenceRule,
    effectivePublishedOn,
    brokerExecutionAllowed,
    headline,
    issues: Array.from(new Set(issues.map((item) => item.trim()).filter(Boolean)))
  }
}

function preconditionLabel(value: unknown) {
  const key = String(value || '').trim()
  const labels: Record<string, string> = {
    broker_order_execution_mode: 'Broker-order execution mode',
    buy_transaction_type: 'BUY transaction type',
    sell_transaction_type: 'SELL transaction type',
    risk_level_present: 'Stop / invalidation level present',
    entry_evidence_after_publication: 'Entry evidence after recommendation',
    open_position_context: 'Open-position context',
    add_on_evidence_present: 'Add-on evidence',
    exit_trigger_present: 'Exit trigger',
    partial_exit_trigger_present: 'Partial-exit trigger',
    partial_exit_fraction_resolved: 'Partial-exit fraction',
    full_exit_implied: 'Full exit implied',
    recommended_stop_present: 'Recommended stop present',
    policy_audit_only: 'Policy audit only',
    no_exit_trigger_present: 'No exit trigger',
    watch_evidence_present: 'Watch evidence',
    no_broker_order: 'No broker order',
    operator_question_present: 'Operator question'
  }
  return labels[key] || key.replaceAll('_', ' ')
}

function actionTransitionSummary(row: Record<string, unknown>) {
  const transition = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'action_transition']))
  const required = stringArray(transition.required_preconditions)
  const missing = stringArray(transition.missing_preconditions)
  const action = String(transition.action_code || actionLabel(row)).toUpperCase()
  const preconditionStatus = String(transition.precondition_status || (missing.length ? 'incomplete' : '')).toLowerCase()
  const hasData = Object.keys(transition).length > 0
  const brokerCandidate = transition.broker_order_candidate === true
  const brokerAllowed = transition.broker_execution_allowed === true
  const status = preconditionStatus || (hasData ? 'unknown' : '')
  const headline = !hasData
    ? ''
    : brokerCandidate
      ? 'This action can only move toward execution after a separate safety-gated preview and the listed preconditions are satisfied.'
      : 'This action is monitoring, policy, or review context and does not create a broker order.'
  return {
    hasData,
    action,
    stateEffect: String(transition.state_effect || '').trim(),
    nextRequiredStage: String(transition.next_required_stage || '').trim(),
    brokerCandidate,
    brokerAllowed,
    allowedAfterPreview: transition.allowed_after_preview === true,
    status,
    headline,
    required,
    missing,
    entryEvidenceRequired: transition.entry_evidence_required === true,
    exitEvidenceRequired: transition.exit_evidence_required === true,
    policyAuditRequired: transition.policy_audit_required === true,
    brokerBoundary: String(transition.broker_boundary || '').trim()
  }
}

function actionTransitionTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const summary = actionTransitionSummary(row)
  if (!summary.hasData) return 'neutral'
  if (summary.missing.length) return 'warning'
  if (summary.brokerCandidate) return 'info'
  return 'neutral'
}

function executionGateTone(row: Record<string, unknown>): 'success' | 'warning' | 'danger' | 'info' | 'dark' | 'neutral' {
  const gate = executionSafetyGate(row)
  if (!gate.hasData) return 'neutral'
  if (gate.liveAllowed) return 'success'
  if (gate.approvalRequired || gate.reconciliationRequired || gate.issues.length) return 'warning'
  return 'info'
}

function actionBlockerSummary(row: Record<string, unknown>) {
  const action = actionLabel(row)
  const contract = asDict(row.recommendation_reason)
  const status = String(row.reason_contract_status || contract.status || '').toLowerCase()
  const executionMode = String(firstActionValue(row, ['execution_mode'], [['recommendation_reason', 'execution_mode']]) || '').toLowerCase()
  const sourceAction = String(actionSourceAction(row) || '').toUpperCase()
  const original = String(contract.original_action_code || contract.original_action || sourceAction || '').toUpperCase()
  const gate = executionSafetyGate(row)
  const freshness = featureFreshnessSummary(row)
  const sourceIssues = sourceIssueSummary(row)
  const explanation = actionExplanation(row)
  const blockers: string[] = []
  if (action.includes('MANUAL') || action.includes('REVIEW')) {
    blockers.push(original && original !== action ? `Final action is manual review instead of ${original}.` : 'Final action requires manual review.')
  }
  if (executionMode && executionMode !== 'broker_order') {
    blockers.push(`Execution mode is ${executionMode}, so this row is not broker-submittable.`)
  }
  if (status && (!status.includes('complete') || status.includes('review') || status.includes('incomplete') || status.includes('blocked'))) {
    blockers.push(`Reason contract status is ${row.reason_contract_status || contract.status}.`)
  }
  if (freshness.status.toLowerCase() === 'blocked') {
    blockers.push('Required price/technical inputs were blocked, stale, or missing.')
  }
  for (const issue of sourceIssues.blockers.slice(0, 3)) blockers.push(issue)
  if (gate.hasData && !gate.liveAllowed) {
    if (gate.approvalRequired && gate.approvalStatus !== 'approved') blockers.push(`Operator approval is ${gate.approvalStatus}.`)
    if (gate.reconciliationRequired && !['passed', 'ok', 'reconciled'].includes(gate.reconciliationStatus)) blockers.push(`Broker reconciliation is ${gate.reconciliationStatus}.`)
    for (const issue of gate.issues.slice(0, 2)) blockers.push(issue)
  }
  const transition = portfolioTransitionSummary(row)
  if (transition.hasData) {
    if (!transition.isPlannedEntry) blockers.push(`Portfolio handoff state is ${transition.state}, not PLANNED_ENTRY.`)
    for (const issue of transition.issues.slice(0, 2)) blockers.push(issue)
  }
  for (const field of explanation.missingFields.slice(0, 3)) blockers.push(`Missing reason field: ${field}.`)
  for (const flag of explanation.riskFlags.slice(0, 3)) blockers.push(flag)
  const uniqueBlockers = Array.from(new Set(blockers.map((item) => item.trim()).filter(Boolean)))
  return {
    hasData: uniqueBlockers.length > 0,
    title: action.includes('MANUAL') || action.includes('REVIEW') ? 'Why this is not approved/executable' : 'Execution readiness blockers',
    originalAction: original,
    finalAction: action,
    blockers: uniqueBlockers
  }
}

function portfolioEligibilitySummary(row: Record<string, unknown>) {
  const action = actionLabel(row).toUpperCase()
  const queue = actionQueueContract(row)
  const trust = finalStateTrust(row)
  const transition = actionTransitionSummary(row)
  const portfolio = portfolioTransitionSummary(row)
  const sourceIssues = sourceIssueSummary(row)
  const blockers = Array.from(new Set([
    ...trust.blockers,
    ...actionBlockerSummary(row).blockers,
    ...portfolio.issues,
    ...sourceIssues.blockers,
    ...transition.missing.map((item) => `Missing execution precondition: ${preconditionLabel(item)}.`)
  ].map((item) => String(item || '').trim()).filter(Boolean)))
  const source = actionSourceLabel(row) || 'unknown'
  const sourceAction = actionSourceAction(row) || 'unknown'
  const brokerCandidate = queue.broker_order_candidate === true || trust.brokerCandidate || transition.brokerCandidate
  const approvedFilterMatch = queue.approved_filter_match === true || trust.approvedFilterMatch
  const isBuyLike = ['BUY', 'BUY_MORE'].includes(action)
  const isExitLike = ['SELL', 'PARTIAL_SELL', 'FULL_EXIT', 'EXIT', 'TIGHTEN_STOP'].includes(action)
  const isManual = action.includes('MANUAL') || action.includes('REVIEW')
  const isWatch = action.includes('WATCH')
  const isHold = action === 'HOLD'

  if (isManual) {
    return {
      status: 'manual_review',
      tone: 'warning' as const,
      headline: 'Not portfolio-eligible until manual review is resolved.',
      detail: `Upstream source ${source} suggested ${sourceAction}, but the final consolidated action is ${action}.`,
      nextStep: 'Resolve the manual review item or wait for a later advisory run to emit a broker-candidate action.',
      blockers
    }
  }
  if (isWatch || isHold) {
    return {
      status: isWatch ? 'watch_only' : 'hold_context',
      tone: 'neutral' as const,
      headline: isWatch ? 'Watch-only: not a portfolio entry.' : 'Hold/context row: no new portfolio entry.',
      detail: `Final action is ${action}; this row is monitoring or lifecycle context, not a new buy approval.`,
      nextStep: 'Keep watching until the final action changes to a broker-candidate BUY/SELL with complete safety evidence.',
      blockers
    }
  }
  if (isExitLike) {
    return {
      status: 'exit_or_risk_reduction',
      tone: 'info' as const,
      headline: 'Exit/risk-reduction action: check existing-position context.',
      detail: 'This is not a new portfolio entry. It needs an existing/open position context and execution safety gates before any handoff.',
      nextStep: 'Review lifecycle/execution evidence and reconcile against actual holdings before acting.',
      blockers
    }
  }
  if (isBuyLike && brokerCandidate && approvedFilterMatch) {
    const hasBlockingEvidence = blockers.length > 0 || trust.status.toLowerCase() === 'blocked' || transition.missing.length > 0
    return {
      status: hasBlockingEvidence ? 'broker_candidate_blocked' : 'portfolio_handoff_candidate',
      tone: hasBlockingEvidence ? 'warning' as const : 'success' as const,
      headline: hasBlockingEvidence
        ? 'Broker-candidate BUY, but portfolio/execution handoff is still blocked.'
        : 'Broker-candidate BUY: eligible for portfolio/execution review, not direct trading.',
      detail: portfolio.hasData
        ? portfolio.headline
        : 'No explicit portfolio transition contract is visible on this row; execution still needs preview, approval, identity, reconciliation, and live-evidence gates.',
      nextStep: hasBlockingEvidence
        ? 'Clear the listed blockers or rerun advisory after missing inputs are repaired.'
        : 'Create/review the dry-run execution preview, then complete approval and reconciliation gates.',
      blockers
    }
  }
  return {
    status: brokerCandidate ? 'broker_candidate_needs_review' : 'not_portfolio_eligible',
    tone: brokerCandidate ? 'info' as const : 'neutral' as const,
    headline: brokerCandidate ? 'Potential broker candidate, but not approved by the current queue filter contract.' : 'Not portfolio-eligible from the current final action.',
    detail: `Final action ${action}; source ${source}; source action ${sourceAction}.`,
    nextStep: brokerCandidate ? 'Check Final State Trust and execution gates before treating this as actionable.' : 'Treat this as information until a later action row becomes broker-candidate.',
    blockers
  }
}

function actionConsolidationSummary(row: Record<string, unknown>) {
  const conflict = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'conflict_resolution']))
  const losingCandidates = Array.isArray(conflict.losing_candidates)
    ? conflict.losing_candidates.filter((item) => typeof item === 'object' && item !== null) as Record<string, unknown>[]
    : []
  const winnerAction = String(conflict.winning_action_code || actionLabel(row) || '').toUpperCase()
  const winnerSource = String(conflict.winning_action_source || actionSourceLabel(row) || '').trim()
  const conflictCount = Number(conflict.same_symbol_conflict_count ?? losingCandidates.length ?? 0)
  const candidateCount = Number(conflict.same_symbol_candidate_count ?? (conflictCount + 1))
  const ruleId = String(conflict.conflict_precedence_rule_id || '').trim()
  const ruleReason = String(conflict.conflict_precedence_reason || '').trim()
  const reason = String(conflict.source_precedence_reason || ruleReason || '').trim()
  const hasData = Boolean(Object.keys(conflict).length && (reason || losingCandidates.length || conflictCount || ruleId || winnerAction))
  return {
    hasData,
    winnerAction,
    winnerSource,
    conflictCount: Number.isFinite(conflictCount) ? conflictCount : losingCandidates.length,
    candidateCount: Number.isFinite(candidateCount) ? candidateCount : losingCandidates.length + 1,
    ruleId,
    ruleReason,
    reason,
    losingCandidates: losingCandidates.slice(0, 4)
  }
}

function losingCandidateLabel(candidate: Record<string, unknown>) {
  const action = String(candidate.action_code || candidate.action || 'UNKNOWN').toUpperCase()
  const source = String(candidate.action_source || candidate.source || 'unknown')
  const setup = String(candidate.setup_id || '').trim()
  const sourceAction = String(candidate.source_action || '').trim()
  const detail = [source, setup, sourceAction ? `source action ${sourceAction}` : ''].filter(Boolean).join(' · ')
  return `${action}${detail ? ` from ${detail}` : ''}`
}

function pluralSuffix(count: unknown) {
  return Number(count) === 1 ? '' : 's'
}

function pluralVerb(count: unknown) {
  return Number(count) === 1 ? 'was' : 'were'
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

function routerContext(row: Record<string, unknown>) {
  return asDict(row.router_context)
}

function routerContextList(context: Dict, key: string) {
  const value = context[key]
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
}

function hasRouterContext(row: Record<string, unknown>) {
  return Object.keys(routerContext(row)).length > 0
}

function nextStepTone(row: Record<string, unknown>) {
  const kind = String(row.operator_next_step_kind || '').toLowerCase()
  if (kind.includes('exposure')) return 'border-rust/20 bg-rust/10 text-rust'
  if (kind.includes('manual')) return 'border-sun/30 bg-sun/20 text-ink'
  if (kind.includes('watch') || kind.includes('wait')) return 'border-moss/20 bg-moss/10 text-moss'
  return 'border-black/10 bg-paper/80 text-ink/65'
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
  <PayloadFreshnessStrip class="mt-4" :items="payloadFreshnessItems" />

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
        <div class="mt-3 rounded-2xl border p-3" :class="nextStepTone(row)">
          <p class="text-xs font-black uppercase tracking-[0.2em]">Operator next step</p>
          <p class="mt-2 text-xs font-semibold leading-5">
            {{ row.operator_next_step || 'No immediate operator action is implied by this refresh.' }}
          </p>
        </div>
        <div v-if="hasRouterContext(row)" class="mt-3 rounded-2xl border border-moss/20 bg-moss/10 p-3">
          <div class="flex flex-wrap items-center justify-between gap-2">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-moss">Router trigger</p>
            <span v-if="routerContext(row).action_type" class="rounded-full bg-white px-3 py-1 text-xs font-black text-moss">
              {{ titleLabel(routerContext(row).action_type) }}
            </span>
          </div>
          <div class="mt-2 flex flex-wrap gap-2 text-xs font-bold text-ink/60">
            <span v-for="source in routerContextList(routerContext(row), 'source_types')" :key="`source-${row.refresh_id}-${source}`" class="rounded-full bg-white px-3 py-1">
              {{ titleLabel(source) }}
            </span>
            <span v-for="reason in routerContextList(routerContext(row), 'reasons')" :key="`reason-${row.refresh_id}-${reason}`" class="rounded-full bg-sun/80 px-3 py-1 text-ink">
              {{ humanRouterLabel(reason) }}
            </span>
            <span v-if="routerContext(row).priority_score !== undefined" class="rounded-full bg-white px-3 py-1">
              priority {{ numberText(routerContext(row).priority_score) }}
            </span>
            <span v-if="routerContext(row).best_rank !== undefined" class="rounded-full bg-white px-3 py-1">
              rank {{ routerContext(row).best_rank }}
            </span>
          </div>
          <p class="mt-2 text-xs font-semibold leading-5 text-ink/60">
            This is watcher evidence only. It explains why the symbol was refreshed; it is not portfolio authority.
          </p>
        </div>
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
          <span class="rounded-full bg-amber-100 px-3 py-1 text-amber-900">authority: {{ row.authority_scope || 'review_input_only' }}</span>
          <span class="rounded-full bg-rust/10 px-3 py-1 text-rust">portfolio: {{ row.portfolio_authority || 'none' }}</span>
          <span class="rounded-full bg-rust/10 px-3 py-1 text-rust">broker: {{ row.broker_execution_allowed === true ? 'allowed' : 'blocked' }}</span>
          <span v-if="row.full_advisory_required !== false" class="rounded-full bg-moss/10 px-3 py-1 text-moss">full advisory required</span>
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
    <div class="mt-5 grid gap-3 md:grid-cols-6">
      <MetricTile label="Top Names" :value="String(marketSummary.top_context_count || 0)" note="Tracked context universe" />
      <MetricTile label="Above 50DMA" :value="pct(marketSummary.breadth_above_dma50_pct)" note="Breadth inside top context" />
      <MetricTile label="Trend Aligned" :value="pct(marketSummary.breadth_trend_alignment_pct)" note="Healthy leadership share" />
      <MetricTile label="RS Positive" :value="pct(marketSummary.breadth_rs_positive_pct)" note="Outperforming benchmark" />
      <MetricTile label="Triggered" :value="String(Number(marketSummary.triggered_event_count_7d || 0))" note="Material top-context events, 7d" />
      <MetricTile label="Observed" :value="String(Number(marketSummary.context_observed_event_count_7d || 0))" note="Context-only top events, 7d" />
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
      <ApiErrorBanner v-if="identityIssuesError" class="mb-3" title="Identity/source issue lookup failed" :error="identityIssuesError" />
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
              <option value="approved">Broker candidate</option>
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
            <MetaChip v-if="queueStatusLabel(row)" label="queue status" :tone="queueStatusTone(row)" :title="queueStatusTitle(row)">{{ queueStatusLabel(row) }}</MetaChip>
            <MetaChip v-if="row.setup_id" label="setup" :title="setupTitle(row)">{{ row.setup_id }}</MetaChip>
            <MetaChip v-if="row.reason_contract_status" label="reason" :title="reasonContractTitle(row)" :tone="String(row.reason_contract_status).includes('complete') ? 'green' : 'yellow'">{{ row.reason_contract_status }}</MetaChip>
            <MetaChip v-if="executionSafetyGate(row).hasData" label="execution gate" :tone="executionSafetyGate(row).liveAllowed ? 'green' : 'yellow'">
              {{ executionSafetyGate(row).liveAllowed ? 'live allowed' : 'approval/reconcile required' }}
            </MetaChip>
          </div>
          <section class="mt-4 rounded-2xl border border-moss/20 bg-white/85 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Portfolio eligibility</p>
                <p class="mt-1 text-sm font-bold leading-6 text-ink">{{ portfolioEligibilitySummary(row).headline }}</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">{{ portfolioEligibilitySummary(row).detail }}</p>
              </div>
              <StatusPill :tone="portfolioEligibilitySummary(row).tone">
                {{ portfolioEligibilitySummary(row).status.replaceAll('_', ' ') }}
              </StatusPill>
            </div>
            <p class="mt-3 rounded-xl bg-paper/80 px-3 py-2 text-sm leading-6 text-ink/70">
              <b>Next step:</b> {{ portfolioEligibilitySummary(row).nextStep }}
            </p>
            <ul v-if="portfolioEligibilitySummary(row).blockers.length" class="mt-3 space-y-1 text-sm leading-6 text-rust">
              <li v-for="blocker in portfolioEligibilitySummary(row).blockers.slice(0, 5)" :key="blocker">- {{ blocker }}</li>
            </ul>
          </section>
          <section
            v-if="sourceIssueSummary(row).hasData"
            class="mt-4 rounded-2xl border border-rust/25 bg-rust/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">Identity / source blocker</p>
                <p class="mt-1 text-sm font-bold leading-6 text-ink">
                  {{ sourceIssueSummary(row).count }} open source issue{{ pluralSuffix(sourceIssueSummary(row).count) }} for {{ sourceIssueSummary(row).symbol }}.
                </p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  This is an operational data problem, not an investment thesis. Do not treat this row as broker-ready until the source issue is repaired and the action is regenerated.
                </p>
              </div>
              <StatusPill tone="danger">SOURCE BLOCKED</StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Type:</b> {{ titleLabel(sourceIssueSummary(row).issueType) }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Exchange:</b> {{ sourceIssueSummary(row).exchange || '-' }}</p>
              <p class="rounded-xl bg-white/80 px-3 py-2"><b>Last seen:</b> {{ sourceIssueSummary(row).lastSeen || '-' }}</p>
            </div>
            <p v-if="sourceIssueSummary(row).error" class="mt-3 rounded-xl bg-white/80 px-3 py-2 text-sm leading-6 text-rust">
              <b>Error:</b> {{ sourceIssueSummary(row).error }}
            </p>
            <p v-if="sourceIssueSummary(row).suggestedAction" class="mt-3 rounded-xl bg-white/80 px-3 py-2 text-sm leading-6 text-ink/70">
              <b>Repair:</b> {{ sourceIssueSummary(row).suggestedAction }}
            </p>
            <LinkButton class="mt-3" to="/identity-issues" title="Open Identity Issues">Open identity issues</LinkButton>
          </section>
          <section
            v-if="finalStateTrust(row).hasData"
            class="mt-4 rounded-2xl border border-ink/10 bg-ink/[0.03] p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Final State Trust</p>
                <p class="mt-1 text-sm leading-6 text-ink/70">
                  Final action is <b>{{ finalStateTrust(row).finalAction }}</b><span v-if="finalStateTrust(row).displayStatus"> in {{ finalStateTrust(row).displayStatus }}</span>.
                  <span v-if="finalStateTrust(row).brokerCandidate">This is only an execution candidate after preview, approval, identity, reconciliation, and live-evidence gates.</span>
                  <span v-else>This is read-only operator work unless a later pipeline run changes the final action.</span>
                </p>
                <p v-if="finalStateTrust(row).operatorNote" class="mt-1 text-xs font-semibold leading-5 text-ink/45">
                  {{ finalStateTrust(row).operatorNote }}
                </p>
              </div>
              <StatusPill :tone="trustStatusTone(row)">
                {{ finalStateTrust(row).status.replaceAll('_', ' ') || 'tracked' }}
              </StatusPill>
            </div>
            <div class="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
              <div
                v-for="check in finalStateTrust(row).checks"
                :key="String(check.key || check.label)"
                class="rounded-xl bg-white/85 px-3 py-2 text-sm leading-6"
              >
                <div class="flex items-start justify-between gap-2">
                  <b>{{ check.label || check.key }}</b>
                  <StatusPill :tone="trustCheckTone(check)">
                    {{ String(check.status || 'unknown').replaceAll('_', ' ') }}
                  </StatusPill>
                </div>
                <p class="mt-1 text-ink/65">{{ check.detail }}</p>
              </div>
            </div>
            <ul v-if="finalStateTrust(row).blockers.length" class="mt-3 space-y-1 text-sm leading-6 text-rust">
              <li v-for="item in finalStateTrust(row).blockers.slice(0, 3)" :key="item">- {{ item }}</li>
            </ul>
          </section>
          <section
            v-if="actionConsolidationSummary(row).hasData"
            class="mt-4 rounded-2xl border border-ink/10 bg-white/80 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Consolidated decision</p>
                <p class="mt-1 text-sm leading-6 text-ink/70">
                  Winner is <b>{{ actionConsolidationSummary(row).winnerAction }}</b><span v-if="actionConsolidationSummary(row).winnerSource"> from {{ actionConsolidationSummary(row).winnerSource }}</span>.
                  {{ actionConsolidationSummary(row).candidateCount }} candidate{{ pluralSuffix(actionConsolidationSummary(row).candidateCount) }} {{ pluralVerb(actionConsolidationSummary(row).candidateCount) }} considered; {{ actionConsolidationSummary(row).conflictCount }} losing conflict{{ pluralSuffix(actionConsolidationSummary(row).conflictCount) }} {{ pluralVerb(actionConsolidationSummary(row).conflictCount) }} recorded.
                </p>
              </div>
              <StatusPill tone="dark">ONE FINAL ACTION</StatusPill>
            </div>
            <p v-if="actionConsolidationSummary(row).ruleId || actionConsolidationSummary(row).reason" class="mt-3 rounded-xl bg-paper/80 px-3 py-2 text-sm leading-6 text-ink/70">
              <b v-if="actionConsolidationSummary(row).ruleId">Rule {{ actionConsolidationSummary(row).ruleId }}:</b>
              {{ actionConsolidationSummary(row).reason || actionConsolidationSummary(row).ruleReason }}
            </p>
            <div v-if="actionConsolidationSummary(row).losingCandidates.length" class="mt-3 grid gap-2 md:grid-cols-2">
              <p
                v-for="candidate in actionConsolidationSummary(row).losingCandidates"
                :key="losingCandidateLabel(candidate)"
                class="rounded-xl bg-paper/80 px-3 py-2 text-sm leading-6 text-ink/65"
              >
                <b>Lost:</b> {{ losingCandidateLabel(candidate) }}
              </p>
            </div>
          </section>
          <section
            v-if="actionBlockerSummary(row).hasData"
            class="mt-4 rounded-2xl border border-rust/25 bg-rust/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-rust">{{ actionBlockerSummary(row).title }}</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  Final action is {{ actionBlockerSummary(row).finalAction }}<span v-if="actionBlockerSummary(row).originalAction && actionBlockerSummary(row).originalAction !== actionBlockerSummary(row).finalAction">, after starting from {{ actionBlockerSummary(row).originalAction }}</span>.
                </p>
              </div>
              <StatusPill tone="warning">NOT BROKER READY</StatusPill>
            </div>
            <ul class="mt-3 space-y-1 text-sm leading-6 text-ink/75">
              <li v-for="blocker in actionBlockerSummary(row).blockers.slice(0, 6)" :key="blocker">- {{ blocker }}</li>
            </ul>
          </section>
          <section
            v-if="portfolioTransitionSummary(row).hasData"
            class="mt-4 rounded-2xl border border-moss/20 bg-moss/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Portfolio handoff boundary</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  {{ portfolioTransitionSummary(row).headline }}
                </p>
              </div>
              <StatusPill :tone="portfolioTransitionSummary(row).isPlannedEntry ? 'info' : 'warning'">
                {{ portfolioTransitionSummary(row).state }}
              </StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Entry evidence:</b> {{ portfolioTransitionSummary(row).entryEvidenceRequired ? 'required' : 'not required' }}
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Broker direct:</b> {{ portfolioTransitionSummary(row).brokerExecutionAllowed ? 'allowed' : 'blocked at portfolio stage' }}
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Effective from:</b> {{ portfolioTransitionSummary(row).effectivePublishedOn || 'portfolio published date' }}
              </p>
            </div>
            <p v-if="portfolioTransitionSummary(row).entryEvidenceRule" class="mt-3 rounded-xl bg-white/80 px-3 py-2 text-sm leading-6 text-ink/70">
              <b>Rule:</b> {{ portfolioTransitionSummary(row).entryEvidenceRule }}
            </p>
            <ul v-if="portfolioTransitionSummary(row).issues.length" class="mt-3 space-y-1 text-sm leading-6 text-rust">
              <li v-for="issue in portfolioTransitionSummary(row).issues.slice(0, 4)" :key="issue">{{ issue }}</li>
            </ul>
          </section>
          <section
            v-if="actionTransitionSummary(row).hasData"
            class="mt-4 rounded-2xl border border-sky/20 bg-sky/10 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-sky">Execution preconditions</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  {{ actionTransitionSummary(row).headline }}
                </p>
              </div>
              <StatusPill :tone="actionTransitionTone(row)">
                {{ actionTransitionSummary(row).status || 'tracked' }}
              </StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>State effect:</b> {{ actionTransitionSummary(row).stateEffect || '-' }}
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>Next stage:</b> {{ actionTransitionSummary(row).nextRequiredStage || '-' }}
              </p>
              <p class="rounded-xl bg-white/80 px-3 py-2">
                <b>After preview:</b> {{ actionTransitionSummary(row).allowedAfterPreview ? 'eligible' : 'not eligible' }}
              </p>
            </div>
            <div v-if="actionTransitionSummary(row).required.length" class="mt-3 rounded-xl bg-white/80 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Required checks</p>
              <div class="mt-2 flex flex-wrap gap-2">
                <span
                  v-for="item in actionTransitionSummary(row).required.slice(0, 8)"
                  :key="item"
                  class="rounded-full bg-moss/10 px-3 py-1 text-xs font-bold text-moss"
                >
                  {{ preconditionLabel(item) }}
                </span>
              </div>
            </div>
            <div v-if="actionTransitionSummary(row).missing.length" class="mt-3 rounded-xl bg-rust/10 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] text-rust">Missing before execution</p>
              <ul class="mt-2 space-y-1 text-sm leading-6 text-rust">
                <li v-for="item in actionTransitionSummary(row).missing.slice(0, 6)" :key="item">- {{ preconditionLabel(item) }}</li>
              </ul>
            </div>
            <p v-if="actionTransitionSummary(row).brokerBoundary" class="mt-3 rounded-xl bg-white/80 px-3 py-2 text-sm leading-6 text-ink/70">
              <b>Broker boundary:</b> {{ actionTransitionSummary(row).brokerBoundary }}
            </p>
          </section>
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
          <section
            v-if="featureFreshnessSummary(row).hasData"
            class="mt-4 rounded-2xl border border-black/10 bg-white/70 p-4"
          >
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Data inputs</p>
                <p class="mt-1 text-sm leading-6 text-ink/65">
                  Required inputs for this action row: daily OHLCV and technical features.
                </p>
              </div>
              <StatusPill :tone="featureFreshnessTone(row)">
                {{ featureFreshnessSummary(row).status.toUpperCase() }}
              </StatusPill>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Fresh:</b> {{ featureFreshnessSummary(row).counts.fresh || 0 }}</p>
              <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Stale:</b> {{ featureFreshnessSummary(row).counts.stale || 0 }}</p>
              <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Missing:</b> {{ featureFreshnessSummary(row).counts.missing || 0 }}</p>
            </div>
            <ul v-if="featureFreshnessSummary(row).blockers.length" class="mt-3 space-y-1 text-sm leading-6 text-rust">
              <li v-for="item in featureFreshnessSummary(row).blockers" :key="String(item.input_key)">{{ item.label || item.input_key }}: {{ item.status }} / {{ item.reason }}</li>
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
              <p v-if="companyMemoryReview(row).sourceContract.coverage_status" class="rounded-xl bg-white/80 px-3 py-2">
                <b>Evidence coverage:</b> {{ companyMemoryReview(row).sourceContract.coverage_status }}
              </p>
            </div>
            <p v-if="companyMemoryReview(row).missingSources.length" class="mt-3 rounded-xl bg-rust/10 px-3 py-2 text-sm font-semibold leading-6 text-rust">
              Missing compact evidence: {{ companyMemoryReview(row).missingSources.join(', ') }}. Treat this review as partial.
            </p>
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

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">TS Forecast Paper Portfolio</p>
        <h2 class="mt-2 text-2xl font-black">Forecast evidence must beat paper baselines first</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Research-only forecast decisions compared with naive momentum and current advisory alignment. These rows do not create action queue, portfolio, or Dhan execution authority.
        </p>
      </div>
      <LinkButton variant="ghost" to="/operations">
        Check research runs
      </LinkButton>
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article class="rounded-3xl border border-black/10 bg-ink p-5 text-paper lg:col-span-2">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-paper/45">Promotion gate</p>
            <h3 class="mt-2 text-xl font-black">{{ tsPromotionScorecard.headline || 'TS forecast paper evidence has not passed promotion gates.' }}</h3>
            <p class="mt-2 max-w-3xl text-sm leading-6 text-paper/65">
              {{ tsPromotionScorecard.operator_action || 'Keep TS forecasts research-only until paper evidence beats momentum with enough breadth.' }}
            </p>
          </div>
          <StatusPill :tone="tsPromotionCheck?.ready_for_operator_review ? 'success' : 'warning'" title="Read-only gate. Passing this only allows manual review, not automatic policy changes.">
            {{ tsPromotionCheck?.decision || 'hold_research_only' }}
          </StatusPill>
        </div>
        <div class="mt-4 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-2xl bg-paper/10 px-3 py-2"><b>Ready groups:</b> {{ numberText(tsPromotionScorecard.ready_group_count) }}/{{ numberText(tsPromotionScorecard.group_count) }}</p>
          <p class="rounded-2xl bg-paper/10 px-3 py-2"><b>Best trades:</b> {{ numberText(tsPromotionBestGroup.evaluated_trades) }}</p>
          <p class="rounded-2xl bg-paper/10 px-3 py-2"><b>Best avg:</b> {{ pct(Number(tsPromotionBestGroup.avg_cost_adjusted_return || 0) * 100) }}</p>
          <p class="rounded-2xl bg-paper/10 px-3 py-2"><b>Lift vs momentum:</b> {{ pct(Number(tsPromotionBestGroup.lift_vs_momentum || 0) * 100) }}</p>
        </div>
        <p v-if="Array.isArray(tsPromotionBestGroup.failed_gates) && tsPromotionBestGroup.failed_gates.length" class="mt-3 rounded-2xl bg-rust/20 px-3 py-2 text-xs font-bold text-paper">
          Failed gates: {{ tsPromotionBestGroup.failed_gates.join(', ') }}
        </p>
      </article>
      <article class="rounded-3xl border border-black/10 bg-white/75 p-5 lg:col-span-2">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Configured review rules</p>
            <h3 class="mt-2 text-lg font-black text-ink">Manual TS rules are visible, not live authority</h3>
            <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
              These are disabled/review-only config entries generated from approved promotion reviews. They do not create actions, portfolio rows, or broker orders.
            </p>
          </div>
          <StatusPill :tone="tsReviewRuleIssues.length ? 'warning' : 'neutral'" title="Config validation status for ts_forecast_review_rules.">
            {{ tsReviewRules?.status || 'not_loaded' }}
          </StatusPill>
        </div>
        <div class="mt-4 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Rules:</b> {{ numberText(tsReviewRuleSummary.row_count) }}</p>
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Active review:</b> {{ numberText(tsReviewRuleSummary.active_review_count) }}</p>
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Trusted overlay:</b> {{ numberText(tsReviewRuleSummary.trusted_overlay_count) }}</p>
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Issues:</b> {{ numberText(tsReviewRuleSummary.issue_count) }}</p>
        </div>
        <div v-if="tsReviewRuleRows.length" class="mt-4 grid gap-2 md:grid-cols-2">
          <p v-for="rule in tsReviewRuleRows.slice(0, 4)" :key="String(rule.rule_id || `${rule.model_name}-${rule.horizon_days}`)" class="rounded-2xl bg-moss/10 px-3 py-2 text-xs font-bold text-moss">
            {{ rule.model_name || '-' }} · {{ rule.horizon_days || '-' }}d · {{ rule.status || '-' }} · live policy: {{ rule.usable_for_live_policy ? 'allowed' : 'blocked' }}
          </p>
        </div>
        <p v-if="tsReviewRuleIssues.length" class="mt-3 rounded-2xl bg-rust/10 px-3 py-2 text-xs font-bold text-rust">
          Rule issues: {{ tsReviewRuleIssues.slice(0, 3).map((issue) => issue.code || issue.message || 'issue').join(', ') }}
        </p>
      </article>
      <article class="rounded-3xl border border-black/10 bg-white/75 p-5 lg:col-span-2">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Reviewed config workflow</p>
            <h3 class="mt-2 text-lg font-black text-ink">TS forecast promotion reviews and application audits</h3>
            <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
              These rows show whether TS forecast evidence has been reviewed and whether a generated config diff was manually marked. This panel is visibility only; it does not create previews, edit config, promote policy, or submit broker orders.
            </p>
          </div>
          <StatusPill tone="neutral" title="Read-only visibility for TS forecast reviewed-config workflow.">
            visibility only
          </StatusPill>
        </div>
        <div class="mt-4 grid gap-4 lg:grid-cols-2">
          <div class="rounded-2xl bg-paper/80 p-4">
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Latest TS reviews</p>
            <div v-if="tsPromotionReviewRows.length" class="mt-3 grid gap-2">
              <p v-for="review in tsPromotionReviewRows.slice(0, 4)" :key="String(`${review.reviewed_at}-${review.model_name}-${review.horizon_days}`)" class="rounded-xl bg-white px-3 py-2 text-xs font-bold text-ink/65">
                {{ review.model_name || '-' }} · {{ review.horizon_days || '-' }}d · {{ titleLabel(review.manual_decision || review.recommendation || 'pending') }} · {{ review.reviewed_at || '-' }}
              </p>
            </div>
            <p v-else class="mt-3 rounded-xl bg-white px-3 py-2 text-xs font-bold text-ink/55">No TS promotion review rows returned.</p>
          </div>
          <div class="rounded-2xl bg-paper/80 p-4">
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Latest config application audits</p>
            <div v-if="configApplicationRows.length" class="mt-3 grid gap-2">
              <p v-for="row in configApplicationRows.slice(0, 4)" :key="String(row.application_id)" class="rounded-xl bg-white px-3 py-2 text-xs font-bold text-ink/65">
                {{ titleLabel(row.application_decision) }} · {{ titleLabel(row.verification_status || 'not verified') }} · broker: {{ asDict(row.operator_boundary).broker_execution_allowed ? 'allowed' : 'blocked' }}
              </p>
            </div>
            <p v-else class="mt-3 rounded-xl bg-white px-3 py-2 text-xs font-bold text-ink/55">No config application audit rows returned.</p>
          </div>
        </div>
      </article>
      <article v-for="row in tsPaperSummary.slice(0, 4)" :key="`${row.model_name || '-'}-${row.horizon_days || '-'}-${row.paper_decision || '-'}`" class="rounded-3xl border border-black/10 bg-white/75 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">{{ row.paper_decision || 'PAPER' }}</p>
            <h3 class="mt-2 text-lg font-black text-ink">{{ row.model_name || '-' }} · {{ row.horizon_days || '-' }}d</h3>
            <p class="mt-1 text-xs font-bold text-ink/45">{{ row.from_date || '-' }} to {{ row.to_date || '-' }}</p>
          </div>
          <StatusPill tone="neutral" title="Research-only paper trades. No broker authority.">
            {{ numberText(row.evaluated_trades) }}/{{ numberText(row.row_count) }} trades
          </StatusPill>
        </div>
        <div class="mt-4 grid gap-2 text-sm sm:grid-cols-3">
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Win:</b> {{ pct(row.win_rate_pct) }}</p>
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Avg:</b> {{ pct(row.avg_cost_adjusted_return_pct) }}</p>
          <p class="rounded-2xl bg-paper/70 px-3 py-2"><b>Momentum:</b> {{ pct(row.baseline_avg_cost_adjusted_return_pct) }}</p>
        </div>
        <div class="mt-3 grid gap-2 text-xs font-bold text-ink/55 sm:grid-cols-2">
          <p class="rounded-2xl bg-moss/10 px-3 py-2 text-moss">Aligned advisory: {{ numberText(row.aligned_positive_count) }}</p>
          <p class="rounded-2xl bg-rust/10 px-3 py-2 text-rust">Exit conflicts: {{ numberText(row.conflict_exit_count) }}</p>
        </div>
        <p class="mt-3 text-xs font-semibold leading-5 text-ink/55">
          {{ row.operator_note || 'Evidence only. Promotion requires enough matured rows and manual approval.' }}
        </p>
      </article>
      <p v-if="!tsPaperSummary.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
        No TS paper-portfolio summary yet. Run `./all_ts_forecast_paper_portfolio.sh` after forecast/evaluator rows exist.
      </p>
    </div>
  </section>
</template>
