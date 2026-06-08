<script setup lang="ts">
import type { Dict, TraceSummary } from '~/types/api'

const api = useOperatorApi()
const route = useRoute()
const symbol = computed(() => String(route.params.symbol || '').trim().toUpperCase())

const [{ data: actions, error: actionsError }, { data: portfolio, error: portfolioError }, { data: events, error: eventsError }, { data: traceData, error: traceError }] = await Promise.all([
  useAsyncData(`symbol-actions-${symbol.value}`, () => api.getActions({ symbol: symbol.value, limit: 100 })),
  useAsyncData(`symbol-portfolio-${symbol.value}`, () => api.getPortfolio({ symbol: symbol.value, limit: 100 })),
  useAsyncData(`symbol-events-${symbol.value}`, () => api.getEvents(100, { symbol: symbol.value })),
  useAsyncData(`symbol-trace-${symbol.value}`, () => api.getSymbolTraceSummary(symbol.value, 250))
])

const trace = computed<TraceSummary | null>(() => traceData.value || null)
const loadErrors = computed(() => [
  { title: `${symbol.value} actions failed`, error: actionsError.value },
  { title: `${symbol.value} portfolio failed`, error: portfolioError.value },
  { title: `${symbol.value} events failed`, error: eventsError.value },
  { title: `${symbol.value} trace failed`, error: traceError.value }
].filter((row) => row.error))
const actionRows = computed(() => filterSymbolRows([...(actions.value?.top_action_recommendations || []), ...(actions.value?.action_recommendations || [])]))
const alertRows = computed(() => filterSymbolRows(actions.value?.alerts || []))
const todayRows = computed(() => filterSymbolRows(portfolio.value?.today_recommendations || []))
const currentRows = computed(() => filterSymbolRows(portfolio.value?.current_recommendations || []))
const portfolioRows = computed(() => filterSymbolRows(portfolio.value?.portfolio || []))
const lifecycleRows = computed(() => filterSymbolRows(portfolio.value?.lifecycle || []))
const recommendationRows = computed(() => [...todayRows.value, ...currentRows.value, ...portfolioRows.value, ...lifecycleRows.value])
const exitedRows = computed(() => filterSymbolRows(portfolio.value?.exited_recommendations || []))
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
const explicitTarget = computed(() => numericFirstValue(['target_price', 'recommended_target_price']))
const derivedTarget = computed(() => computeDerivedTarget())
const displayTarget = computed(() => explicitTarget.value ?? derivedTarget.value)
const runupRead = computed(() => buildRunupRead())
const snapshotMeta = computed(() => asDict(actions.value?.snapshot || portfolio.value?.snapshot || events.value?.snapshot))
const snapshotWarning = computed(() => asDict(actions.value?.snapshot_warning || portfolio.value?.snapshot_warning || events.value?.snapshot_warning))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
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

function actionLabel(row: Dict) {
  return row.action_code || row.action || row.next_action || row.reason || row.alert_type || row.portfolio_status || row.status || 'NO ACTION'
}

function eventTitle(row: Dict) {
  return row.subject || row.title || row.event_type || row.unique_id || 'Event'
}

function eventSubtitle(row: Dict) {
  return row.concise_summary_text || row.summary || row.action_reason || row.reason || row.source_type || ''
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Symbol Detail</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="text-5xl font-black tracking-tight">{{ symbol }}</h1>
        <p class="mt-4 max-w-3xl text-lg leading-8 text-paper/70">
          One read-only view for final action, recommendation evidence, live alerts, events, and decision trace.
        </p>
      </div>
      <NuxtLink class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink" :to="`/decision-trace?symbol=${encodeURIComponent(symbol)}`">
        Open full trace
      </NuxtLink>
    </div>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="snapshotMeta" :warning="snapshotWarning" :generated-at="actions?.generated_at || portfolio?.generated_at || events?.generated_at" />

  <section v-if="loadErrors.length" class="mt-6 grid gap-3">
    <ApiErrorBanner v-for="row in loadErrors" :key="row.title" :title="row.title" :error="row.error" />
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
          <NuxtLink v-if="row.unique_id" class="mt-4 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink" :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id))}`">
            Open event trace
          </NuxtLink>
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
