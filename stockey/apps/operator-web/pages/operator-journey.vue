<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const symbolFilter = ref('')
const itemIdFilter = ref('')
const uniqueIdFilter = ref('')
const limitFilter = ref(25)

const queryParams = computed(() => ({
  symbol: symbolFilter.value.trim().toUpperCase() || undefined,
  item_id: itemIdFilter.value.trim() || undefined,
  unique_id: uniqueIdFilter.value.trim() || undefined,
  limit: limitFilter.value
}))

const { data, refresh, pending, error: loadError } = await useAsyncData(
  'operator-journey',
  () => api.getOperatorJourney(queryParams.value),
  { watch: [queryParams] }
)

const summary = computed(() => asDict(data.value?.summary))
const boundary = computed(() => asDict(summary.value.operator_boundary))
const stageCounts = computed(() => asDict(summary.value.stage_counts))
const stages = computed(() => asDict(data.value?.stages))
const timeline = computed(() => asList(data.value?.timeline))
const skippedSources = computed(() => asList(data.value?.skipped_sources))
const stageCards = computed(() => [
  { key: 'manual_decisions', label: 'Manual Decisions', description: 'Operator choices and rationale recorded from Manual Review.' },
  { key: 'wait_signals', label: 'Wait Signals', description: 'Conditions created from manual review or playbook scans.' },
  { key: 'wait_signal_matches', label: 'Wait Matches', description: 'Fresh evidence that matched a wait condition.' },
  { key: 'signal_refresh', label: 'Signal Refresh', description: 'Fast refresh output from watcher or wait-signal activity.' },
  { key: 'actions', label: 'Actions', description: 'Consolidated action recommendation rows.' },
  { key: 'portfolio', label: 'Portfolio', description: 'Portfolio planning or review implication.' },
  { key: 'execution', label: 'Execution', description: 'Dry-run execution preview or blocker state.' }
])

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}

function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
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

function stageRows(stage: string) {
  return asList(stages.value[stage])
}

function stageTone(stage: unknown) {
  const text = String(stage || '').toLowerCase()
  if (text.includes('execution')) return 'danger'
  if (text.includes('portfolio') || text.includes('actions')) return 'success'
  if (text.includes('match') || text.includes('refresh')) return 'info'
  return 'dark'
}

function rowReason(row: Dict) {
  return row.rationale || row.wait_question || row.match_reason || row.action_reason || row.portfolio_reason || row.execution_reason || row.reason || row.operator_summary || '-'
}

function rowAction(row: Dict) {
  return row.decision || row.expected_action || row.signal_action || row.action_code || row.portfolio_status || row.execution_status || row.status || '-'
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <div class="flex flex-wrap items-end justify-between gap-5">
      <div>
        <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Operator Journey</p>
        <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">
          Trace a decision from issue to downstream implication.
        </h1>
        <p class="mt-4 max-w-3xl text-sm leading-6 text-paper/65">
          Read-only stitched view across Manual Review, wait signals, matched evidence, signal refresh, action recommendations, portfolio planning, and execution previews.
        </p>
      </div>
      <button class="rounded-full bg-sun px-5 py-3 text-sm font-black text-ink shadow-soft disabled:opacity-50" type="button" :disabled="pending" @click="() => refresh()">
        {{ pending ? 'Refreshing...' : 'Refresh Journey' }}
      </button>
    </div>

    <div class="mt-6 grid gap-4 md:grid-cols-5">
      <MetricTile label="Timeline" :value="display(summary.timeline_count)" note="stitched rows" />
      <MetricTile label="Manual" :value="display(stageCounts.manual_decisions)" note="review decisions" />
      <MetricTile label="Wait Matches" :value="display(stageCounts.wait_signal_matches)" note="fresh evidence" />
      <MetricTile label="Actions" :value="display(stageCounts.actions)" note="recommendations" />
      <MetricTile label="Skipped" :value="display(summary.skipped_source_count)" note="missing/failed sources" />
    </div>
  </section>

  <ApiErrorBanner v-if="loadError" class="mt-6" title="Could not load operator journey" :error="loadError" />

  <section class="mt-6 grid gap-4 lg:grid-cols-[1fr_0.8fr]">
    <div class="glass-panel rounded-3xl p-6">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Filters</p>
      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <input v-model="symbolFilter" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold uppercase text-ink outline-none focus:border-moss" placeholder="Symbol">
        <input v-model="itemIdFilter" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold text-ink outline-none focus:border-moss" placeholder="Manual item id">
        <input v-model="uniqueIdFilter" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold text-ink outline-none focus:border-moss" placeholder="Unique id">
        <select v-model="limitFilter" class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold text-ink outline-none focus:border-moss">
          <option :value="10">10 rows</option>
          <option :value="25">25 rows</option>
          <option :value="50">50 rows</option>
          <option :value="100">100 rows</option>
        </select>
      </div>
      <p class="mt-4 text-sm font-semibold leading-6 text-ink/60">
        Use a symbol for a broad journey, or an item/unique id to follow one Manual Review or action path.
      </p>
    </div>

    <div class="rounded-3xl border border-moss/20 bg-moss/10 p-6">
      <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Safety Boundary</p>
      <p class="mt-2 text-2xl font-black text-ink">Read-only operator context</p>
      <div class="mt-4 flex flex-wrap gap-2">
        <MetaChip tone="green" label="read only">{{ display(boundary.read_only) }}</MetaChip>
        <MetaChip tone="blue" label="portfolio">{{ boundary.mutates_portfolio ? 'mutates' : 'unchanged' }}</MetaChip>
        <MetaChip tone="blue" label="actions">{{ boundary.mutates_action_recommendation ? 'mutates' : 'unchanged' }}</MetaChip>
        <MetaChip tone="blue" label="broker">{{ boundary.submits_order ? 'can submit' : 'no submit' }}</MetaChip>
      </div>
    </div>
  </section>

  <section v-if="skippedSources.length" class="mt-6 rounded-3xl border border-sun/35 bg-sun/10 p-5">
    <p class="text-sm font-black text-ink">Skipped sources</p>
    <div class="mt-3 grid gap-2">
      <div v-for="source in skippedSources" :key="`${source.stage}-${source.source}`" class="rounded-2xl bg-white/75 p-3 text-sm">
        <b>{{ titleCase(source.stage) }}</b> · {{ source.source }} · {{ source.reason }}
      </div>
    </div>
  </section>

  <section class="mt-8 grid gap-6 xl:grid-cols-[0.85fr_1.15fr]">
    <div class="space-y-4">
      <article v-for="card in stageCards" :key="card.key" class="rounded-3xl border border-black/10 bg-white/75 p-5 shadow-soft">
        <div class="flex items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">{{ card.label }}</p>
            <p class="mt-2 text-sm font-semibold leading-6 text-ink/60">{{ card.description }}</p>
          </div>
          <StatusPill :tone="stageTone(card.key)">{{ display(stageCounts[card.key]) }}</StatusPill>
        </div>
        <div class="mt-4 space-y-2">
          <div v-for="row in stageRows(card.key).slice(0, 3)" :key="`${card.key}-${row.signal_id || row.refresh_id || row.unique_id || row.item_id || row.correlation_id || row.source_key}`" class="rounded-2xl bg-paper/70 p-3 text-sm">
            <div class="flex flex-wrap items-center justify-between gap-2">
              <SymbolLink v-if="row.symbol" :symbol="row.symbol" />
              <b v-else>{{ row.item_id || row.signal_id || row.unique_id || 'row' }}</b>
              <MetaChip tone="plain" label="action">{{ display(rowAction(row)) }}</MetaChip>
            </div>
            <p class="mt-2 text-xs leading-5 text-ink/60">{{ display(rowReason(row)) }}</p>
          </div>
          <p v-if="!stageRows(card.key).length" class="rounded-2xl bg-paper/70 p-3 text-sm font-semibold text-ink/45">No rows.</p>
        </div>
      </article>
    </div>

    <section class="rounded-3xl border border-black/10 bg-white/80 p-6 shadow-soft">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Newest First</p>
          <h2 class="mt-2 text-3xl font-black text-ink">Journey Timeline</h2>
        </div>
        <StatusPill tone="info">{{ timeline.length }}</StatusPill>
      </div>
      <div class="mt-6 space-y-4">
        <article v-for="event in timeline" :key="`${event.stage}-${event.source_key}-${event.timestamp}`" class="relative rounded-3xl border border-black/10 bg-paper/70 p-5">
          <div class="flex flex-wrap items-start justify-between gap-3">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">{{ titleCase(event.stage) }}</p>
              <h3 class="mt-1 text-xl font-black text-ink">{{ event.title || titleCase(event.stage) }}</h3>
              <p class="mt-1 text-xs text-ink/45">{{ formatTime(event.timestamp) }}</p>
            </div>
            <StatusPill :tone="stageTone(event.stage)">{{ display(event.action) }}</StatusPill>
          </div>
          <p class="mt-4 text-sm font-semibold leading-6 text-ink/70">{{ display(event.reason) }}</p>
          <div class="mt-4 flex flex-wrap gap-2">
            <SymbolLink v-if="event.symbol" :symbol="event.symbol" />
            <MetaChip v-if="event.setup_id" tone="plain" label="setup">{{ event.setup_id }}</MetaChip>
            <MetaChip v-if="event.unique_id" tone="plain" label="unique">{{ event.unique_id }}</MetaChip>
            <MetaChip tone="blue" label="source">{{ event.source_table }}</MetaChip>
          </div>
        </article>
        <p v-if="!timeline.length && !pending" class="rounded-3xl bg-paper/70 p-5 text-sm font-bold text-ink/55">
          No journey rows found for the current filters.
        </p>
        <p v-if="pending" class="rounded-3xl bg-paper/70 p-5 text-sm font-bold text-ink/55">Loading journey...</p>
      </div>
    </section>
  </section>
</template>
