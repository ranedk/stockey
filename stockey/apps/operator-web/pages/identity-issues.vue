<script setup lang="ts">
import type { Dict, IdentityIssueResolutionPayload } from '~/types/api'

const api = useOperatorApi()
const symbolFilter = ref('')

const queryParams = computed(() => ({
  limit: 250,
  symbol: symbolFilter.value.trim() || undefined
}))

const { data, pending, error: loadError, refresh } = await useAsyncData(
  'identity-issues',
  () => api.getIdentityIssues(queryParams.value),
  { watch: [queryParams] }
)

const actionPending = ref(false)
const actionError = ref<unknown>(null)
const previewResult = ref<IdentityIssueResolutionPayload | null>(null)
const applyResult = ref<IdentityIssueResolutionPayload | null>(null)

const summary = computed(() => asDict(data.value?.summary))
const issues = computed(() => asList(data.value?.issues))
const skipped = computed(() => asList(data.value?.skipped))
const sourceWarnings = computed(() => asList(data.value?.source_warnings || summary.value.source_warnings))
const previewCounts = computed(() => asDict(previewResult.value?.counts))
const applyCounts = computed(() => asDict(applyResult.value?.counts))
const previewBoundary = computed(() => asDict(previewResult.value?.operator_boundary))
const applyBoundary = computed(() => asDict(applyResult.value?.operator_boundary))
const previewRows = computed(() => asList(previewResult.value?.results))
const applyRows = computed(() => asList(applyResult.value?.results))
const wouldResolveKeys = computed(() => previewRows.value
  .filter((row) => String(row.status || '') === 'would_resolve')
  .map((row) => String(row.issue_key || '').trim())
  .filter(Boolean)
)

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN').format(value)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}

function formatTime(value: unknown) {
  const text = String(value || '')
  if (!text) return '-'
  const date = new Date(text)
  if (Number.isNaN(date.getTime())) return text
  return new Intl.DateTimeFormat('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata'
  }).format(date)
}

function titleCase(value: unknown) {
  return String(value || 'unknown')
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function listText(value: unknown) {
  if (!Array.isArray(value) || !value.length) return '-'
  return value.map((item) => typeof item === 'object' && item !== null ? JSON.stringify(item) : String(item)).join(', ')
}

async function previewResolution() {
  actionPending.value = true
  actionError.value = null
  applyResult.value = null
  try {
    previewResult.value = await api.previewIdentityIssueResolution({ limit: 250 })
  } catch (error) {
    actionError.value = error
  } finally {
    actionPending.value = false
  }
}

async function applyResolution() {
  actionPending.value = true
  actionError.value = null
  try {
    applyResult.value = await api.applyIdentityIssueResolution({ limit: 250, issue_keys: wouldResolveKeys.value })
    await refresh()
  } catch (error) {
    actionError.value = error
  } finally {
    actionPending.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <div class="flex flex-wrap items-end justify-between gap-5">
      <div>
        <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Identity Issues</p>
        <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">
          Open Dhan security mapping failures blocking clean advisory runs.
        </h1>
        <p class="mt-4 max-w-3xl text-sm leading-6 text-paper/65">
          This page is read-only. Fix mappings and rerun the failed source so the issue no longer blocks ingestion or advisory.
        </p>
      </div>
    </div>

    <div class="mt-6 grid gap-4 md:grid-cols-4">
      <MetricTile label="Open" :value="display(summary.total_open)" note="Current unresolved rows" />
      <MetricTile label="Read-only" :value="display(summary.read_only)" note="No row mutation here" />
      <MetricTile label="Broker" :value="summary.broker_execution_enabled ? 'enabled' : 'disabled'" note="Execution boundary" />
      <MetricTile label="Skipped" :value="display(skipped.length)" note="Unavailable sources" />
    </div>
  </section>

  <ApiErrorBanner v-if="loadError" class="mt-6" title="Could not load identity issues" :error="loadError" />
  <ApiErrorBanner v-if="actionError" class="mt-6" title="Could not run identity resolver" :error="actionError" />
  <SourceWarnings :warnings="sourceWarnings" />

  <section class="mt-8 rounded-[2rem] border border-moss/20 bg-moss/10 p-6 shadow-soft">
    <div class="flex flex-wrap items-start justify-between gap-5">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.28em] text-moss">Repair Flow</p>
        <h2 class="mt-2 text-2xl font-black text-ink">Recheck Dhan mappings and close fixed issues</h2>
        <p class="mt-2 max-w-3xl text-sm font-semibold leading-6 text-ink/65">
          Preview is read-only. Apply only closes identity issue rows that the latest preview says would resolve. It does not edit Dhan/company mappings and does not touch broker execution.
        </p>
      </div>
      <div class="flex flex-wrap gap-3">
        <button
          class="rounded-full bg-ink px-5 py-3 text-sm font-black text-paper shadow-soft disabled:cursor-not-allowed disabled:opacity-45"
          :disabled="actionPending"
          @click="previewResolution"
        >
          {{ actionPending ? 'Working...' : 'Recheck mappings' }}
        </button>
        <button
          class="rounded-full bg-moss px-5 py-3 text-sm font-black text-paper shadow-soft disabled:cursor-not-allowed disabled:opacity-45"
          :disabled="actionPending || !wouldResolveKeys.length"
          @click="applyResolution"
        >
          Close {{ display(wouldResolveKeys.length) }} resolved
        </button>
      </div>
    </div>

    <div class="mt-5 grid gap-4 md:grid-cols-4">
      <MetricTile label="Would close" :value="display(previewCounts.would_resolve || 0)" note="Preview only" />
      <MetricTile label="Still open" :value="display(previewCounts.still_open || 0)" note="Needs mapping fix" />
      <MetricTile label="Closed" :value="display(applyCounts.resolved || 0)" note="Latest apply" />
      <MetricTile label="Checked" :value="display(previewResult?.checked_rows || applyResult?.checked_rows || 0)" note="Resolver rows" />
    </div>

    <div v-if="previewResult || applyResult" class="mt-5 grid gap-4 lg:grid-cols-2">
      <div v-if="previewResult" class="rounded-3xl bg-white/75 p-5">
        <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Preview Result</p>
        <p class="mt-2 text-sm font-semibold text-ink/70">{{ display(previewResult.note) }}</p>
        <div class="mt-3 grid gap-2 text-xs font-bold text-ink/55 md:grid-cols-3">
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Mutates issue status: {{ display(previewBoundary.mutates_identity_issue_status) }}</p>
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Mutates mappings: {{ display(previewBoundary.mutates_identity_mapping) }}</p>
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Broker execution: {{ display(previewBoundary.mutates_broker_execution) }}</p>
        </div>
        <div class="mt-3 max-h-64 space-y-2 overflow-auto">
          <p v-for="row in previewRows" :key="`preview-${String(row.issue_key)}`" class="rounded-2xl bg-paper/80 p-3 text-xs font-bold text-ink/65">
            {{ display(row.symbol) }} / {{ display(row.requested_exchange) }}: {{ titleCase(row.status) }} {{ row.error ? `- ${display(row.error)}` : '' }}
          </p>
        </div>
      </div>
      <div v-if="applyResult" class="rounded-3xl bg-white/75 p-5">
        <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Apply Result</p>
        <p class="mt-2 text-sm font-semibold text-ink/70">{{ display(applyResult.note) }}</p>
        <div class="mt-3 grid gap-2 text-xs font-bold text-ink/55 md:grid-cols-3">
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Mutates issue status: {{ display(applyBoundary.mutates_identity_issue_status) }}</p>
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Mutates mappings: {{ display(applyBoundary.mutates_identity_mapping) }}</p>
          <p class="rounded-2xl bg-paper/80 px-3 py-2">Broker execution: {{ display(applyBoundary.mutates_broker_execution) }}</p>
        </div>
        <div class="mt-3 max-h-64 space-y-2 overflow-auto">
          <p v-for="row in applyRows" :key="`apply-${String(row.issue_key)}`" class="rounded-2xl bg-paper/80 p-3 text-xs font-bold text-ink/65">
            {{ display(row.symbol) }} / {{ display(row.requested_exchange) }}: {{ titleCase(row.status) }} {{ row.error ? `- ${display(row.error)}` : '' }}
          </p>
        </div>
      </div>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Filters</p>
        <h2 class="mt-2 text-2xl font-black">Skipped Symbols</h2>
      </div>
      <input
        v-model="symbolFilter"
        class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold uppercase text-ink outline-none focus:border-moss"
        placeholder="Symbol"
      >
    </div>
    <p v-if="pending" class="mt-5 rounded-3xl bg-white/70 p-5 text-sm font-bold text-ink/60">Loading identity issues...</p>
    <div v-if="skipped.length" class="mt-5 space-y-2">
      <p v-for="row in skipped" :key="String(row.source || row.error)" class="rounded-2xl bg-rust/10 p-4 text-sm font-bold text-rust">
        {{ display(row.source) }}: {{ display(row.error) }}
      </p>
    </div>
  </section>

  <section class="mt-8 space-y-4">
    <article v-for="issue in issues" :key="String(issue.issue_key)" class="rounded-3xl border border-black/10 bg-white/80 p-5 shadow-soft">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="flex flex-wrap items-center gap-2">
            <SymbolLink v-if="issue.symbol" :symbol="String(issue.symbol)" />
            <h2 v-else class="text-xl font-black text-ink">Unknown Symbol</h2>
            <MetaChip tone="blue" label="exchange">{{ display(issue.requested_exchange) }}</MetaChip>
            <MetaChip tone="plain" label="asset">{{ display(issue.asset_type) }}</MetaChip>
            <MetaChip tone="red" label="status">{{ display(issue.status) }}</MetaChip>
          </div>
          <p class="mt-2 text-xs font-semibold uppercase tracking-[0.18em] text-ink/45">{{ display(issue.issue_key) }}</p>
        </div>
        <StatusPill tone="warning">{{ titleCase(issue.issue_type) }}</StatusPill>
      </div>

      <p class="mt-4 text-sm font-semibold leading-6 text-ink/75">{{ display(issue.error_text) }}</p>

      <div class="mt-5 grid gap-3 lg:grid-cols-3">
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Repair Hint</p>
          <p class="mt-2 text-sm font-semibold leading-6 text-ink/75">{{ display(issue.repair_hint || issue.suggested_action) }}</p>
        </div>
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Attempts</p>
          <p class="mt-2 text-sm font-semibold leading-6 text-ink/75">Seen: {{ display(issue.attempt_count || 0) }} time(s)</p>
          <p class="mt-1 text-sm font-semibold leading-6 text-ink/75">Exchanges: {{ listText(issue.exchanges_tried) }}</p>
          <p class="mt-1 text-sm font-semibold leading-6 text-ink/75">Fallbacks: {{ listText(issue.fallback_tried) }}</p>
          <p v-if="issue.resolution_error_text" class="mt-1 text-sm font-semibold leading-6 text-rust">Last recheck: {{ display(issue.resolution_error_text) }}</p>
        </div>
        <div class="rounded-2xl bg-paper/80 p-4">
          <p class="text-xs font-black uppercase tracking-[0.22em] text-ink/45">Timing</p>
          <p class="mt-2 text-sm font-semibold leading-6 text-ink/75">First: {{ formatTime(issue.first_seen_at) }}</p>
          <p class="mt-1 text-sm font-semibold leading-6 text-ink/75">Latest: {{ formatTime(issue.last_seen_at || issue.load_ts) }}</p>
        </div>
      </div>

      <div class="mt-4 flex flex-wrap gap-2">
        <MetaChip tone="plain" label="source">{{ titleCase(issue.source) }}</MetaChip>
        <MetaChip v-if="issue.company_master_id" tone="green" label="company">{{ display(issue.company_master_id) }}</MetaChip>
        <MetaChip tone="blue" label="manual item">{{ display(issue.manual_review_item_id) }}</MetaChip>
      </div>
    </article>

    <p v-if="!pending && !issues.length" class="rounded-3xl border border-black/10 bg-white/80 p-8 text-center text-sm font-bold text-ink/55">
      No open identity issues match the current filter.
    </p>
  </section>
</template>
