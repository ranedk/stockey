<script setup lang="ts">
import type { Dict, TraceSummary } from '~/types/api'

const api = useOperatorApi()
const route = useRoute()
const symbol = computed(() => String(route.params.symbol || '').trim().toUpperCase())

const [{ data: actions, error: actionsError }, { data: portfolio, error: portfolioError }, { data: portfolioDetail, error: portfolioDetailError }, { data: events, error: eventsError }, { data: traceData, error: traceError }, { data: featureFreshness, error: featureFreshnessError }, { data: identityIssues, error: identityIssuesError }] = await Promise.all([
  useAsyncData(`symbol-actions-${symbol.value}`, () => api.getActions({ symbol: symbol.value, limit: 100, include_feature_freshness: true })),
  useAsyncData(`symbol-portfolio-${symbol.value}`, () => api.getPortfolio({ symbol: symbol.value, limit: 100 })),
  useAsyncData(`symbol-portfolio-detail-${symbol.value}`, () => api.getPortfolioDetail(symbol.value)),
  useAsyncData(`symbol-events-${symbol.value}`, () => api.getEvents(100, { symbol: symbol.value })),
  useAsyncData(`symbol-trace-${symbol.value}`, () => api.getSymbolTraceSummary(symbol.value, 250)),
  useAsyncData(`symbol-feature-freshness-${symbol.value}`, () => api.getFeatureFreshness(symbol.value)),
  useAsyncData(`symbol-identity-issues-${symbol.value}`, () => api.getIdentityIssues({ symbol: symbol.value, limit: 25 }))
])

const trace = computed<TraceSummary | null>(() => traceData.value || null)
const loadErrors = computed(() => [
  { title: `${symbol.value} actions failed`, error: actionsError.value },
  { title: `${symbol.value} portfolio failed`, error: portfolioError.value },
  { title: `${symbol.value} portfolio detail failed`, error: portfolioDetailError.value },
  { title: `${symbol.value} events failed`, error: eventsError.value },
  { title: `${symbol.value} trace failed`, error: traceError.value },
  { title: `${symbol.value} data inputs failed`, error: featureFreshnessError.value },
  { title: `${symbol.value} identity issues failed`, error: identityIssuesError.value }
].filter((row) => row.error))
const actionRows = computed(() => filterSymbolRows([...(actions.value?.top_action_recommendations || []), ...(actions.value?.action_recommendations || [])]))
const alertRows = computed(() => filterSymbolRows(actions.value?.alerts || []))
const todayRows = computed(() => filterSymbolRows(portfolio.value?.today_recommendations || []))
const currentRows = computed(() => filterSymbolRows(portfolio.value?.current_recommendations || []))
const portfolioRows = computed(() => filterSymbolRows(portfolio.value?.portfolio || []))
const lifecycleRows = computed(() => filterSymbolRows(portfolio.value?.lifecycle || []))
const recommendationRows = computed(() => [...todayRows.value, ...currentRows.value, ...portfolioRows.value, ...lifecycleRows.value])
const exitedRows = computed(() => filterSymbolRows(portfolio.value?.exited_recommendations || []))
const policyChangeRows = computed(() => Array.isArray(portfolioDetail.value?.policy_changes) ? portfolioDetail.value.policy_changes : [])
const eventRows = computed(() => filterSymbolRows([...(events.value?.events || []), ...(events.value?.operator_feed || []), ...(events.value?.alerts || [])]))
const finalAction = computed(() => mergeRows(
  actionRows.value[0],
  alertRows.value[0],
  currentRows.value[0],
  todayRows.value[0],
  portfolioRows.value[0],
  lifecycleRows.value[0]
))
const reasonContract = computed(() => finalAction.value.recommendation_reason || finalAction.value.reason_contract || finalAction.value.recommendation_reason_json)
const reasonStatus = computed(() => finalAction.value.reason_contract_status || finalAction.value.status)
const companyMemory = computed(() => companyMemoryReview(finalAction.value))
const decisionTimeFreshness = computed(() => asDict(finalAction.value.feature_freshness))
const decisionTimeFreshnessSummary = computed(() => asDict(finalAction.value.feature_freshness_summary))
const featureGateEffects = computed(() => collectFeatureGateEffects([finalAction.value, ...actionRows.value, ...recommendationRows.value]))
const dataInputs = computed(() => Array.isArray(featureFreshness.value?.inputs) ? featureFreshness.value.inputs : [])
const dataInputCounts = computed(() => asDict(featureFreshness.value?.counts))
const dataInputBlockers = computed(() => Array.isArray(featureFreshness.value?.blockers) ? featureFreshness.value.blockers : [])
const currentStageGates = computed(() => Array.isArray(featureFreshness.value?.stage_gates) ? featureFreshness.value.stage_gates : [])
const currentStageGateSummary = computed(() => asDict(featureFreshness.value?.stage_gate_summary))
const currentStageGateBlockedInputs = computed(() => Array.isArray(currentStageGateSummary.value.blocked_inputs) ? currentStageGateSummary.value.blocked_inputs : [])
const symbolIdentityIssues = computed(() => Array.isArray(identityIssues.value?.issues) ? identityIssues.value.issues : [])
const identityIssueSummary = computed(() => asDict(identityIssues.value?.summary))
const identitySourceWarnings = computed(() => Array.isArray(identityIssues.value?.source_warnings) ? identityIssues.value.source_warnings : [])
const explicitTarget = computed(() => numericFirstValue(['target_price', 'recommended_target_price']))
const derivedTarget = computed(() => computeDerivedTarget())
const displayTarget = computed(() => explicitTarget.value ?? derivedTarget.value)
const runupRead = computed(() => buildRunupRead())
const snapshotMeta = computed(() => asDict(actions.value?.snapshot || portfolio.value?.snapshot || events.value?.snapshot))
const snapshotWarning = computed(() => asDict(actions.value?.snapshot_warning || portfolio.value?.snapshot_warning || events.value?.snapshot_warning))
const tracePayloadMeta = computed(() => asDict(traceData.value))
const payloadFreshnessItems = computed(() => [
  {
    label: 'Actions',
    generated_at: actions.value?.generated_at,
    snapshot: actions.value?.snapshot,
    warning: actions.value?.snapshot_warning,
    source: 'actions'
  },
  {
    label: 'Portfolio',
    generated_at: portfolio.value?.generated_at,
    snapshot: portfolio.value?.snapshot,
    warning: portfolio.value?.snapshot_warning,
    source: 'portfolio'
  },
  {
    label: 'Events',
    generated_at: events.value?.generated_at,
    snapshot: events.value?.snapshot,
    warning: events.value?.snapshot_warning,
    source: 'events'
  },
  {
    label: 'Trace',
    generated_at: tracePayloadMeta.value.generated_at,
    status: tracePayloadMeta.value.status,
    source: 'trace'
  },
  {
    label: 'Data inputs',
    generated_at: featureFreshness.value?.generated_at,
    status: featureFreshness.value?.status,
    source: 'feature_freshness'
  },
  {
    label: 'Identity',
    generated_at: identityIssues.value?.generated_at,
    status: identityIssues.value?.status,
    source: 'identity_issues',
    warning: identitySourceWarnings.value[0]
  }
])

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function nestedValue(source: unknown, path: string[]): unknown {
  let current = source
  for (const key of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return null
    current = (current as Dict)[key]
  }
  return current
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => String(item || '').trim()).filter(Boolean)
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function filterSymbolRows(rows: Dict[]): Dict[] {
  const target = symbol.value
  return rows.filter((row) => String(row.symbol || row.ticker || '').trim().toUpperCase() === target)
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
  return String(value)
}

function pct(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  const percent = Math.abs(num) <= 1 ? num * 100 : num
  return `${Math.round(percent * 10) / 10}%`
}

function money(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function firstValue(keys: string[]) {
  for (const key of keys) {
    const value = finalAction.value[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  return null
}

function numericFirstValue(keys: string[]) {
  for (const key of keys) {
    const value = Number(finalAction.value[key])
    if (Number.isFinite(value) && value > 0) return value
  }
  return null
}

function computeDerivedTarget() {
  const entry = numericFirstValue(['entry_price'])
  const stop = numericFirstValue(['recommended_stop_price', 'stop_price', 'invalidation_price'])
  if (!entry || !stop || stop >= entry) return null
  const riskPerShare = entry - stop
  return Math.round((entry + riskPerShare * 2) * 100) / 100
}

function buildRunupRead() {
  const entry = numericFirstValue(['entry_price'])
  const current = numericFirstValue(['current_price', 'last_price', 'reference_price'])
  const stop = numericFirstValue(['recommended_stop_price', 'stop_price', 'invalidation_price'])
  const target = displayTarget.value
  const zoneLow = numericFirstValue(['attractive_price_low'])
  const zoneHigh = numericFirstValue(['attractive_price_high'])
  const gainPct = entry && current ? ((current / entry) - 1) * 100 : null
  const riskPct = entry && stop && stop < entry ? ((entry / stop) - 1) * 100 : null
  const targetProgressPct = entry && current && target && target > entry ? ((current - entry) / (target - entry)) * 100 : null
  const zoneDistancePct = current && zoneHigh ? ((current / zoneHigh) - 1) * 100 : null
  let status = 'Insufficient price context.'
  let tone = 'bg-white/75 text-ink/65'
  if (targetProgressPct !== null && targetProgressPct >= 100) {
    status = 'Target zone has been reached. New participation should wait for a fresh setup or partial-exit review.'
    tone = 'bg-rust/10 text-rust'
  } else if (targetProgressPct !== null && targetProgressPct >= 75) {
    status = 'Most of the implied target move is already done. Avoid chasing full size; wait for pullback or a new trigger.'
    tone = 'bg-sun/25 text-ink'
  } else if (targetProgressPct !== null && targetProgressPct >= 50) {
    status = 'This has already moved meaningfully from entry. Participation is possible only with reduced size and a clear stop.'
    tone = 'bg-sun/15 text-ink'
  } else if (targetProgressPct !== null) {
    status = 'Move is still below halfway to the implied target, but use the displayed stop and entry-zone context.'
    tone = 'bg-moss/10 text-moss'
  }
  if (zoneDistancePct !== null && zoneDistancePct > 5) {
    status = `${status} Current price is ${Math.round(zoneDistancePct * 10) / 10}% above the attractive zone high.`
  }
  return {
    entry,
    current,
    stop,
    target,
    targetIsDerived: explicitTarget.value === null && derivedTarget.value !== null,
    gainPct,
    riskPct,
    targetProgressPct,
    zoneLow,
    zoneHigh,
    zoneDistancePct,
    status,
    tone
  }
}

function mergeRows(...rows: Array<Dict | undefined>): Dict {
  const merged: Dict = {}
  for (const row of rows) {
    if (!row || typeof row !== 'object') continue
    for (const [key, value] of Object.entries(row)) {
      if (merged[key] !== null && merged[key] !== undefined && merged[key] !== '') continue
      if (value === null || value === undefined || value === '') continue
      merged[key] = value
    }
  }
  return merged
}

function statusClass(value: unknown) {
  const status = String(value || '').toUpperCase()
  if (status.includes('SELL') || status.includes('EXIT') || status.includes('REDUCE')) return 'bg-rust text-paper'
  if (status.includes('BUY') || status.includes('HOLD') || status.includes('WATCH')) return 'bg-moss text-paper'
  if (status.includes('MANUAL') || status.includes('REVIEW')) return 'bg-sun text-ink'
  return 'bg-ink text-paper'
}

function dataInputClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'fresh' || status === 'ok') return 'bg-moss text-paper'
  if (status === 'stale' || status === 'warning') return 'bg-sun text-ink'
  if (status === 'missing' || status === 'error' || status === 'blocked') return 'bg-rust text-paper'
  return 'bg-ink/10 text-ink'
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

function actionTransitionSummary(row: Dict) {
  const transition = asDict(nestedValue(row, ['recommendation_reason', 'evidence', 'action_transition']))
  const required = stringArray(transition.required_preconditions)
  const missing = stringArray(transition.missing_preconditions)
  const preconditionStatus = String(transition.precondition_status || (missing.length ? 'incomplete' : '')).toLowerCase()
  const hasData = Object.keys(transition).length > 0
  const brokerCandidate = transition.broker_order_candidate === true
  const headline = !hasData
    ? ''
    : brokerCandidate
      ? 'This action can only move toward execution after a separate safety-gated preview and the listed preconditions are satisfied.'
      : 'This action is monitoring, policy, or review context and does not create a broker order.'
  return {
    hasData,
    stateEffect: String(transition.state_effect || '').trim(),
    nextRequiredStage: String(transition.next_required_stage || '').trim(),
    brokerCandidate,
    allowedAfterPreview: transition.allowed_after_preview === true,
    status: preconditionStatus || (hasData ? 'unknown' : ''),
    headline,
    required,
    missing,
    brokerBoundary: String(transition.broker_boundary || '').trim()
  }
}

function actionTransitionClass(row: Dict) {
  const summary = actionTransitionSummary(row)
  if (!summary.hasData) return 'bg-ink/10 text-ink'
  if (summary.missing.length) return 'bg-sun text-ink'
  if (summary.brokerCandidate) return 'bg-sky text-paper'
  return 'bg-ink/10 text-ink'
}

function collectFeatureGateEffects(rows: Dict[]) {
  const seen = new Set<string>()
  const out: Dict[] = []
  for (const row of rows) {
    for (const effect of asArray(row.feature_gate_effects)) {
      const item = asDict(effect)
      if (!Object.keys(item).length) continue
      const key = [item.stage, item.gate, item.original_action, JSON.stringify(item.blocked_inputs || [])].join('|')
      if (seen.has(key)) continue
      seen.add(key)
      out.push(item)
    }
  }
  return out
}

function featureGateTone(effect: Dict) {
  const stage = String(effect.stage || '').toLowerCase()
  if (stage === 'actions' || stage === 'risk' || stage === 'portfolio') return 'border-rust/20 bg-rust/10 text-rust'
  if (stage === 'lifecycle') return 'border-sun/30 bg-sun/15 text-ink'
  return 'border-moss/20 bg-moss/10 text-moss'
}

function stageGateClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'ok') return 'border-moss/20 bg-moss/10 text-moss'
  if (status === 'blocked' || status === 'error') return 'border-rust/20 bg-rust/10 text-rust'
  if (status === 'skipped' || status === 'not_configured') return 'border-ink/10 bg-ink/5 text-ink/70'
  return 'border-sun/30 bg-sun/15 text-ink'
}

function actionLabel(row: Dict) {
  return row.action_code || row.action || row.next_action || row.reason || row.alert_type || row.portfolio_status || row.status || 'NO ACTION'
}

function eventTitle(row: Dict) {
  return row.subject || row.title || row.event_type || row.unique_id || 'Event'
}

function eventSubtitle(row: Dict) {
  return row.concise_summary_text || row.summary || row.action_reason || row.reason || row.source_type || ''
}

function companyMemoryReview(row: Dict) {
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
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Symbol Detail</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="text-5xl font-black tracking-tight">{{ symbol }}</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          One read-only view for final action, recommendation evidence, live alerts, and events.
        </p>
      </div>
    </div>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="snapshotMeta" :warning="snapshotWarning" :generated-at="actions?.generated_at || portfolio?.generated_at || events?.generated_at" />
  <PayloadFreshnessStrip class="mt-4" :items="payloadFreshnessItems" />

  <section v-if="loadErrors.length" class="mt-6 grid gap-3">
    <ApiErrorBanner v-for="row in loadErrors" :key="row.title" :title="row.title" :error="row.error" />
  </section>

  <section
    v-if="symbolIdentityIssues.length || identitySourceWarnings.length"
    class="mt-6 rounded-3xl border border-rust/25 bg-rust/10 p-5 shadow-soft"
  >
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-rust">Identity / Source Blockers</p>
        <h2 class="mt-2 text-2xl font-black text-ink">Broker and reference mapping issues for {{ symbol }}</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">
          These are operational data issues, not investment recommendations. They can explain skipped OHLCV pulls, missing latest prices, and blocked execution previews until the Dhan/company mapping is fixed and the failed source is rerun.
        </p>
      </div>
      <NuxtLink class="rounded-full bg-rust px-4 py-2 text-sm font-black text-paper" :to="`/identity-issues?symbol=${encodeURIComponent(symbol)}`">
        Open identity workbench
      </NuxtLink>
    </div>
    <div class="mt-4 grid gap-3 md:grid-cols-4">
      <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Open:</b> {{ display(identityIssueSummary.total_open || symbolIdentityIssues.length) }}</p>
      <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Broker execution:</b> {{ display(identityIssueSummary.broker_execution_enabled) }}</p>
      <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Types:</b> {{ Object.keys(asDict(identityIssueSummary.by_type)).join(', ') || '-' }}</p>
      <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Source warnings:</b> {{ display(identitySourceWarnings.length) }}</p>
    </div>
    <div v-if="identitySourceWarnings.length" class="mt-4 grid gap-2">
      <p
        v-for="warning in identitySourceWarnings"
        :key="`${warning.source || 'identity'}-${warning.reason || 'warning'}`"
        class="rounded-2xl bg-white/80 px-4 py-3 text-sm leading-6 text-rust"
      >
        <b>{{ warning.title || 'Identity source warning' }}:</b> {{ warning.message || warning.reason }}
        <span class="text-ink/55">Action: {{ warning.operator_action || 'rerun source or refresh advisory' }}</span>
      </p>
    </div>
    <div v-if="symbolIdentityIssues.length" class="mt-4 grid gap-3 lg:grid-cols-2">
      <article
        v-for="issue in symbolIdentityIssues"
        :key="String(issue.issue_key || issue.manual_review_item_id || issue.symbol)"
        class="rounded-2xl bg-white/85 p-4"
      >
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ display(issue.issue_type || 'identity issue') }}</p>
            <h3 class="mt-1 font-black text-ink">{{ display(issue.requested_exchange) }}:{{ display(issue.symbol) }}</h3>
          </div>
          <span class="rounded-full bg-rust/10 px-3 py-1 text-xs font-black text-rust">{{ display(issue.status || 'open') }}</span>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/70">{{ issue.error_text || issue.repair_hint || issue.suggested_action || 'Unresolved identity issue.' }}</p>
        <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
          <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Asset:</b> {{ display(issue.asset_type) }}</p>
          <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Source:</b> {{ display(issue.source) }}</p>
          <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Attempts:</b> {{ display(issue.attempt_count) }}</p>
          <p class="rounded-xl bg-paper/80 px-3 py-2"><b>Last seen:</b> {{ display(issue.last_seen_at) }}</p>
        </div>
        <p class="mt-3 rounded-xl bg-paper/80 px-3 py-2 text-sm leading-6 text-ink/70">
          <b>Repair:</b> {{ issue.repair_hint || issue.suggested_action || 'Refresh Dhan scrip master/company master and rerun the failed source.' }}
        </p>
      </article>
    </div>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-5">
    <MetricTile label="Final Action" :value="String(actionLabel(finalAction)).toUpperCase()" note="Consolidated latest row" />
    <MetricTile label="Current Price" :value="money(firstValue(['current_price', 'last_price', 'reference_price', 'entry_price']))" note="Best available payload price" />
    <MetricTile label="P&L" :value="pct(firstValue(['pnl_pct', 'return_pct']))" note="Since recommendation when available" />
    <MetricTile label="Target" :value="money(displayTarget)" :note="runupRead.targetIsDerived ? 'Derived 2R target; formal target missing' : 'Lifecycle/portfolio target'" />
    <MetricTile label="Stop" :value="money(firstValue(['stop_price', 'recommended_stop_price', 'invalidation_price']))" note="Exit or invalidation" />
  </section>

  <section class="mt-6 rounded-3xl border border-black/10 p-5 shadow-soft" :class="runupRead.tone">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] opacity-60">Participation Read</p>
        <h2 class="mt-2 text-2xl font-black">Can I still participate?</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6">{{ runupRead.status }}</p>
      </div>
      <span v-if="runupRead.targetIsDerived" class="rounded-full bg-white/80 px-3 py-1 text-xs font-black text-ink/60">
        formal target missing
      </span>
    </div>
    <div class="mt-4 grid gap-3 md:grid-cols-5">
      <p class="rounded-2xl bg-white/75 p-3 text-sm"><b>Entry:</b> {{ money(runupRead.entry) }}</p>
      <p class="rounded-2xl bg-white/75 p-3 text-sm"><b>Latest:</b> {{ money(runupRead.current) }}</p>
      <p class="rounded-2xl bg-white/75 p-3 text-sm"><b>Gain:</b> {{ pct(runupRead.gainPct) }}</p>
      <p class="rounded-2xl bg-white/75 p-3 text-sm"><b>Target progress:</b> {{ pct(runupRead.targetProgressPct) }}</p>
      <p class="rounded-2xl bg-white/75 p-3 text-sm"><b>Entry zone:</b> {{ runupRead.zoneLow && runupRead.zoneHigh ? `${money(runupRead.zoneLow)} - ${money(runupRead.zoneHigh)}` : '-' }}</p>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.05fr_0.95fr]">
    <div class="glass-panel rounded-3xl p-6">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Decision</p>
          <h2 class="mt-2 text-2xl font-black">Why this action exists</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(actionLabel(finalAction))">{{ actionLabel(finalAction) }}</span>
      </div>
      <p class="mt-3 text-sm leading-6 text-ink/65">
        {{ finalAction.action_reason || finalAction.reason || finalAction.reason_detail || finalAction.action_summary || 'No final action reason was available in the compact payload.' }}
      </p>
      <ReasonContractPanel v-if="reasonContract || reasonStatus" class="mt-4" :contract="reasonContract" :status="reasonStatus" />
      <section
        v-if="actionTransitionSummary(finalAction).hasData"
        class="mt-4 rounded-2xl border border-sky/20 bg-sky/10 p-4"
      >
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-sky">Execution preconditions</p>
            <p class="mt-1 text-sm leading-6 text-ink/65">
              {{ actionTransitionSummary(finalAction).headline }}
            </p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="actionTransitionClass(finalAction)">
            {{ actionTransitionSummary(finalAction).status || 'tracked' }}
          </span>
        </div>
        <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
          <p class="rounded-xl bg-white/80 px-3 py-2">
            <b>State effect:</b> {{ actionTransitionSummary(finalAction).stateEffect || '-' }}
          </p>
          <p class="rounded-xl bg-white/80 px-3 py-2">
            <b>Next stage:</b> {{ actionTransitionSummary(finalAction).nextRequiredStage || '-' }}
          </p>
          <p class="rounded-xl bg-white/80 px-3 py-2">
            <b>After preview:</b> {{ actionTransitionSummary(finalAction).allowedAfterPreview ? 'eligible' : 'not eligible' }}
          </p>
        </div>
        <div v-if="actionTransitionSummary(finalAction).required.length" class="mt-3 rounded-xl bg-white/80 p-3">
          <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Required checks</p>
          <div class="mt-2 flex flex-wrap gap-2">
            <span
              v-for="item in actionTransitionSummary(finalAction).required.slice(0, 8)"
              :key="item"
              class="rounded-full bg-moss/10 px-3 py-1 text-xs font-bold text-moss"
            >
              {{ preconditionLabel(item) }}
            </span>
          </div>
        </div>
        <div v-if="actionTransitionSummary(finalAction).missing.length" class="mt-3 rounded-xl bg-rust/10 p-3">
          <p class="text-xs font-black uppercase tracking-[0.18em] text-rust">Missing before execution</p>
          <ul class="mt-2 space-y-1 text-sm leading-6 text-rust">
            <li v-for="item in actionTransitionSummary(finalAction).missing.slice(0, 6)" :key="item">- {{ preconditionLabel(item) }}</li>
          </ul>
        </div>
        <p v-if="actionTransitionSummary(finalAction).brokerBoundary" class="mt-3 rounded-xl bg-white/80 px-3 py-2 text-sm leading-6 text-ink/70">
          <b>Broker boundary:</b> {{ actionTransitionSummary(finalAction).brokerBoundary }}
        </p>
      </section>
      <section
        class="mt-4 rounded-2xl border p-4"
        :class="companyMemory.hasData ? 'border-moss/25 bg-moss/10' : 'border-black/10 bg-white/65'"
      >
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.22em] text-moss">Company Memory Review</p>
            <h3 class="mt-1 text-lg font-black">
              {{ companyMemory.hasData ? (companyMemory.signal || 'Review input') : 'Not generated yet' }}
            </h3>
            <p class="mt-2 text-sm leading-6 text-ink/65">
              {{ companyMemory.hasData ? (companyMemory.summary || companyMemory.thesis || 'Review generated without summary.') : 'Run advisory/company_memory stage to generate compact read-only company evidence for this symbol.' }}
            </p>
          </div>
          <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">{{ companyMemory.authority || 'review_input_only' }}</span>
        </div>
        <div v-if="companyMemory.hasData" class="mt-3 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-white/80 px-3 py-2"><b>Confidence:</b> {{ pct(companyMemory.confidence) }}</p>
          <p class="rounded-xl bg-white/80 px-3 py-2"><b>Conviction:</b> {{ display(companyMemory.conviction) }}</p>
          <p class="rounded-xl bg-white/80 px-3 py-2"><b>Status:</b> {{ companyMemory.status || '-' }}</p>
          <p class="rounded-xl bg-white/80 px-3 py-2"><b>Date:</b> {{ companyMemory.reviewDate || '-' }}</p>
          <p v-if="companyMemory.sourceContract.coverage_status" class="rounded-xl bg-white/80 px-3 py-2">
            <b>Evidence coverage:</b> {{ companyMemory.sourceContract.coverage_status }}
          </p>
        </div>
        <p v-if="companyMemory.hasData && companyMemory.missingSources.length" class="mt-3 rounded-xl bg-rust/10 px-3 py-2 text-sm font-semibold leading-6 text-rust">
          Missing compact evidence: {{ companyMemory.missingSources.join(', ') }}. Treat this review as partial.
        </p>
        <p v-if="companyMemory.hasData && companyMemory.thesis && companyMemory.thesis !== companyMemory.summary" class="mt-3 rounded-xl bg-white/75 p-3 text-sm leading-6 text-ink/70">
          {{ companyMemory.thesis }}
        </p>
        <div v-if="companyMemory.hasData" class="mt-3 grid gap-3 md:grid-cols-3">
          <div v-if="companyMemory.evidenceUsed.length" class="rounded-xl bg-white/80 p-3">
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Evidence used</p>
            <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
              <li v-for="item in companyMemory.evidenceUsed" :key="item">{{ item }}</li>
            </ul>
          </div>
          <div v-if="companyMemory.riskFlags.length" class="rounded-xl bg-white/80 p-3">
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Risk flags</p>
            <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
              <li v-for="item in companyMemory.riskFlags" :key="item">{{ item }}</li>
            </ul>
          </div>
          <div v-if="companyMemory.waitFor.length" class="rounded-xl bg-white/80 p-3">
            <p class="text-xs font-black uppercase tracking-[0.18em] text-ink/45">Wait for</p>
            <ul class="mt-2 space-y-1 text-sm leading-6 text-ink/70">
              <li v-for="item in companyMemory.waitFor" :key="item">{{ item }}</li>
            </ul>
          </div>
        </div>
      </section>
      <TechnicalDecisionPanel class="mt-4" :record="finalAction" />
      <details class="mt-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Show final action row</summary>
        <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(finalAction, null, 2) }}</pre>
      </details>
    </div>

    <div class="glass-panel rounded-3xl p-6">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">What Would Change This?</p>
      <h2 class="mt-2 text-2xl font-black">Exit, wait, and review conditions</h2>
      <div class="mt-4 space-y-3 text-sm">
        <p class="rounded-2xl bg-white/75 p-3"><b>Exit strategy:</b> {{ display(firstValue(['exit_strategy', 'active_exit_condition', 'exit_condition_status'])) }}</p>
        <p class="rounded-2xl bg-white/75 p-3"><b>Invalidation:</b> {{ display(firstValue(['invalidation_rule', 'invalidation_price', 'support_price'])) }}</p>
        <p class="rounded-2xl bg-white/75 p-3"><b>Horizon:</b> {{ display(firstValue(['expected_horizon_days', 'target_review_date', 'holding_window_days'])) }}</p>
        <p class="rounded-2xl bg-white/75 p-3"><b>Manual review:</b> {{ display(firstValue(['manual_revision_summary', 'manual_revision_pointers', 'review_action'])) }}</p>
      </div>

      <div v-if="policyChangeRows.length" class="mt-6 rounded-3xl border border-black/10 bg-white/70 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Policy Change Audit</p>
            <h3 class="mt-2 text-xl font-black">Stop and target changes</h3>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              Durable lifecycle policy changes applied to this symbol. Stop tightening changes are recorded only when the portfolio baseline was actually updated.
            </p>
          </div>
          <span class="rounded-full bg-sun/20 px-3 py-1 text-xs font-black text-ink">{{ policyChangeRows.length }} rows</span>
        </div>
        <div class="mt-4 space-y-3">
          <div v-for="row in policyChangeRows.slice(0, 5)" :key="String(row.change_id || `${row.changed_at}-${row.change_type}`)" class="rounded-2xl bg-paper/85 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">{{ display(row.change_type) }}</p>
                <p class="mt-1 text-sm text-ink/60">{{ display(row.reason) }}</p>
              </div>
              <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">{{ display(row.changed_at) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>Old:</b> {{ money(row.old_value) }}</p>
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>New:</b> {{ money(row.new_value) }}</p>
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>Source:</b> {{ display(row.source_action) }}</p>
            </div>
          </div>
        </div>
      </div>

      <div class="mt-6 rounded-3xl border border-black/10 bg-white/70 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Data Inputs Used</p>
            <h3 class="mt-2 text-xl font-black">Freshness contract</h3>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              Required stale/missing inputs can explain why an action is blocked or review-only. Optional missing inputs are shown so you know what evidence was unavailable.
            </p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="dataInputClass(featureFreshness?.status)">{{ String(featureFreshness?.status || 'unknown').toUpperCase() }}</span>
        </div>
        <div class="mt-4 grid gap-2 md:grid-cols-4">
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Fresh:</b> {{ display(dataInputCounts.fresh || 0) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Stale:</b> {{ display(dataInputCounts.stale || 0) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Missing:</b> {{ display(dataInputCounts.missing || 0) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Skipped:</b> {{ display(dataInputCounts.intentionally_skipped || 0) }}</p>
        </div>
        <div v-if="dataInputBlockers.length" class="mt-4 rounded-2xl bg-rust/10 p-4">
          <p class="text-sm font-black text-rust">Required input blockers</p>
          <ul class="mt-2 space-y-1 text-sm font-semibold text-rust">
            <li v-for="row in dataInputBlockers" :key="String(row.input_key)">{{ row.label || row.input_key }}: {{ row.status }} / {{ row.reason }}</li>
          </ul>
        </div>
        <details class="mt-4">
          <summary class="cursor-pointer text-sm font-black text-moss">Show all input checks</summary>
          <div class="mt-3 grid gap-2">
            <div v-for="row in dataInputs" :key="String(row.input_key)" class="rounded-2xl bg-paper/80 p-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <p class="font-black text-ink">{{ row.label || row.input_key }}</p>
                <span class="rounded-full px-3 py-1 text-xs font-black" :class="dataInputClass(row.status)">{{ String(row.status || 'unknown').toUpperCase() }}</span>
              </div>
              <p class="mt-1 text-sm text-ink/60">{{ row.purpose || '-' }}</p>
              <p class="mt-1 text-xs font-semibold text-ink/45">
                {{ row.table }} · latest {{ display(row.latest_at) }} · age {{ display(row.age_days) }}d · rows {{ display(row.row_count) }} · {{ row.reason || '-' }}
              </p>
            </div>
          </div>
        </details>
      </div>

      <div v-if="currentStageGates.length" class="mt-4 rounded-3xl border border-black/10 bg-white/70 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Current Stage Gates</p>
            <h3 class="mt-2 text-xl font-black">What would be blocked if the pipeline ran now</h3>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              This is a read-only current check. It explains stale/missing input consequences but does not change actions, portfolio rows, or broker execution until the advisory/action pipeline reruns.
            </p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="dataInputClass(currentStageGateSummary.blocked_stage_count ? 'blocked' : 'ok')">
            {{ display(currentStageGateSummary.blocked_stage_count || 0) }} BLOCKED
          </span>
        </div>
        <div class="mt-4 grid gap-2 md:grid-cols-3">
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Stages checked:</b> {{ display(currentStageGateSummary.stage_count || currentStageGates.length) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Error stages:</b> {{ display(currentStageGateSummary.error_stage_count || 0) }}</p>
          <p class="rounded-2xl bg-paper/80 p-3 text-sm"><b>Broker execution:</b> {{ display(asDict(currentStageGateSummary.operator_boundary).broker_execution_allowed) }}</p>
        </div>
        <div v-if="currentStageGateBlockedInputs.length" class="mt-4 rounded-2xl bg-rust/10 p-4">
          <p class="text-sm font-black text-rust">Stage blockers</p>
          <ul class="mt-2 space-y-1 text-sm font-semibold text-rust">
            <li v-for="row in currentStageGateBlockedInputs" :key="`${row.stage || ''}-${row.input_key || row.label}`">
              {{ row.stage ? `${row.stage}: ` : '' }}{{ row.label || row.input_key }} · {{ row.status }} · {{ row.reason || 'blocked' }}
            </li>
          </ul>
        </div>
        <details class="mt-4">
          <summary class="cursor-pointer text-sm font-black text-moss">Show stage gate details</summary>
          <div class="mt-3 grid gap-3 md:grid-cols-2">
            <div v-for="gate in currentStageGates" :key="String(gate.stage)" class="rounded-2xl border p-4" :class="stageGateClass(gate.status)">
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <p class="font-black">{{ display(gate.stage) }}</p>
                  <p class="mt-1 text-sm leading-6 opacity-80">{{ display(gate.gate_effect) }}</p>
                </div>
                <span class="rounded-full bg-white/80 px-3 py-1 text-xs font-black text-ink/65">{{ String(gate.status || 'unknown').toUpperCase() }}</span>
              </div>
              <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
                <p class="rounded-xl bg-white/75 px-3 py-2"><b>Symbols checked:</b> {{ display(gate.symbols_checked || 0) }}</p>
                <p class="rounded-xl bg-white/75 px-3 py-2"><b>Blocked symbols:</b> {{ display(gate.blocked_count || 0) }}</p>
              </div>
              <p class="mt-3 text-xs font-semibold opacity-70">Required inputs: {{ stringArray(gate.required_input_keys).join(', ') || '-' }}</p>
            </div>
          </div>
        </details>
      </div>

      <div v-if="Object.keys(decisionTimeFreshness).length || Object.keys(decisionTimeFreshnessSummary).length" class="mt-4 rounded-3xl border border-black/10 bg-paper/80 p-5">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Decision-Time Data Inputs</p>
            <h3 class="mt-2 text-xl font-black">What the action saw when it was created</h3>
            <p class="mt-2 text-sm leading-6 text-ink/60">
              This persisted snapshot is different from the current check above. Use it to understand why the original action was approved, blocked, or sent to review.
            </p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="dataInputClass(decisionTimeFreshness.status || decisionTimeFreshnessSummary.status)">{{ String(decisionTimeFreshness.status || decisionTimeFreshnessSummary.status || 'unknown').toUpperCase() }}</span>
        </div>
        <div class="mt-4 grid gap-2 md:grid-cols-4">
          <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Captured:</b> {{ display(decisionTimeFreshness.captured_at || decisionTimeFreshnessSummary.captured_at) }}</p>
          <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Fresh:</b> {{ display(asDict(decisionTimeFreshness.counts || decisionTimeFreshnessSummary.counts).fresh || 0) }}</p>
          <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Stale:</b> {{ display(asDict(decisionTimeFreshness.counts || decisionTimeFreshnessSummary.counts).stale || 0) }}</p>
          <p class="rounded-2xl bg-white/80 p-3 text-sm"><b>Missing:</b> {{ display(asDict(decisionTimeFreshness.counts || decisionTimeFreshnessSummary.counts).missing || 0) }}</p>
        </div>
      </div>

      <div v-if="featureGateEffects.length" class="mt-4 rounded-3xl border border-black/10 bg-paper/80 p-5">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Stage Gate Effects</p>
          <h3 class="mt-2 text-xl font-black">What stale or missing inputs changed</h3>
          <p class="mt-2 text-sm leading-6 text-ink/60">
            These rows explain the concrete pipeline effect: watch downgrade, manual review, deferred capital, lifecycle warning, or final action downgrade.
          </p>
        </div>
        <div class="mt-4 grid gap-3">
          <div v-for="effect in featureGateEffects" :key="`${effect.stage}-${effect.gate}-${effect.original_action || ''}`" class="rounded-2xl border p-4" :class="featureGateTone(effect)">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="text-sm font-black">{{ effect.label || effect.stage || 'Feature gate' }}</p>
                <p class="mt-1 text-sm leading-6 opacity-80">{{ effect.summary || effect.gate_effect || effect.gate || 'Required feature input was blocked.' }}</p>
              </div>
              <span class="rounded-full bg-white/80 px-3 py-1 text-xs font-black text-ink/65">{{ String(effect.status || 'blocked').toUpperCase() }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>Original action:</b> {{ display(effect.original_action) }}</p>
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>Broker allowed:</b> {{ display(effect.broker_execution_allowed) }}</p>
              <p class="rounded-xl bg-white/75 px-3 py-2"><b>Source:</b> {{ display(effect.source) }}</p>
            </div>
            <div v-if="asArray(effect.blocked_inputs).length" class="mt-3 rounded-xl bg-white/75 p-3">
              <p class="text-xs font-black uppercase tracking-[0.18em] opacity-60">Blocked inputs</p>
              <ul class="mt-2 space-y-1 text-sm leading-6">
                <li v-for="input in asArray(effect.blocked_inputs)" :key="String(asDict(input).input_key || asDict(input).label)">
                  <b>{{ asDict(input).label || asDict(input).input_key }}</b>
                  <span v-if="asDict(input).status"> · {{ asDict(input).status }}</span>
                  <span v-if="asDict(input).reason"> · {{ asDict(input).reason }}</span>
                </li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-2">
    <div class="glass-panel rounded-3xl p-6">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Action Rows</p>
      <h2 class="mt-2 text-2xl font-black">Consolidated actions and recommendations</h2>
      <div class="mt-5 space-y-3">
        <RecordCard v-for="(row, idx) in [...actionRows, ...recommendationRows]" :key="idx" :title="String(actionLabel(row))" :subtitle="String(row.action_reason || row.reason || row.action_summary || '')" :record="row">
          <template #badge>
            <span class="rounded-full px-3 py-1 text-xs font-bold" :class="statusClass(actionLabel(row))">{{ actionLabel(row) }}</span>
          </template>
          <ReasonContractPanel v-if="row.recommendation_reason || row.reason_contract_status" class="mt-4" :contract="row.recommendation_reason" :status="row.reason_contract_status" compact />
          <TechnicalDecisionPanel class="mt-4" :record="row" compact />
        </RecordCard>
        <p v-if="![...actionRows, ...recommendationRows].length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No action/recommendation rows for this symbol.</p>
      </div>
    </div>

    <div class="glass-panel rounded-3xl p-6">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Live Context</p>
      <h2 class="mt-2 text-2xl font-black">Alerts and events</h2>
      <div class="mt-5 space-y-3">
        <RecordCard v-for="(row, idx) in [...alertRows, ...eventRows].slice(0, 8)" :key="idx" :title="String(eventTitle(row))" :subtitle="String(eventSubtitle(row))" :record="row">
          <template #badge>
            <span class="rounded-full bg-ink px-3 py-1 text-xs font-bold text-paper">{{ row.source_type || row.alert_type || row.event_status || 'EVENT' }}</span>
          </template>
        </RecordCard>
        <p v-if="![...alertRows, ...eventRows].length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No alerts or events in the current payload for this symbol.</p>
      </div>
    </div>
  </section>

  <section class="mt-8">
    <TraceTimeline v-if="trace" :trace="trace" title="Symbol decision trace" sync-url />
    <p v-else class="glass-panel rounded-3xl p-6 text-ink/60">No symbol trace available.</p>
  </section>
</template>
