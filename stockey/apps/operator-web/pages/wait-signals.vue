<script setup lang="ts">
import type { Dict } from '~/types/api'

const api = useOperatorApi()
const statusFilter = ref('all')
const symbolFilter = ref('')
const runningMatch = ref(false)
const runError = ref('')
const runResult = ref<Dict | null>(null)

const queryParams = computed(() => ({
  limit: 250,
  status: statusFilter.value === 'all' ? undefined : statusFilter.value,
  symbol: symbolFilter.value.trim() || undefined
}))

const { data, refresh, pending, error: loadError } = await useAsyncData(
  'wait-signals',
  () => api.getWaitSignals(queryParams.value),
  { watch: [queryParams] }
)

const summary = computed(() => asDict(data.value?.summary))
const sections = computed(() => asDict(data.value?.sections))
const activeSignals = computed(() => asList(sections.value.active))
const matchedSignals = computed(() => asList(sections.value.matched))
const expiredSignals = computed(() => asList(sections.value.expired))
const closedSignals = computed(() => asList(sections.value.closed))
const matches = computed(() => asList(data.value?.matches))
const sourceWarnings = computed(() => asList(data.value?.source_warnings || summary.value.source_warnings))

const statusOptions = computed(() => [
  { key: 'all', label: 'All', count: Number(summary.value.total_signals || 0) },
  { key: 'active', label: 'Active', count: Number(summary.value.active || 0) },
  { key: 'matched', label: 'Matched', count: Number(summary.value.matched || 0) }
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

function signalTone(row: Dict) {
  const bucket = String(row.state_bucket || row.status || '').toLowerCase()
  if (bucket === 'matched') return 'success'
  if (bucket === 'expired') return 'warning'
  if (bucket === 'closed') return 'neutral'
  return 'action'
}

function sourceTone(row: Dict) {
  if (row.is_manual_review_signal) return 'blue'
  if (row.is_playbook_signal) return 'green'
  return 'plain'
}

async function runMatcher() {
  runningMatch.value = true
  runError.value = ''
  runResult.value = null
  try {
    const payload = await api.runWaitSignalMatch({
      limit: queryParams.value.limit,
      symbol: queryParams.value.symbol
    })
    runResult.value = asDict(payload.match_result)
    await refresh()
  } catch (err) {
    runError.value = err instanceof Error ? err.message : String(err)
  } finally {
    runningMatch.value = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <div class="flex flex-wrap items-end justify-between gap-5">
      <div>
        <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Wait Signals</p>
        <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">
          Conditions the system is watching after manual review or playbook scans.
        </h1>
        <p class="mt-4 max-w-3xl text-sm leading-6 text-paper/65">
          These are not trades by themselves. A match means new price, news, or announcement evidence hit a recorded condition and should be reviewed through signal refresh or advisory.
        </p>
      </div>
      <button
        class="rounded-full bg-sun px-5 py-3 text-sm font-black text-ink shadow-soft disabled:opacity-50"
        type="button"
        :disabled="runningMatch"
        @click="runMatcher"
      >
        {{ runningMatch ? 'Checking...' : 'Check Active Waits Now' }}
      </button>
    </div>

    <div class="mt-6 grid gap-4 md:grid-cols-5">
      <MetricTile label="Active" :value="display(summary.active)" note="Still being watched" />
      <MetricTile label="Matched" :value="display(summary.matched)" note="Evidence found" />
      <MetricTile label="Expired" :value="display(summary.expired)" note="Window passed" />
      <MetricTile label="Manual" :value="display(summary.manual_review)" note="Created by operator review" />
      <MetricTile label="Playbook" :value="display(summary.playbook)" note="Created by hypothesis scans" />
    </div>
  </section>

  <ApiErrorBanner v-if="loadError" class="mt-6" title="Could not load wait signals" :error="loadError" />
  <SourceWarnings :warnings="sourceWarnings" />
  <p v-if="runError" class="mt-6 rounded-3xl bg-rust/10 p-4 text-sm font-bold text-rust">{{ runError }}</p>
  <div v-if="runResult" class="mt-6 rounded-3xl border border-moss/20 bg-moss/10 p-4 text-sm text-moss">
    Matcher completed: {{ display(runResult.matched_rows) }} matched from {{ display(runResult.active_signal_rows) }} active signals.
  </div>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-end justify-between gap-4">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Filters</p>
        <h2 class="mt-2 text-2xl font-black">Signal Queue</h2>
      </div>
      <div class="flex flex-wrap gap-3">
        <input
          v-model="symbolFilter"
          class="rounded-2xl border border-black/10 bg-white px-4 py-3 text-sm font-bold uppercase text-ink outline-none focus:border-moss"
          placeholder="Symbol"
        >
        <button
          v-for="option in statusOptions"
          :key="option.key"
          class="rounded-full px-4 py-2 text-sm font-black transition"
          :class="statusFilter === option.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60 hover:bg-white'"
          type="button"
          @click="statusFilter = option.key"
        >
          {{ option.label }} · {{ option.count }}
        </button>
      </div>
    </div>
    <p v-if="pending" class="mt-5 rounded-3xl bg-white/70 p-5 text-sm font-bold text-ink/60">Loading wait signals...</p>
  </section>

  <section class="mt-8 grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
    <div class="space-y-6">
      <SignalGroup title="Active Waits" subtitle="Open conditions that watchers and signal refresh can match." :rows="activeSignals" />
      <SignalGroup title="Expired Waits" subtitle="The wait window has passed. These should be reviewed or regenerated if still relevant." :rows="expiredSignals" muted />
      <SignalGroup title="Closed Waits" subtitle="Rows no longer active for matching." :rows="closedSignals" muted />
    </div>

    <div class="space-y-6">
      <SignalGroup title="Matched Waits" subtitle="Conditions that found fresh evidence." :rows="matchedSignals" matched />
      <section class="rounded-3xl border border-black/10 bg-white/75 p-5 shadow-soft">
        <div class="flex items-start justify-between gap-4">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Latest Evidence</p>
            <h2 class="mt-2 text-2xl font-black text-ink">Match Rows</h2>
          </div>
          <StatusPill tone="info">{{ matches.length }}</StatusPill>
        </div>
        <div class="mt-5 space-y-3">
          <article v-for="match in matches.slice(0, 12)" :key="`${match.signal_id}-${match.source_key}`" class="rounded-2xl bg-paper/70 p-4">
            <div class="flex flex-wrap items-start justify-between gap-3">
              <div>
                <SymbolLink v-if="match.symbol" :symbol="match.symbol" />
                <p v-else class="text-sm font-black text-ink">Market-wide</p>
                <p class="mt-1 text-xs text-ink/45">{{ formatTime(match.matched_at) }}</p>
              </div>
              <MetaChip tone="green" label="score">{{ display(match.match_score) }}</MetaChip>
            </div>
            <p class="mt-3 text-sm font-semibold leading-6 text-ink/70">{{ match.evidence_summary || match.match_reason || 'Matched evidence recorded.' }}</p>
            <div class="mt-3 flex flex-wrap gap-2">
              <MetaChip tone="blue" label="source">{{ titleCase(match.source_table) }}</MetaChip>
              <MetaChip tone="plain" label="type">{{ titleCase(match.signal_type) }}</MetaChip>
              <MetaChip v-if="match.observed_value !== null && match.observed_value !== undefined && match.observed_value !== ''" tone="green" label="value">{{ display(match.observed_value) }}</MetaChip>
            </div>
          </article>
          <p v-if="!matches.length" class="rounded-2xl bg-paper/70 p-5 text-sm font-semibold text-ink/55">
            No match evidence yet.
          </p>
        </div>
      </section>
    </div>
  </section>
</template>

<script lang="ts">
import type { Dict as ApiDict } from '~/types/api'

export default {
  components: {
    SignalGroup: defineComponent({
      props: {
        title: { type: String, required: true },
        subtitle: { type: String, required: true },
        rows: { type: Array as () => ApiDict[], required: true },
        muted: { type: Boolean, default: false },
        matched: { type: Boolean, default: false }
      },
      setup(props) {
        const open = ref(!props.muted)
        const display = (value: unknown) => {
          if (value === null || value === undefined || value === '') return '-'
          if (typeof value === 'number') return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(value)
          return String(value)
        }
        const formatTime = (value: unknown) => {
          const text = String(value || '')
          if (!text) return '-'
          const date = new Date(text)
          if (Number.isNaN(date.getTime())) return text
          return new Intl.DateTimeFormat('en-IN', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Kolkata' }).format(date)
        }
        const titleCase = (value: unknown) => String(value || 'unknown').replaceAll('_', ' ').replace(/\b\w/g, (char) => char.toUpperCase())
        const signalTone = (row: ApiDict) => {
          const bucket = String(row.state_bucket || row.status || '').toLowerCase()
          if (bucket === 'matched') return 'success'
          if (bucket === 'expired') return 'warning'
          if (bucket === 'closed') return 'neutral'
          return 'action'
        }
        const sourceTone = (row: ApiDict) => {
          if (row.is_manual_review_signal) return 'blue'
          if (row.is_playbook_signal) return 'green'
          return 'plain'
        }
        return { open, display, formatTime, titleCase, signalTone, sourceTone }
      },
      template: `
        <section class="rounded-3xl border border-black/10 bg-white/75 p-5 shadow-soft">
          <div class="flex items-start justify-between gap-4">
            <div>
              <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">{{ title }}</p>
              <h2 class="mt-2 text-2xl font-black text-ink">{{ rows.length }} rows</h2>
              <p class="mt-2 text-sm leading-6 text-ink/60">{{ subtitle }}</p>
            </div>
            <button class="rounded-full bg-ink px-3 py-1 text-xs font-black text-paper" type="button" @click="open = !open">
              {{ open ? 'Hide' : 'Show' }}
            </button>
          </div>
          <div v-if="open" class="mt-5 space-y-4">
            <article v-for="row in rows" :key="String(row.signal_id)" class="rounded-3xl bg-paper/70 p-5">
              <div class="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <SymbolLink v-if="row.symbol" :symbol="row.symbol" />
                  <p v-else class="text-sm font-black text-ink">Market-wide</p>
                  <p class="mt-1 text-xs font-semibold text-ink/45">{{ row.hypothesis_title || row.hypothesis_id || row.source_key || row.signal_id }}</p>
                </div>
                <div class="flex flex-wrap justify-end gap-2">
                  <StatusPill :tone="signalTone(row)">{{ titleCase(row.state_bucket || row.status || 'active') }}</StatusPill>
                  <MetaChip :tone="sourceTone(row)" label="source">{{ row.source_label || 'System' }}</MetaChip>
                  <MetaChip v-if="row.manual_review_item_id" tone="blue" label="manual review">{{ row.manual_review_item_id }}</MetaChip>
                </div>
              </div>
              <p class="mt-4 text-sm font-semibold leading-6 text-ink/75">{{ row.wait_question || row.operator_summary || 'No wait question recorded.' }}</p>
              <div class="mt-4 grid gap-3 text-sm lg:grid-cols-2">
                <p class="rounded-2xl bg-white/70 px-4 py-3 text-ink/70"><b>Condition:</b> {{ row.condition_summary || '-' }}</p>
                <p class="rounded-2xl bg-white/70 px-4 py-3 text-ink/70"><b>Expected action:</b> {{ row.expected_action || 'Manual review' }}</p>
                <p class="rounded-2xl bg-white/70 px-4 py-3 text-ink/70"><b>Created:</b> {{ formatTime(row.created_at) }}</p>
                <p class="rounded-2xl bg-white/70 px-4 py-3 text-ink/70"><b>Valid until:</b> {{ formatTime(row.valid_until) }}</p>
              </div>
              <p v-if="row.manual_review_item_id" class="mt-3 rounded-2xl border border-blue-500/20 bg-blue-500/10 px-4 py-3 text-sm font-semibold leading-6 text-ink/70">
                Created from Manual Review item <b>{{ row.manual_review_item_id }}</b>. A match should return to Manual Review for operator follow-up; it does not mutate portfolio/actions/execution.
              </p>
              <div v-if="row.latest_match" class="mt-4 rounded-2xl border border-moss/20 bg-moss/10 p-4">
                <p class="text-xs font-black uppercase tracking-[0.2em] text-moss">Latest Match</p>
                <p class="mt-2 text-sm font-semibold leading-6 text-ink/75">{{ row.latest_match.evidence_summary || row.latest_match.match_reason }}</p>
                <div class="mt-3 flex flex-wrap gap-2">
                  <MetaChip tone="green" label="matched">{{ formatTime(row.latest_match.matched_at) }}</MetaChip>
                  <MetaChip tone="plain" label="source">{{ titleCase(row.latest_match.source_table) }}</MetaChip>
                  <MetaChip v-if="row.latest_match.observed_value !== null && row.latest_match.observed_value !== undefined && row.latest_match.observed_value !== ''" tone="green" label="value">{{ display(row.latest_match.observed_value) }}</MetaChip>
                  <MetaChip v-if="row.latest_match.manual_review_item_id" tone="blue" label="review item">{{ row.latest_match.manual_review_item_id }}</MetaChip>
                </div>
              </div>
              <details class="mt-4 rounded-2xl border border-black/10 bg-white/55 px-4 py-3">
                <summary class="cursor-pointer text-sm font-black text-moss">Show raw signal</summary>
                <pre class="mt-3 max-h-72 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row, null, 2) }}</pre>
              </details>
            </article>
            <p v-if="!rows.length" class="rounded-2xl bg-paper/70 p-5 text-sm font-semibold text-ink/55">
              No rows in this bucket.
            </p>
          </div>
        </section>
      `
    })
  }
}
</script>
