<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
type SourceFailureRow = Dict & {
  failed_symbol_list: string[]
  issue_count: number
}
const [{ data: summary, error: summaryError }, { data: details, refresh, error: detailsError }, { data: ingestionState, refresh: refreshIngestionState, error: ingestionStateError }] = await Promise.all([
  useAsyncData('summary-health', () => api.getSummary()),
  useAsyncData('operator-health-details', () => api.getHealthDetails()),
  useAsyncData('operator-ingestion-state', () => api.getIngestionState({ limit: 1000, sample_limit: 8 }))
])

const sections = computed(() => details.value?.sections || {})
const tableFreshness = computed(() => asList(sections.value.table_freshness))
const cronLogs = computed(() => asList(sections.value.cron_logs))
const optionalDeps = computed(() => asList(sections.value.optional_dependencies))
const database = computed(() => asDict(sections.value.database))
const operatorApi = computed(() => asDict(sections.value.operator_api))
const traceSummaries = computed(() => asDict(sections.value.trace_summaries))
const redis = computed(() => asDict(sections.value.redis))
const dhan = computed(() => asDict(sections.value.dhan))
const dhanCache = computed(() => asDict(sections.value.dhan_cache))
const frontend = computed(() => asDict(sections.value.frontend))
const operatorSnapshot = computed(() => asDict(sections.value.operator_snapshot))
const summarySnapshot = computed(() => asDict(summary.value?.snapshot))
const summarySnapshotWarning = computed(() => asDict(summary.value?.snapshot_warning))
const apiLatencyProbe = computed(() => asDict(sections.value.api_latency_probe))
const slowOperations = computed(() => asDict(sections.value.slow_operations))
const slowIssues = computed(() => asList(slowOperations.value.issues))
const syncStateFailures = computed(() => asList(sections.value.sync_state_failures))
const watcherSourceCounters = computed(() => asDict(sections.value.watcher_source_counters))
const watcherSourceRows = computed(() => asList(watcherSourceCounters.value.rows))
const downloaderRunState = computed(() => asDict(sections.value.downloader_run_state))
const downloaderRunRows = computed(() => asList(downloaderRunState.value.rows))
const sourceFailureRows = computed<SourceFailureRow[]>(() => downloaderRunRows.value
  .map((row): SourceFailureRow => ({
    ...row,
    failed_symbol_list: failedSymbolList(row),
    issue_count: sourceIssueCount(row)
  }))
  .filter((row) => row.failed_symbol_list.length || row.issue_count > 0 || Number(row.reference_mapping_missing_count || 0) > 0 || Number(row.source_unavailable_count || 0) > 0 || Number(row.no_data_count || 0) > 0 || Number(row.auth_unavailable_count || 0) > 0)
  .slice(0, 6))
const ingestionStateSummary = computed(() => asDict(ingestionState.value?.summary))
const ingestionStateSamples = computed(() => {
  const enriched = asList(ingestionStateSummary.value.enriched_sample_rows)
  return enriched.length ? enriched : asList(ingestionStateSummary.value.sample_rows)
})
const ingestionStatusCounts = computed(() => dictEntries(asDict(ingestionStateSummary.value.status_counts)))
const ingestionSourceCounts = computed(() => dictEntries(asDict(ingestionStateSummary.value.source_counts)))
const ingestionClassificationCounts = computed(() => dictEntries(asDict(ingestionStateSummary.value.classification_counts)))
const ingestionClassificationDetails = computed(() => asDict(ingestionStateSummary.value.classification_details))
const ingestionBoundary = computed(() => asDict(ingestionState.value?.operator_boundary))
const degradationFeed = computed(() => asDict(sections.value.degradation_feed))
const fallbackTelemetry = computed(() => asDict(sections.value.fallback_telemetry))
const fallbackRows = computed(() => asList(fallbackTelemetry.value.rows))
const degradationLifecycle = computed(() => asDict(degradationFeed.value.lifecycle))
const degradationLifecycleGroups = computed(() => asList(degradationLifecycle.value.groups))
const supersededPreview = computed(() => asDict(degradationLifecycle.value.superseded_preview))
const supersededEventSamples = computed(() => asList(supersededPreview.value.event_processing_sample))
const supersededDocumentSamples = computed(() => asList(supersededPreview.value.announcement_document_sample))
const degradations = computed(() => asList(degradationFeed.value.rows))
const fixHints = computed(() => asList(details.value?.fix_hints))
const currentBlockers = computed(() => asDict(details.value?.current_blockers))
const currentBlockerRows = computed(() => asList(currentBlockers.value.rows))
const compactMeta = computed(() => asDict(details.value?.compact_meta))
const deferredDiagnostics = computed(() => asDict(details.value?.deferred_diagnostics))
const deferredDiagnosticRows = computed(() => asList(deferredDiagnostics.value.rows))
const trustGate = computed(() => asDict(sections.value.trust_gate))
const trustGateChecks = computed(() => asList(trustGate.value.checks))
const healthFilter = ref('all')
const degradationFilter = ref('active')
const degradationKindFilter = ref('all')
const slowIssueOperatorId = ref('operator')
const slowIssueNotes = ref<Record<string, string>>({})
const slowIssueSaving = ref('')
const slowIssueError = ref('')
const slowIssueSuccess = ref('')
const supersededCleanupOperatorId = ref('operator')
const supersededCleanupReason = ref('')
const supersededCleanupApplying = ref(false)
const supersededCleanupError = ref('')
const supersededCleanupResult = ref<Dict | null>(null)
const loadErrors = computed(() => [
  { title: 'Summary payload failed', error: summaryError.value },
  { title: 'Operator health details failed', error: detailsError.value },
  { title: 'Ingestion state summary failed', error: ingestionStateError.value }
].filter((row) => row.error))
const healthFilters = [
  { key: 'all', label: 'All' },
  { key: 'error', label: 'Errors' },
  { key: 'warn', label: 'Warnings' },
  { key: 'recovered', label: 'Recovered' },
  { key: 'ok', label: 'OK' }
]
const degradationFilters = [
  { key: 'active', label: 'Active' },
  { key: 'error', label: 'Errors' },
  { key: 'warn', label: 'Warnings' },
  { key: 'recovered', label: 'Recovered' },
  { key: 'all', label: 'All' }
]
const filteredCronLogs = computed(() => cronLogs.value.filter((row) => matchesHealthFilter(row, healthFilter.value)))
const filteredFixHints = computed(() => fixHints.value.filter((row) => matchesHealthFilter(row, healthFilter.value)))
const degradationKindOptions = computed(() => {
  const counts: Record<string, number> = {}
  for (const row of degradations.value) {
    const kind = String(row.kind || 'unknown')
    counts[kind] = (counts[kind] || 0) + 1
  }
  return [{ key: 'all', label: 'All Kinds', count: degradations.value.length }, ...Object.entries(counts).map(([key, count]) => ({ key, label: titleCase(key), count }))]
})
const filteredDegradations = computed(() => degradations.value.filter((row) => {
  const recovered = Boolean(row.recovered) || String(row.status || '').toLowerCase() === 'recovered'
  const status = String(row.status || row.severity || '').toLowerCase()
  const filterOk = degradationFilter.value === 'all'
    || (degradationFilter.value === 'active' && !recovered)
    || (degradationFilter.value === 'recovered' && recovered)
    || (degradationFilter.value === status && !recovered)
  const kindOk = degradationKindFilter.value === 'all' || String(row.kind || '') === degradationKindFilter.value
  return filterOk && kindOk
}))
const healthFilterCounts = computed(() => {
  const rows = [...cronLogs.value, ...fixHints.value, ...syncStateFailures.value, ...watcherSourceRows.value, ...downloaderRunRows.value, ...degradations.value]
  return Object.fromEntries(healthFilters.map((item) => [item.key, rows.filter((row) => matchesHealthFilter(row, item.key)).length]))
})

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)) : []
}

function ingestionClassDetail(value: unknown): Dict {
  return asDict(ingestionClassificationDetails.value[String(value || '')])
}

function ingestionRowClassDetail(row: Dict): Dict {
  const detail = asDict(row.classification_detail)
  return Object.keys(detail).length ? detail : ingestionClassDetail(row.classification)
}

function dictEntries(value: Dict) {
  return Object.entries(value)
    .map(([key, count]) => ({ key, count: Number(count || 0) }))
    .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key))
}

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'ok') return 'bg-moss text-paper'
  if (status === 'error') return 'bg-rust text-paper'
  return 'bg-sun text-ink'
}

function statusText(value: unknown) {
  return String(value || 'unknown').toUpperCase()
}

function trustLevelText(value: unknown) {
  return String(value || 'unknown').replaceAll('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function titleCase(value: unknown) {
  return String(value || '').replaceAll('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase())
}

function secondsText(value: unknown) {
  const seconds = Number(value)
  if (Number.isNaN(seconds)) return '-'
  if (seconds < 0) return 'expired'
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`
  return `${Math.round(seconds / 86400)}d`
}

function counterText(row: Dict, key: string) {
  const counters = asDict(row.counters)
  const value = Number(counters[key] || 0)
  return Number.isFinite(value) ? Intl.NumberFormat('en-IN').format(value) : '0'
}

function failedSymbolList(row: Dict): string[] {
  const raw = row.failed_symbols
  if (!Array.isArray(raw)) return []
  return raw
    .map((item) => {
      if (typeof item === 'object' && item !== null) {
        return String((item as Dict).symbol || (item as Dict).ticker || (item as Dict).identifier || JSON.stringify(item))
      }
      return String(item || '')
    })
    .map((item) => item.trim())
    .filter(Boolean)
}

function sourceIssueCount(row: Dict) {
  return Number(row.failed_attempt_count || 0)
    + Number(row.source_unavailable_count || 0)
    + Number(row.no_data_count || 0)
    + Number(row.auth_unavailable_count || 0)
    + Number(row.reference_mapping_missing_count || 0)
}

function isRecovered(row: Dict) {
  const status = String(row.latest_run_status || row.title || '').toLowerCase()
  return Boolean(row.recovered) || String(row.status || '').toLowerCase() === 'recovered' || status.includes('recovered') || status.includes('ok_after_historical_errors') || String(row.title || '').toLowerCase().includes('historical cron errors recovered')
}

function matchesHealthFilter(row: Dict, filter: string) {
  if (filter === 'all') return true
  if (filter === 'recovered') return isRecovered(row)
  if (filter === 'ok') return String(row.status || '').toLowerCase() === 'ok'
  if (filter === 'error') return String(row.status || '').toLowerCase() === 'error'
  if (filter === 'warn') return String(row.status || '').toLowerCase() === 'warn' && !isRecovered(row)
  return true
}

function slowIssueRoute(row: Dict) {
  const details = asDict(row.last_details)
  return details.route || details.endpoint || row.operation || '-'
}

function slowIssueFix(row: Dict) {
  const route = String(slowIssueRoute(row)).toLowerCase()
  if (route.includes('/trace/summary')) return 'Rebuild trace summaries from Operations; cache misses should disappear after materialization.'
  if (route.includes('/events') || route.includes('/actions') || route.includes('/portfolio')) return 'Use filters/compact payloads first; add narrower server-side filters if this route remains slow.'
  if (String(row.kind || '').includes('large_response')) return 'Prefer compact list payloads and detail-on-demand for this route.'
  return 'Inspect the endpoint payload and add pagination/materialization if this remains open.'
}

async function refreshHealthPage() {
  await Promise.all([refresh(), refreshIngestionState()])
}

async function updateSlowIssue(issue: Dict, status: string) {
  const fingerprint = String(issue.fingerprint || '')
  if (!fingerprint) return
  slowIssueError.value = ''
  slowIssueSuccess.value = ''
  const note = (slowIssueNotes.value[fingerprint] || '').trim()
  if ((status === 'fixed' || status === 'ignored') && !note) {
    slowIssueError.value = 'A note is required before marking a slow issue fixed or ignored.'
    return
  }
  slowIssueSaving.value = `${fingerprint}:${status}`
  try {
    await api.updateSlowIssueStatus({
      fingerprint,
      status,
      note,
      operator_id: slowIssueOperatorId.value || 'operator'
    })
    slowIssueSuccess.value = `${fingerprint} marked ${status}.`
    slowIssueNotes.value[fingerprint] = ''
    await refreshHealthPage()
  } catch (err) {
    slowIssueError.value = err instanceof Error ? err.message : String(err)
  } finally {
    slowIssueSaving.value = ''
  }
}

async function applySupersededCleanup() {
  const reason = supersededCleanupReason.value.trim()
  if (!reason) {
    supersededCleanupError.value = 'A reason is required before marking recovered failures superseded.'
    return
  }
  supersededCleanupError.value = ''
  supersededCleanupResult.value = null
  supersededCleanupApplying.value = true
  try {
    const result = await api.applySupersededCleanup({
      confirm: true,
      operator_id: supersededCleanupOperatorId.value || 'operator',
      requested_reason: reason,
      limit: 500
    })
    supersededCleanupResult.value = result as unknown as Dict
    supersededCleanupReason.value = ''
    await refreshHealthPage()
  } catch (err) {
    supersededCleanupError.value = err instanceof Error ? err.message : String(err)
  } finally {
    supersededCleanupApplying.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Operator Health</p>
    <div class="mt-4 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 class="max-w-4xl text-5xl font-black tracking-tight">Runtime checks before debugging strategy output.</h1>
        <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
          Generated {{ details?.generated_at || '-' }}. Dashboard payload generated {{ summary?.generated_at || '-' }}.
        </p>
      </div>
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink" type="button" @click="refreshHealthPage()">
        Refresh
      </button>
    </div>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="summarySnapshot" :warning="summarySnapshotWarning" :generated-at="summary?.generated_at" />

  <section v-if="loadErrors.length" class="mt-6 grid gap-3">
    <ApiErrorBanner v-for="row in loadErrors" :key="row.title" :title="row.title" :error="row.error" />
  </section>

  <section v-if="details?.compact" class="mt-6 rounded-3xl border border-sun/30 bg-sun/10 p-5">
    <p class="text-xs font-black uppercase tracking-[0.25em] text-sun">Compact health payload</p>
    <p class="mt-2 text-sm leading-6 text-ink/65">
      Health uses bounded lists and truncated long strings for fast loads.
      {{ compactMeta.omitted_list_items || 0 }} list item(s) and {{ compactMeta.truncated_strings || 0 }} long string(s) were omitted/truncated in this response.
      Use <code class="rounded bg-white/70 px-1 py-0.5">/api/health/details?mode=full&amp;compact=false</code> only for short debugging sessions.
    </p>
  </section>

  <section v-if="deferredDiagnosticRows.length" class="mt-6 rounded-3xl border border-moss/25 bg-moss/10 p-5">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-moss">Deferred diagnostics</p>
        <h2 class="mt-2 text-2xl font-black">{{ deferredDiagnostics.count || deferredDiagnosticRows.length }} deep check(s) skipped in fast Health</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">
          {{ deferredDiagnostics.operator_action || 'Fast Health intentionally skips expensive diagnostics. Run full Health when you need the listed evidence.' }}
        </p>
      </div>
      <code class="max-w-full overflow-auto rounded-2xl bg-ink px-3 py-2 text-xs text-paper">
        {{ deferredDiagnostics.full_diagnostics_command || 'python -m advisory.operator_health --full --skip-dhan' }}
      </code>
    </div>
    <details class="mt-4">
      <summary class="cursor-pointer text-sm font-black text-moss">Show deferred checks</summary>
      <div class="mt-4 grid gap-3 lg:grid-cols-2">
        <article v-for="row in deferredDiagnosticRows" :key="String(row.section || row.message)" class="rounded-2xl bg-white/75 p-4">
          <p class="font-black text-ink">{{ titleCase(row.section) }}</p>
          <p class="mt-1 text-sm leading-6 text-ink/60">{{ row.reason || row.message || '-' }}</p>
          <code class="mt-3 block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">
            {{ row.command || deferredDiagnostics.full_diagnostics_command || 'python -m advisory.operator_health --full --skip-dhan' }}
          </code>
        </article>
      </div>
    </details>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-10">
    <MetricTile label="Overall" :value="statusText(details?.status)" note="Worst current check" />
    <MetricTile label="DB" :value="statusText(database.status)" :note="String(database.message || '-')" />
    <MetricTile label="API" :value="statusText(operatorApi.status)" :note="operatorApi.latency_ms ? `${operatorApi.latency_ms} ms` : String(operatorApi.message || '-')" />
    <MetricTile label="Trace Cache" :value="statusText(traceSummaries.status)" :note="`${traceSummaries.row_count ?? 0} summaries`" />
    <MetricTile label="Snapshot" :value="statusText(operatorSnapshot.status)" :note="operatorSnapshot.age_seconds ? `${Math.round(Number(operatorSnapshot.age_seconds) / 60)}m old` : String(operatorSnapshot.message || '-')" />
    <MetricTile label="Slowlog" :value="statusText(slowOperations.status)" :note="`${slowOperations.returned_count ?? 0} open`" />
    <MetricTile label="Redis" :value="statusText(redis.status)" :note="String(redis.message || '-')" />
    <MetricTile label="Dhan" :value="statusText(dhan.status)" :note="String(dhan.message || '-')" />
    <MetricTile label="Token" :value="statusText(dhanCache.status)" :note="`expires ${secondsText(dhanCache.seconds_to_expiry)}`" />
    <MetricTile label="Frontend" :value="statusText(frontend.status)" :note="String(frontend.message || '-')" />
  </section>

  <section class="mt-8 rounded-[2rem] border p-6 shadow-soft" :class="trustGate.status === 'error' ? 'border-rust/25 bg-rust/10' : trustGate.status === 'warn' ? 'border-sun/30 bg-sun/15' : 'border-moss/25 bg-moss/10'">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.3em] text-ink/45">Advisory Trust Gate</p>
        <h2 class="mt-2 text-3xl font-black">{{ trustLevelText(trustGate.trust_level) }}</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/70">
          {{ trustGate.recommendation || 'Health has not produced a trust-gate recommendation yet.' }}
        </p>
      </div>
      <span class="rounded-full px-4 py-2 text-sm font-black" :class="statusClass(trustGate.status)">
        {{ statusText(trustGate.status) }}
      </span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-4">
      <MetricTile label="Checks" :value="String(trustGate.count || 0)" note="Open trust checks" />
      <MetricTile label="Errors" :value="String(trustGate.error_count || 0)" note="Block usage" />
      <MetricTile label="Warnings" :value="String(trustGate.warn_count || 0)" note="Review-only" />
      <MetricTile label="Decision" :value="trustLevelText(trustGate.trust_level)" note="Use boundary" />
    </div>
    <div v-if="trustGateChecks.length" class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="row in trustGateChecks" :key="String(row.key || row.title)" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ row.title || 'Trust check' }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ row.reason || '-' }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
        </div>
        <p class="mt-3 rounded-2xl bg-paper/80 p-3 text-sm font-semibold text-ink/70">
          {{ row.impact || 'Triage this before relying on the latest advisory output.' }}
        </p>
        <details v-if="Object.keys(asDict(row.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show check details</summary>
          <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-paper/80 p-3 text-xs">{{ JSON.stringify(row.details, null, 2) }}</pre>
        </details>
      </article>
    </div>
    <p v-else class="mt-5 rounded-2xl bg-white/75 p-4 text-sm text-ink/65">
      No trust-gate blockers. Normal operator review and broker-safety gates still apply.
    </p>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Current Blockers</p>
        <h2 class="mt-2 text-2xl font-black">What blocks advisory trust now</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Prioritized from active health errors, stale inputs, pipeline failures, degradation rows, and fix hints.
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(currentBlockers.status)">
        {{ statusText(currentBlockers.status) }} · {{ currentBlockers.count || 0 }} open
      </span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-4">
      <MetricTile label="Open" :value="String(currentBlockers.count || 0)" note="Trust blockers" />
      <MetricTile label="Errors" :value="String(currentBlockers.error_count || 0)" note="Highest priority" />
      <MetricTile label="Warnings" :value="String(currentBlockers.warn_count || 0)" note="Needs triage" />
      <MetricTile label="Categories" :value="String(Object.keys(asDict(currentBlockers.counts_by_category)).length)" note="Affected areas" />
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="row in currentBlockerRows" :key="`${row.category}-${row.title}-${row.reason}`" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ row.title || 'Health blocker' }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ row.reason || '-' }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
        </div>
        <div class="mt-3 grid gap-2 text-xs md:grid-cols-2">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Category:</b> {{ titleCase(row.category) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Source:</b> {{ row.source || '-' }}</p>
        </div>
        <p class="mt-3 rounded-2xl bg-sun/15 p-3 text-sm font-semibold text-ink/70">
          {{ row.trust_impact || 'Triage before relying on fresh advisory output.' }}
        </p>
        <div v-if="asStringList(row.commands).length" class="mt-3 space-y-2">
          <code v-for="command in asStringList(row.commands).slice(0, 3)" :key="command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">
            {{ command }}
          </code>
        </div>
        <details v-if="Object.keys(asDict(row.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show blocker details</summary>
          <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-paper/80 p-3 text-xs">{{ JSON.stringify(row.details, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!currentBlockerRows.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No active advisory trust blockers.</p>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Trace Cache</p>
        <h2 class="mt-2 text-2xl font-black">Materialized trace-summary performance</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Symbol and event trace pages should read this cache first. Cache misses fall back to live trace builds and appear in the degradation feed.
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(traceSummaries.status)">{{ statusText(traceSummaries.status) }}</span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-4">
      <MetricTile label="Rows" :value="String(traceSummaries.row_count || 0)" note="Materialized summaries" />
      <MetricTile label="Age" :value="traceSummaries.age_hours !== undefined && traceSummaries.age_hours !== null ? `${Math.round(Number(traceSummaries.age_hours) * 10) / 10}h` : '-'" note="Since latest generation" />
      <MetricTile label="Generated" :value="String(traceSummaries.latest_generated_at || '-')" note="Latest cache write" />
      <MetricTile label="Status" :value="statusText(traceSummaries.status)" :note="String(traceSummaries.message || '-')" />
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="row in asList(traceSummaries.rows)" :key="String(row.entity_type)" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ titleCase(row.entity_type) }}</p>
            <p class="mt-1 text-sm text-ink/60">Latest source {{ row.latest_source_max_ts || '-' }}</p>
          </div>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper">{{ row.row_count || 0 }} rows</span>
        </div>
        <p class="mt-2 text-xs text-ink/50">Generated {{ row.latest_generated_at || '-' }}</p>
      </article>
      <p v-if="!asList(traceSummaries.rows).length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No trace-summary cache rows found yet.</p>
    </div>
    <div class="mt-5 grid gap-2">
      <code class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">python -m advisory.trace_summary_store --symbol-limit 150 --event-limit 150 --trace-limit 100</code>
      <code class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">python -m advisory.trace_summary_store --cleanup --keep-latest-per-entity 1 --older-than-days 14 --dry-run</code>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Fix Hints</p>
        <h2 class="mt-2 text-2xl font-black">What to do next</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Generated from failing or stale health checks. Start with error hints, then warnings.
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(details?.status)">{{ statusText(details?.status) }}</span>
    </div>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="item in healthFilters"
        :key="item.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="healthFilter === item.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="healthFilter = item.key"
      >
        {{ item.label }} · {{ healthFilterCounts[item.key] || 0 }}
      </button>
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="hint in filteredFixHints" :key="String(hint.title || hint.reason)" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="font-black text-ink">{{ hint.title }}</p>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ hint.reason }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(hint.status)">{{ statusText(hint.status) }}</span>
        </div>
        <div v-if="asStringList(hint.commands).length" class="mt-3 space-y-2">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Commands</p>
          <code v-for="command in asStringList(hint.commands)" :key="command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">
            {{ command }}
          </code>
        </div>
        <details v-if="Object.keys(asDict(hint.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show hint details</summary>
          <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-paper/80 p-3 text-xs">{{ JSON.stringify(hint.details, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!filteredFixHints.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No fix hints for this filter.</p>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Fallbacks & Degradation</p>
        <h2 class="mt-2 text-2xl font-black">Skipped symbols, fallbacks, and partial-data runs</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          This is where Dhan master misses, Dhan no-data skips, NSE retries, LLM fallbacks, OCR failures, Redis/Postgres reconnects, and slow endpoints surface without reading cron logs.
        </p>
      </div>
      <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(degradationFeed.status)">
        {{ statusText(degradationFeed.status) }} · {{ degradationFeed.active_count || 0 }} active
      </span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-4">
      <MetricTile label="Active" :value="String(degradationFeed.active_count || 0)" note="Not recovered yet" />
      <MetricTile label="Recovered" :value="String(degradationFeed.recovered_count || 0)" note="Historical markers" />
      <MetricTile label="Kinds" :value="String(Object.keys(asDict(degradationFeed.counts_by_kind)).length)" note="Degradation categories" />
      <MetricTile label="Shown" :value="String(filteredDegradations.length)" note="After filters" />
    </div>
    <div class="mt-5 rounded-2xl border border-black/10 bg-white/75 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Watcher Source Counters</p>
          <p class="mt-2 text-sm leading-6 text-ink/65">
            {{ watcherSourceCounters.message || 'Latest OHLCV/news/announcement watcher counters have not been recorded yet.' }}
          </p>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(watcherSourceCounters.status)">
          {{ statusText(watcherSourceCounters.status) }} · {{ watcherSourceCounters.returned_count || 0 }} sources
        </span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <MetricTile label="Sources" :value="String(watcherSourceCounters.returned_count || 0)" note="OHLCV/news/announcements" />
        <MetricTile label="Stale" :value="String(watcherSourceCounters.stale_count || 0)" note="Older than max age" />
        <MetricTile label="Errors" :value="String(watcherSourceCounters.error_count || 0)" note="Sync-state errors" />
        <MetricTile label="Missing" :value="String(asStringList(watcherSourceCounters.missing_sources).length)" note="No sync row" />
      </div>
      <div class="mt-4 grid gap-3 lg:grid-cols-3">
        <article v-for="row in watcherSourceRows" :key="String(row.source_name || row.watcher_source)" class="rounded-2xl bg-paper/80 p-4 text-sm">
          <div class="flex flex-wrap items-start justify-between gap-2">
            <div>
              <p class="font-black text-ink">{{ titleCase(row.watcher_source || row.source_name) }}</p>
              <p class="mt-1 text-xs font-semibold text-ink/45">
                {{ row.updated_at || '-' }} · age {{ row.age_minutes ?? '-' }}m
              </p>
            </div>
            <span class="rounded-full px-2 py-1 text-[0.65rem] font-black" :class="statusClass(row.status)">
              {{ statusText(row.status) }}
            </span>
          </div>
          <p class="mt-2 text-sm leading-6 text-ink/60">
            {{ row.produced_data ? 'Fresh data or matched events were produced.' : 'No produced-data counter was positive in the latest run.' }}
          </p>
          <div class="mt-3 grid gap-2 text-xs">
            <template v-if="String(row.watcher_source) === 'ohlcv'">
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Symbols:</b> {{ counterText(row, 'symbol_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Latest prices:</b> {{ counterText(row, 'latest_price_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Alerts persisted/suppressed:</b> {{ counterText(row, 'alert_persisted_count') }} / {{ counterText(row, 'alert_suppressed_count') }}</p>
            </template>
            <template v-else-if="String(row.watcher_source) === 'news'">
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Watch rows:</b> {{ counterText(row, 'watch_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>RSS items:</b> {{ counterText(row, 'news_item_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Matched/persisted events:</b> {{ counterText(row, 'matched_event_count') }} / {{ counterText(row, 'persisted_event_count') }}</p>
            </template>
            <template v-else-if="String(row.watcher_source) === 'announcements'">
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Targets/runs:</b> {{ counterText(row, 'unique_ingest_targets') }} / {{ counterText(row, 'ingest_run_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Discovered/parsed/failed:</b> {{ counterText(row, 'discovered_count') }} / {{ counterText(row, 'parsed_count') }} / {{ counterText(row, 'failed_count') }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Matched/persisted events:</b> {{ counterText(row, 'match_count') }} / {{ counterText(row, 'persisted_event_count') }}</p>
            </template>
          </div>
          <p v-if="row.error" class="mt-3 rounded-xl bg-rust/10 p-2 text-xs font-semibold text-rust">{{ row.error }}</p>
        </article>
        <p v-if="!watcherSourceRows.length" class="rounded-2xl bg-paper/80 p-4 text-sm text-ink/50">No watcher source counter rows are available yet.</p>
      </div>
    </div>
    <div class="mt-5 rounded-2xl border border-black/10 bg-white/75 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Latest Downloader / Parser Run-State</p>
          <p class="mt-2 text-sm leading-6 text-ink/65">
            {{ downloaderRunState.message || 'No standardized downloader/parser run-state has been recorded yet.' }}
          </p>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(downloaderRunState.status)">
          {{ statusText(downloaderRunState.status) }}
        </span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-5">
        <MetricTile label="Runs" :value="String(downloaderRunState.returned_count || 0)" note="Latest source rows" />
        <MetricTile label="Advanced" :value="String(downloaderRunState.advanced_count || 0)" note="State moved forward" />
        <MetricTile label="Stalled" :value="String(downloaderRunState.stalled_count || 0)" note="No new rows/state" />
        <MetricTile label="Errors" :value="String(downloaderRunState.error_count || 0)" note="Needs fix" />
        <MetricTile label="Retries" :value="String(downloaderRunState.retry_count || 0)" note="Source attempts" />
      </div>
      <div v-if="sourceFailureRows.length" class="mt-4 rounded-2xl border border-rust/20 bg-rust/10 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.2em] text-rust">Failed Symbols / Source Skips</p>
            <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">
              These are source-specific failures from the latest downloader/parser run-state. They explain missing OHLCV, missing Dhan security ids, source outages, no-data windows, and partial symbol failures before they become confusing action or portfolio gaps.
            </p>
          </div>
          <span class="rounded-full bg-rust px-3 py-1 text-xs font-black text-paper">{{ sourceFailureRows.length }} source issue{{ sourceFailureRows.length === 1 ? '' : 's' }}</span>
        </div>
        <div class="mt-4 grid gap-3 lg:grid-cols-2">
          <article v-for="row in sourceFailureRows" :key="`source-issue-${row.source_name}-${row.updated_at}`" class="rounded-2xl bg-white/80 p-4 text-sm">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">{{ row.module || row.source_name || 'unknown source' }}</p>
                <p class="mt-1 text-xs font-semibold text-ink/45">{{ row.purpose || '-' }} · {{ titleCase(row.classification) }} · {{ row.updated_at || '-' }}</p>
              </div>
              <span class="rounded-full px-2 py-1 text-[0.65rem] font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-xs md:grid-cols-3">
              <p class="rounded-lg bg-paper/80 px-2 py-1"><b>Failed symbols:</b> {{ row.failed_symbol_list.length }}</p>
              <p class="rounded-lg bg-paper/80 px-2 py-1"><b>Mapping misses:</b> {{ row.reference_mapping_missing_count ?? 0 }}</p>
              <p class="rounded-lg bg-paper/80 px-2 py-1"><b>No-data/source:</b> {{ row.no_data_count ?? 0 }} / {{ row.source_unavailable_count ?? 0 }}</p>
            </div>
            <p v-if="row.failed_symbol_list.length" class="mt-3 rounded-xl bg-paper/80 p-2 text-xs font-semibold leading-5 text-rust">
              Symbols: {{ row.failed_symbol_list.slice(0, 12).join(', ') }}{{ row.failed_symbol_list.length > 12 ? '...' : '' }}
            </p>
            <p v-if="row.classification_operator_action" class="mt-3 rounded-xl bg-sun/15 p-2 text-xs font-semibold leading-5 text-ink/70">
              {{ row.classification_operator_action }}
            </p>
            <p v-if="row.error" class="mt-3 rounded-xl bg-rust/10 p-2 text-xs font-semibold text-rust">{{ row.error }}</p>
          </article>
        </div>
      </div>
      <details v-if="downloaderRunRows.length" class="mt-4" open>
        <summary class="cursor-pointer text-sm font-black text-moss">Show latest downloader/parser runs</summary>
        <div class="mt-3 grid gap-2 lg:grid-cols-2">
          <article v-for="row in downloaderRunRows.slice(0, 10)" :key="`${row.source_name}-${row.updated_at}`" class="rounded-xl bg-paper/80 p-3 text-sm">
            <div class="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p class="font-black text-ink">{{ row.module || row.source_name || 'unknown module' }}</p>
                <p class="mt-1 text-xs font-semibold text-ink/45">
                  {{ row.purpose || '-' }} · {{ titleCase(row.phase) }} · {{ row.updated_at || '-' }}
                </p>
              </div>
              <span class="rounded-full px-2 py-1 text-[0.65rem] font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-xs md:grid-cols-3">
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Class:</b> {{ titleCase(row.classification) }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Rows:</b> {{ row.rows_written ?? row.rows ?? '-' }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Advanced:</b> {{ row.state_advanced === true ? 'yes' : row.state_advanced === false ? 'no' : '-' }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Attempts:</b> {{ row.attempt_count ?? '-' }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Retries:</b> {{ row.retry_count ?? '-' }}</p>
              <p class="rounded-lg bg-white/70 px-2 py-1"><b>Fallbacks:</b> {{ row.fallback_count ?? (row.fallback_used ? 1 : 0) }}</p>
            </div>
            <p v-if="row.classification_meaning" class="mt-3 rounded-xl bg-white/70 p-2 text-xs leading-5 text-ink/65">
              <b>{{ row.classification_label || titleCase(row.classification) }}:</b>
              {{ row.classification_meaning }}
              <span v-if="row.classification_operator_action" class="mt-1 block font-semibold">
                {{ row.classification_operator_action }}
              </span>
            </p>
            <p v-if="row.error" class="mt-3 rounded-xl bg-rust/10 p-2 text-xs font-semibold text-rust">{{ row.error }}</p>
            <details class="mt-3">
              <summary class="cursor-pointer text-xs font-black text-moss">Raw state</summary>
              <pre class="mt-2 max-h-48 overflow-auto rounded-xl bg-ink p-3 text-xs text-paper">{{ JSON.stringify(row.state || {}, null, 2) }}</pre>
            </details>
          </article>
        </div>
      </details>
    </div>
    <div class="mt-5 rounded-2xl border border-black/10 bg-white/75 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">File-Level Ingestion State</p>
          <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/65">
            Failed files, valid empty source files, and parser classifications from <code>ingestion_file_state</code>.
            Use this to decide whether a repeated parser item is a retryable bad download, schema drift, parser bug, or a valid no-row source.
          </p>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(ingestionState?.status)">
          {{ statusText(ingestionState?.status) }} · {{ ingestionStateSummary.count || 0 }} rows
        </span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <MetricTile label="State Rows" :value="String(ingestionStateSummary.count || 0)" note="Within API limit" />
        <MetricTile label="Statuses" :value="String(ingestionStatusCounts.length)" note="Processed / failed / empty" />
        <MetricTile label="Sources" :value="String(ingestionSourceCounts.length)" note="Source prefixes" />
        <MetricTile label="Failure Classes" :value="String(ingestionClassificationCounts.length)" note="Classified parser errors" />
      </div>
      <div class="mt-4 grid gap-3 lg:grid-cols-3">
        <div class="rounded-2xl bg-paper/75 p-3">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Status Counts</p>
          <div class="mt-3 grid gap-2">
            <p v-for="item in ingestionStatusCounts" :key="`status-${item.key}`" class="flex items-center justify-between rounded-xl bg-white/70 px-3 py-2 text-sm">
              <span class="font-bold text-ink/70">{{ titleCase(item.key) }}</span>
              <span class="font-black text-ink">{{ item.count }}</span>
            </p>
            <p v-if="!ingestionStatusCounts.length" class="rounded-xl bg-white/70 px-3 py-2 text-sm text-ink/50">No file state rows in this response.</p>
          </div>
        </div>
        <div class="rounded-2xl bg-paper/75 p-3">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Failure Class Counts</p>
          <div class="mt-3 grid gap-2">
            <div v-for="item in ingestionClassificationCounts" :key="`classification-${item.key}`" class="rounded-xl bg-white/70 px-3 py-2 text-sm">
              <div class="flex items-center justify-between gap-3">
                <span class="font-bold text-ink/70">{{ ingestionClassDetail(item.key).label || titleCase(item.key) }}</span>
                <span class="font-black text-ink">{{ item.count }}</span>
              </div>
              <p v-if="ingestionClassDetail(item.key).meaning" class="mt-1 text-xs leading-5 text-ink/55">
                {{ ingestionClassDetail(item.key).meaning }}
              </p>
              <p v-if="ingestionClassDetail(item.key).operator_action" class="mt-1 text-xs font-semibold leading-5 text-ink/65">
                Action: {{ ingestionClassDetail(item.key).operator_action }}
              </p>
            </div>
            <p v-if="!ingestionClassificationCounts.length" class="rounded-xl bg-white/70 px-3 py-2 text-sm text-ink/50">No classified failed rows in this response.</p>
          </div>
        </div>
        <div class="rounded-2xl bg-paper/75 p-3">
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Top Sources</p>
          <div class="mt-3 grid gap-2">
            <p v-for="item in ingestionSourceCounts.slice(0, 8)" :key="`source-${item.key}`" class="flex items-center justify-between rounded-xl bg-white/70 px-3 py-2 text-sm">
              <span class="font-bold text-ink/70">{{ item.key }}</span>
              <span class="font-black text-ink">{{ item.count }}</span>
            </p>
            <p v-if="!ingestionSourceCounts.length" class="rounded-xl bg-white/70 px-3 py-2 text-sm text-ink/50">No source counts in this response.</p>
          </div>
        </div>
      </div>
      <details v-if="ingestionStateSamples.length" class="mt-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Show sample ingestion state rows</summary>
        <div class="mt-3 grid gap-2 lg:grid-cols-2">
          <article v-for="row in ingestionStateSamples" :key="`${row.source_prefix}-${row.object_key}`" class="rounded-xl bg-paper/80 p-3 text-sm">
            <div class="flex flex-wrap items-start justify-between gap-2">
              <div>
                <p class="font-black text-ink">{{ row.object_key || '-' }}</p>
                <p class="mt-1 text-xs font-semibold text-ink/45">{{ row.source_prefix || '-' }} · {{ row.processed_at || '-' }}</p>
              </div>
              <span class="rounded-full px-2 py-1 text-[0.65rem] font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <p v-if="row.error_message" class="mt-3 rounded-xl bg-rust/10 p-2 text-xs font-semibold text-rust">{{ row.error_message }}</p>
            <p v-if="ingestionRowClassDetail(row).meaning" class="mt-3 rounded-xl bg-white/70 p-2 text-xs leading-5 text-ink/65">
              <b>{{ ingestionRowClassDetail(row).label || titleCase(row.classification) }}:</b>
              {{ ingestionRowClassDetail(row).meaning }}
              <span v-if="ingestionRowClassDetail(row).operator_action" class="mt-1 block font-semibold">
                {{ ingestionRowClassDetail(row).operator_action }}
              </span>
            </p>
          </article>
        </div>
      </details>
      <p class="mt-4 rounded-xl bg-sun/10 px-3 py-2 text-xs font-semibold text-ink/65">
        Read-only boundary: {{ ingestionBoundary.note || 'This card does not clear failed rows or retry ingestion.' }}
        Manual cleanup command: <code class="font-black">{{ ingestionBoundary.clear_command || 'python scripts/ingestion_state_runner.py clear --source <source> --key <object_key>' }}</code>
      </p>
    </div>
    <div class="mt-5 rounded-2xl border border-black/10 bg-white/75 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Persisted Fallback Telemetry</p>
          <p class="mt-2 text-sm leading-6 text-ink/65">
            {{ fallbackTelemetry.message || 'No fallback telemetry has been recorded yet.' }}
          </p>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(fallbackTelemetry.status)">
          {{ statusText(fallbackTelemetry.status) }}
        </span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <MetricTile label="Window" :value="`${fallbackTelemetry.window_hours || 24}h`" note="Telemetry lookback" />
        <MetricTile label="Events" :value="String(fallbackTelemetry.active_count || 0)" note="Recent fallbacks" />
        <MetricTile label="Errors" :value="String(fallbackTelemetry.error_count || 0)" note="High-impact" />
        <MetricTile label="Types" :value="String(Object.keys(asDict(fallbackTelemetry.counts_by_type)).length)" note="Fallback classes" />
      </div>
      <details v-if="fallbackRows.length" class="mt-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Show latest fallback events</summary>
        <div class="mt-3 grid gap-2">
          <article v-for="row in fallbackRows.slice(0, 6)" :key="String(row.event_id)" class="rounded-xl bg-paper/80 p-3 text-sm">
            <div class="flex flex-wrap items-start justify-between gap-2">
              <p class="font-black text-ink">{{ titleCase(row.fallback_type) }}</p>
              <span class="rounded-full px-2 py-1 text-[0.65rem] font-black" :class="statusClass(row.severity)">{{ statusText(row.severity) }}</span>
            </div>
            <p class="mt-1 text-ink/65">{{ row.reason || row.error_message || '-' }}</p>
            <p class="mt-2 text-xs font-semibold text-ink/45">{{ row.module || '-' }} · {{ row.symbol || row.unique_id || '-' }} · {{ row.observed_at || '-' }}</p>
          </article>
        </div>
      </details>
    </div>
    <div v-if="degradationLifecycleGroups.length" class="mt-5 grid gap-3 lg:grid-cols-3">
      <article v-for="group in degradationLifecycleGroups" :key="String(group.key)" class="rounded-2xl bg-white/75 p-4">
        <div class="flex items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">{{ group.label }}</p>
            <p class="mt-2 text-3xl font-black text-ink">{{ group.count ?? 0 }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(group.status)">
            {{ statusText(group.status) }}
          </span>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/60">{{ group.description || '-' }}</p>
        <p class="mt-3 rounded-2xl bg-paper/70 p-3 text-sm font-semibold text-ink/70">
          {{ group.next_action || '-' }}
        </p>
        <details v-if="Object.keys(asDict(group.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show lifecycle details</summary>
          <pre class="mt-3 max-h-48 overflow-auto rounded-2xl bg-ink p-3 text-xs text-paper">{{ JSON.stringify(group.details, null, 2) }}</pre>
        </details>
      </article>
    </div>
    <div v-if="Number(supersededPreview.event_processing_candidates || 0) || Number(supersededPreview.announcement_document_candidates || 0)" class="mt-5 rounded-2xl bg-white/75 p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/40">Superseded Cleanup Preview</p>
          <p class="mt-2 text-sm leading-6 text-ink/60">
            Candidate rows are recovered failures. Applying this marks only superseded metadata so stale operational errors stop polluting Manual Review and Health.
            It never changes portfolio rows, action recommendations, config, or broker orders.
          </p>
        </div>
        <span class="rounded-full bg-sun px-3 py-1 text-xs font-black text-ink">
          {{ Number(supersededPreview.event_processing_candidates || 0) + Number(supersededPreview.announcement_document_candidates || 0) }} ready
        </span>
      </div>
      <div class="mt-4 grid gap-3 md:grid-cols-2">
        <p class="rounded-xl bg-paper/70 px-3 py-2 text-xs font-semibold text-ink/70">
          Dry run: <code class="font-black">{{ supersededPreview.dry_run_command || 'python -m advisory.superseded_failures --limit 500' }}</code>
        </p>
        <p class="rounded-xl bg-paper/70 px-3 py-2 text-xs font-semibold text-ink/70">
          Manual fallback: <code class="font-black">{{ supersededPreview.apply_command || 'python -m advisory.superseded_failures --apply --limit 500' }}</code>
        </p>
      </div>
      <div class="mt-4 rounded-2xl border border-sun/30 bg-sun/10 p-4">
        <div class="grid gap-3 lg:grid-cols-[1fr_2fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Operator id
            <input v-model="supersededCleanupOperatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Apply reason
            <input v-model="supersededCleanupReason" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Example: reviewed recovered rows; safe to clear stale failure noise" />
          </label>
          <button
            class="self-end rounded-full bg-ink px-5 py-3 text-sm font-black text-paper disabled:opacity-50"
            type="button"
            :disabled="supersededCleanupApplying || !supersededCleanupReason.trim()"
            @click="applySupersededCleanup"
          >
            {{ supersededCleanupApplying ? 'Applying...' : 'Mark Superseded' }}
          </button>
        </div>
        <p class="mt-3 text-xs font-semibold text-ink/60">
          This writes durable cleanup metadata only. It is audited as <code>superseded_failure_cleanup_apply</code>.
        </p>
        <p v-if="supersededCleanupError" class="mt-3 rounded-xl bg-rust/10 px-3 py-2 text-sm font-bold text-rust">{{ supersededCleanupError }}</p>
        <div v-if="supersededCleanupResult" class="mt-3 rounded-xl bg-moss/10 px-3 py-2 text-sm text-ink/70">
          <p class="font-black text-moss">
            Applied {{ asDict(supersededCleanupResult.counts).total_updated ?? 0 }} cleanup marker(s).
          </p>
          <p class="mt-1 text-xs">
            Audit run: {{ asDict(supersededCleanupResult.audit_run).run_id || '-' }}
          </p>
        </div>
      </div>
      <div class="mt-4 grid gap-3 lg:grid-cols-2">
        <div>
          <p class="text-sm font-black text-ink">Event processing candidates · {{ supersededPreview.event_processing_candidates || 0 }}</p>
          <div class="mt-2 grid gap-2">
            <p v-for="row in supersededEventSamples" :key="`event-${row.unique_id}-${row.stage}-${row.started_at}`" class="rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/70">
              <b>{{ row.symbol || row.unique_id || '-' }}</b> · {{ row.stage || '-' }} · superseded by {{ row.superseded_by_status || '-' }}
            </p>
            <p v-if="!supersededEventSamples.length" class="rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/50">No event-processing samples in the preview.</p>
          </div>
        </div>
        <div>
          <p class="text-sm font-black text-ink">Announcement document candidates · {{ supersededPreview.announcement_document_candidates || 0 }}</p>
          <div class="mt-2 grid gap-2">
            <p v-for="row in supersededDocumentSamples" :key="`doc-${row.unique_id}-${row.updated_at || row.load_ts}`" class="rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/70">
              <b>{{ row.symbol || row.ticker || row.unique_id || '-' }}</b> · OCR {{ row.ocr_status || '-' }} · Parse {{ row.parse_status || '-' }}
            </p>
            <p v-if="!supersededDocumentSamples.length" class="rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/50">No announcement-document samples in the preview.</p>
          </div>
        </div>
      </div>
    </div>
    <div class="mt-5 flex flex-wrap gap-2">
      <button
        v-for="item in degradationFilters"
        :key="item.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="degradationFilter === item.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="degradationFilter = item.key"
      >
        {{ item.label }}
      </button>
    </div>
    <div class="mt-3 flex flex-wrap gap-2">
      <button
        v-for="item in degradationKindOptions"
        :key="item.key"
        class="rounded-full px-4 py-2 text-sm font-black transition"
        :class="degradationKindFilter === item.key ? 'bg-moss text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
        type="button"
        @click="degradationKindFilter = item.key"
      >
        {{ item.label }} · {{ item.count }}
      </button>
    </div>
    <div class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="row in filteredDegradations" :key="`${row.kind}-${row.source}-${row.symbol}-${row.observed_at}-${row.message}`" class="rounded-2xl bg-white/75 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div class="flex flex-wrap items-center gap-2">
              <p class="font-black text-ink">{{ row.title || titleCase(row.kind) }}</p>
              <SymbolLink v-if="row.symbol" :symbol="row.symbol" subtle />
            </div>
            <p class="mt-1 text-sm leading-6 text-ink/60">{{ row.message || '-' }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status || row.severity)">
            {{ statusText(row.status || row.severity) }}
          </span>
        </div>
        <div class="mt-3 grid gap-2 text-xs md:grid-cols-2">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Kind:</b> {{ titleCase(row.kind) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Observed:</b> {{ row.observed_at || '-' }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Source:</b> {{ row.source || '-' }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Recovered:</b> {{ row.recovered ? 'yes' : 'no' }}</p>
        </div>
        <p v-if="row.suggested_fix" class="mt-3 rounded-2xl bg-sun/15 p-3 text-sm font-semibold text-ink/70">
          Fix hint: {{ row.suggested_fix }}
        </p>
        <div class="mt-3 flex flex-wrap gap-2">
          <NuxtLink v-if="row.symbol" class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" :to="`/symbols/${encodeURIComponent(String(row.symbol).toUpperCase())}`">
            Open symbol
          </NuxtLink>
          <NuxtLink v-if="row.unique_id" class="rounded-full bg-white px-4 py-2 text-sm font-bold text-ink" :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id))}`">
            Open event trace
          </NuxtLink>
        </div>
        <details v-if="Object.keys(asDict(row.details)).length" class="mt-3">
          <summary class="cursor-pointer text-sm font-black text-moss">Show details</summary>
          <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-ink p-3 text-xs text-paper">{{ JSON.stringify(row.details, null, 2) }}</pre>
        </details>
      </article>
      <p v-if="!filteredDegradations.length" class="rounded-2xl bg-white/75 p-4 text-sm text-ink/60">No degradation rows for this filter.</p>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
    <div class="glass-panel rounded-3xl p-5">
      <div class="flex items-start justify-between gap-3">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Data Freshness</p>
          <h2 class="mt-2 text-2xl font-black">Key table timestamps</h2>
        </div>
        <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(details?.status)">{{ statusText(details?.status) }}</span>
      </div>
      <div class="mt-5 space-y-3">
        <article v-for="row in tableFreshness" :key="String(row.name || row.table)" class="rounded-2xl bg-white/70 p-4">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="font-black text-ink">{{ row.name || row.table }}</p>
              <p class="text-xs text-ink/50">{{ row.table }} · {{ row.latest_at || 'no timestamp' }}</p>
            </div>
            <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
          </div>
          <p class="mt-2 text-sm text-ink/60">{{ row.message }}</p>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Rows:</b> {{ row.row_count ?? '-' }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Age hours:</b> {{ row.age_hours ?? '-' }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Limit:</b> {{ row.max_age_hours ?? '-' }}</p>
          </div>
        </article>
      </div>
    </div>

    <div class="space-y-6">
      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Runtime Endpoints</p>
        <h2 class="mt-2 text-2xl font-black">API, snapshot, and broker token</h2>
        <div class="mt-4 space-y-3">
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Operator API</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ operatorApi.url || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(operatorApi.status)">{{ statusText(operatorApi.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Latency:</b> {{ operatorApi.latency_ms ?? '-' }} ms</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Status code:</b> {{ operatorApi.status_code ?? '-' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ operatorApi.message }}</p>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Operator snapshot</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ operatorSnapshot.generated_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(operatorSnapshot.status)">{{ statusText(operatorSnapshot.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Age:</b> {{ secondsText(operatorSnapshot.age_seconds) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Bytes:</b> {{ operatorSnapshot.payload_bytes ?? '-' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ operatorSnapshot.message }}</p>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Slow-operation log</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ slowOperations.state_file || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(slowOperations.status)">{{ statusText(slowOperations.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Issues:</b> {{ slowOperations.issue_count ?? 0 }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Open shown:</b> {{ slowOperations.returned_count ?? 0 }}</p>
            </div>
            <div v-if="slowIssues.length" class="mt-4 grid gap-2">
              <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
                Operator id
                <input v-model="slowIssueOperatorId" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="operator" />
              </label>
              <p v-if="slowIssueError" class="rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ slowIssueError }}</p>
              <p v-if="slowIssueSuccess" class="rounded-2xl bg-moss/10 p-3 text-sm font-bold text-moss">{{ slowIssueSuccess }}</p>
            </div>
            <div v-if="slowIssues.length" class="mt-4 space-y-3">
              <article v-for="issue in slowIssues.slice(0, 6)" :key="String(issue.fingerprint)" class="rounded-2xl bg-paper/80 p-3">
                <div class="flex flex-wrap items-start justify-between gap-3">
                  <div>
                    <p class="break-all text-sm font-black text-ink">{{ slowIssueRoute(issue) }}</p>
                    <p class="mt-1 text-xs text-ink/50">{{ issue.kind }} · {{ issue.fingerprint }}</p>
                  </div>
                  <span class="rounded-full bg-rust px-3 py-1 text-xs font-black text-paper">{{ issue.status || 'open' }}</span>
                </div>
                <div class="mt-3 grid gap-2 text-xs md:grid-cols-3">
                  <p class="rounded-xl bg-white/75 px-3 py-2"><b>Last:</b> {{ issue.last_elapsed_ms ?? '-' }} ms</p>
                  <p class="rounded-xl bg-white/75 px-3 py-2"><b>Max:</b> {{ issue.max_elapsed_ms ?? '-' }} ms</p>
                  <p class="rounded-xl bg-white/75 px-3 py-2"><b>Count:</b> {{ issue.count ?? '-' }}</p>
                </div>
                <p class="mt-3 rounded-xl bg-sun/15 px-3 py-2 text-xs font-semibold text-ink/70">{{ slowIssueFix(issue) }}</p>
                <div class="mt-3 grid gap-2">
                  <textarea
                    v-model="slowIssueNotes[String(issue.fingerprint || '')]"
                    class="min-h-20 rounded-2xl border border-black/10 bg-white/80 px-3 py-2 text-sm text-ink outline-none focus:border-moss"
                    placeholder="Operator note. Required for fixed/ignored."
                  />
                  <div class="flex flex-wrap gap-2">
                    <button class="rounded-full bg-sun px-3 py-2 text-xs font-black text-ink disabled:opacity-50" :disabled="Boolean(slowIssueSaving)" type="button" @click="updateSlowIssue(issue, 'triaged')">
                      {{ slowIssueSaving === `${issue.fingerprint}:triaged` ? 'Saving...' : 'Triaged' }}
                    </button>
                    <button class="rounded-full bg-moss px-3 py-2 text-xs font-black text-paper disabled:opacity-50" :disabled="Boolean(slowIssueSaving)" type="button" @click="updateSlowIssue(issue, 'fixed')">
                      {{ slowIssueSaving === `${issue.fingerprint}:fixed` ? 'Saving...' : 'Fixed' }}
                    </button>
                    <button class="rounded-full bg-white px-3 py-2 text-xs font-black text-ink disabled:opacity-50" :disabled="Boolean(slowIssueSaving)" type="button" @click="updateSlowIssue(issue, 'ignored')">
                      {{ slowIssueSaving === `${issue.fingerprint}:ignored` ? 'Saving...' : 'Ignored' }}
                    </button>
                    <button class="rounded-full bg-ink px-3 py-2 text-xs font-black text-paper disabled:opacity-50" :disabled="Boolean(slowIssueSaving)" type="button" @click="updateSlowIssue(issue, 'open')">
                      {{ slowIssueSaving === `${issue.fingerprint}:open` ? 'Saving...' : 'Reopen' }}
                    </button>
                  </div>
                </div>
                <details class="mt-3">
                  <summary class="cursor-pointer text-xs font-black text-moss">Show slow issue details</summary>
                  <pre class="mt-3 max-h-56 overflow-auto rounded-2xl bg-ink p-3 text-xs leading-5 text-paper">{{ JSON.stringify(issue, null, 2) }}</pre>
                </details>
              </article>
            </div>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">API latency probe</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ apiLatencyProbe.path || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(apiLatencyProbe.status)">{{ statusText(apiLatencyProbe.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Age:</b> {{ secondsText(apiLatencyProbe.age_seconds) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Slow:</b> {{ apiLatencyProbe.slow_count ?? 0 }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Errors:</b> {{ apiLatencyProbe.error_count ?? 0 }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ apiLatencyProbe.operator_action || apiLatencyProbe.message }}</p>
            <div class="mt-3 grid gap-2">
              <code v-if="apiLatencyProbe.command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">{{ apiLatencyProbe.command }}</code>
              <code v-if="apiLatencyProbe.performance_report_command" class="block overflow-auto rounded-xl bg-ink px-3 py-2 text-xs text-paper">{{ apiLatencyProbe.performance_report_command }}</code>
            </div>
          </article>
          <article class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="font-black text-ink">Dhan token cache</p>
                <p class="mt-1 break-all text-xs text-ink/50">{{ dhanCache.cache_path || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(dhanCache.status)">{{ statusText(dhanCache.status) }}</span>
            </div>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-2">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Expires:</b> {{ dhanCache.expires_at || '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Time left:</b> {{ secondsText(dhanCache.seconds_to_expiry) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Cache age:</b> {{ secondsText(dhanCache.cache_age_seconds) }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Env token:</b> {{ dhanCache.has_direct_env_token ? 'yes' : 'no' }}</p>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ dhanCache.message }}</p>
          </article>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Cron Logs</p>
        <h2 class="mt-2 text-2xl font-black">Latest run status</h2>
        <div class="mt-4 space-y-3">
          <article v-for="row in filteredCronLogs" :key="String(row.log_file || row.message)" class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="break-all text-sm font-black text-ink">{{ row.log_file || 'cron' }}</p>
                <p class="mt-1 text-xs text-ink/50">{{ row.latest_run_status || 'unknown' }} · modified {{ row.modified_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ row.message }}</p>
            <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Current errors:</b> {{ row.current_error_count ?? row.recent_error_count ?? '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Historical:</b> {{ row.historical_error_count ?? '-' }}</p>
              <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Analyzed lines:</b> {{ row.analyzed_lines ?? '-' }}</p>
            </div>
            <p v-if="row.latest_success_line" class="mt-3 rounded-xl bg-moss/10 px-3 py-2 text-xs text-ink/70">
              <b>Latest success:</b> {{ row.latest_success_line }}
            </p>
            <p v-if="row.latest_error_line" class="mt-2 rounded-xl bg-rust/10 px-3 py-2 text-xs text-ink/70">
              <b>Latest error:</b> {{ row.latest_error_line }}
            </p>
            <p v-if="asDict(row.latest_script_marker).status" class="mt-2 rounded-xl bg-paper/70 px-3 py-2 text-xs text-ink/70">
              <b>Script marker:</b> {{ JSON.stringify(row.latest_script_marker) }}
            </p>
            <details v-if="asStringList(row.recent_errors).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-rust">Show current errors</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row.recent_errors, null, 2) }}</pre>
            </details>
            <details v-if="asStringList(row.historical_errors).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-moss">Show historical errors</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-paper/80 p-4 text-xs leading-5 text-ink">{{ JSON.stringify(row.historical_errors, null, 2) }}</pre>
            </details>
            <details v-if="asList(row.recovery_outputs).length" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-moss">Show recovery evidence</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-paper/80 p-4 text-xs leading-5 text-ink">{{ JSON.stringify(row.recovery_outputs, null, 2) }}</pre>
            </details>
          </article>
          <p v-if="!filteredCronLogs.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">No cron logs for this filter.</p>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Optional Dependencies</p>
        <h2 class="mt-2 text-2xl font-black">OCR, Codex, Node, TimesFM</h2>
        <div class="mt-4 flex flex-wrap gap-2">
          <span v-for="row in optionalDeps" :key="String(row.module || row.message)" class="rounded-full px-3 py-2 text-xs font-black" :class="statusClass(row.status)">
            {{ row.module || row.message }}: {{ statusText(row.status) }}
          </span>
        </div>
      </div>

      <div class="glass-panel rounded-3xl p-5">
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Sync Failures</p>
        <h2 class="mt-2 text-2xl font-black">Watcher/router state</h2>
        <div class="mt-4 space-y-3">
          <article v-for="row in syncStateFailures" :key="String(row.source_name || row.message)" class="rounded-2xl bg-white/70 p-4">
            <div class="flex items-start justify-between gap-3">
              <div>
                <p class="break-all text-sm font-black text-ink">{{ row.source_name || 'sync-state' }}</p>
                <p class="mt-1 text-xs text-ink/50">{{ row.updated_at || '-' }}</p>
              </div>
              <span class="rounded-full px-3 py-1 text-xs font-black" :class="statusClass(row.status)">{{ statusText(row.status) }}</span>
            </div>
            <p class="mt-2 text-sm text-ink/60">{{ row.error || row.message }}</p>
            <details v-if="row.state_json" class="mt-3">
              <summary class="cursor-pointer text-sm font-black text-rust">Show state payload</summary>
              <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ row.state_json }}</pre>
            </details>
          </article>
        </div>
      </div>
    </div>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-3">
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Runtime Processes</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(summary?.runtime_processes || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Sync State</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(summary?.sync_state || [], null, 2) }}</pre>
    </div>
    <div class="glass-panel rounded-3xl p-5">
      <h2 class="font-black">Raw Health</h2>
      <pre class="mt-4 max-h-96 overflow-auto text-xs">{{ JSON.stringify(details || {}, null, 2) }}</pre>
    </div>
  </section>
</template>
