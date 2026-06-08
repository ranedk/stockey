<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const [{ data: summary, error: summaryError }, { data: details, refresh, error: detailsError }] = await Promise.all([
  useAsyncData('summary-health', () => api.getSummary()),
  useAsyncData('operator-health-details', () => api.getHealthDetails())
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
const slowOperations = computed(() => asDict(sections.value.slow_operations))
const slowIssues = computed(() => asList(slowOperations.value.issues))
const syncStateFailures = computed(() => asList(sections.value.sync_state_failures))
const degradationFeed = computed(() => asDict(sections.value.degradation_feed))
const degradationLifecycle = computed(() => asDict(degradationFeed.value.lifecycle))
const degradationLifecycleGroups = computed(() => asList(degradationLifecycle.value.groups))
const supersededPreview = computed(() => asDict(degradationLifecycle.value.superseded_preview))
const supersededEventSamples = computed(() => asList(supersededPreview.value.event_processing_sample))
const supersededDocumentSamples = computed(() => asList(supersededPreview.value.announcement_document_sample))
const degradations = computed(() => asList(degradationFeed.value.rows))
const fixHints = computed(() => asList(details.value?.fix_hints))
const currentBlockers = computed(() => asDict(details.value?.current_blockers))
const currentBlockerRows = computed(() => asList(currentBlockers.value.rows))
const healthFilter = ref('all')
const degradationFilter = ref('active')
const degradationKindFilter = ref('all')
const slowIssueOperatorId = ref('operator')
const slowIssueNotes = ref<Record<string, string>>({})
const slowIssueSaving = ref('')
const slowIssueError = ref('')
const slowIssueSuccess = ref('')
const loadErrors = computed(() => [
  { title: 'Summary payload failed', error: summaryError.value },
  { title: 'Operator health details failed', error: detailsError.value }
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
  const rows = [...cronLogs.value, ...fixHints.value, ...syncStateFailures.value, ...degradations.value]
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

function statusClass(value: unknown) {
  const status = String(value || '').toLowerCase()
  if (status === 'ok') return 'bg-moss text-paper'
  if (status === 'error') return 'bg-rust text-paper'
  return 'bg-sun text-ink'
}

function statusText(value: unknown) {
  return String(value || 'unknown').toUpperCase()
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
    await refresh()
  } catch (err) {
    slowIssueError.value = err instanceof Error ? err.message : String(err)
  } finally {
    slowIssueSaving.value = ''
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
      <button class="rounded-full bg-paper px-5 py-3 text-sm font-black text-ink" type="button" @click="refresh()">
        Refresh
      </button>
    </div>
  </section>

  <SnapshotWarning class="mt-4" :snapshot="summarySnapshot" :warning="summarySnapshotWarning" :generated-at="summary?.generated_at" />

  <section v-if="loadErrors.length" class="mt-6 grid gap-3">
    <ApiErrorBanner v-for="row in loadErrors" :key="row.title" :title="row.title" :error="row.error" />
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
            Candidate rows are read-only here. Marking them superseded requires an explicit shell apply command after reviewing the dry-run output.
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
          Apply: <code class="font-black">{{ supersededPreview.apply_command || 'python -m advisory.superseded_failures --apply --limit 500' }}</code>
        </p>
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
