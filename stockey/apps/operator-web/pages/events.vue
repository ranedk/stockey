<script setup lang="ts">
import type { Dict } from '~/types/api'
import type { TraceSummary } from '~/types/api'

const api = useOperatorApi()
const eventLimit = ref(50)
const eventOffset = ref(0)
const eventSymbol = ref('')
const eventStatus = ref('all')
const eventSearch = ref('')
const { data, refresh: refreshEvents, error: eventsError } = await useAsyncData(
  'events',
  () => api.getEvents(eventLimit.value, {
    offset: eventOffset.value,
    symbol: eventSymbol.value.trim().toUpperCase(),
    status: eventStatus.value,
    search: eventSearch.value.trim(),
    compact: true
  }),
  { watch: [eventLimit, eventOffset, eventStatus] }
)
const selectedActionType = ref('ALL')
const { data: policyData, refresh: refreshPolicy } = await useAsyncData('event-policy', () => api.getEventPolicy(100, selectedActionType.value), {
  watch: [selectedActionType]
})
const { data: policyEvalData } = await useAsyncData('event-policy-evaluation', () => api.getEventPolicyEvaluation(80))
const events = computed(() => data.value?.events || [])
const eventMeta = computed(() => data.value?.meta?.events as Dict || {})
const policyRows = computed(() => policyData.value?.rows || [])
const policyEvalRows = computed(() => policyEvalData.value?.summary || [])
const policySummary = computed(() => policyData.value?.summary || {})
const actionCounts = computed(() => policySummary.value.action_counts as Record<string, number> || {})
const policyClassCounts = computed(() => policySummary.value.policy_class_counts as Record<string, number> || {})
const traces = reactive<Record<string, TraceSummary>>({})
const loadingTrace = reactive<Record<string, boolean>>({})
const actionTypes = ['ALL', 'MANUAL_REVIEW', 'BUY_WATCH', 'REDUCE_EXPOSURE_REVIEW', 'NO_ACTION']

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function notes(row: Dict): Dict {
  return typeof row.operator_notes === 'object' && row.operator_notes ? row.operator_notes as Dict : {}
}

function checks(row: Dict): unknown[] {
  return asList(row.checks)
}

function tone(actionType: unknown) {
  const value = String(actionType || '').toUpperCase()
  if (value === 'REDUCE_EXPOSURE_REVIEW') return 'bg-rust text-paper'
  if (value === 'BUY_WATCH') return 'bg-moss text-paper'
  if (value === 'MANUAL_REVIEW') return 'bg-sun text-ink'
  return 'bg-white text-ink'
}

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 1000) / 10}%`
}

function numberText(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return String(value || '-')
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 3 }).format(num)
}

async function loadTrace(row: Record<string, unknown>) {
  const uniqueId = String(row.unique_id || '')
  if (!uniqueId || traces[uniqueId] || loadingTrace[uniqueId]) return
  loadingTrace[uniqueId] = true
  try {
    traces[uniqueId] = await api.getEventTraceSummary(uniqueId)
  } finally {
    loadingTrace[uniqueId] = false
  }
}

async function applyEventFilters() {
  eventOffset.value = 0
  await refreshEvents()
}

async function loadNextEvents() {
  const next = Number(eventMeta.value.next_offset)
  if (!Number.isFinite(next)) return
  eventOffset.value = next
}

function eventDetailPath(row: Record<string, unknown>) {
  const uniqueId = String(row.unique_id || '')
  return uniqueId ? `/api/events/${encodeURIComponent(uniqueId)}/detail` : ''
}
</script>

<template>
  <section>
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Event Inbox</p>
    <h1 class="mt-3 text-4xl font-black">Event policy decisions</h1>
    <p class="mt-3 max-w-3xl text-ink/60">
      Structured news and announcements are mapped into bounded event-policy actions. Manual review rows include operator notes, wait-for events, and questions; low-value rows are downgraded to no action.
    </p>
  </section>

  <section class="mt-8 grid gap-4 md:grid-cols-4">
    <MetricTile label="Manual Review" :value="String(actionCounts.MANUAL_REVIEW || 0)" note="Needs operator judgement" />
    <MetricTile label="Buy Watch" :value="String(actionCounts.BUY_WATCH || 0)" note="Positive evidence only" />
    <MetricTile label="Reduce Review" :value="String(actionCounts.REDUCE_EXPOSURE_REVIEW || 0)" note="Risk overlay" />
    <MetricTile label="No Action" :value="String(actionCounts.NO_ACTION || 0)" note="Ignored unless follow-up appears" />
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Policy filters</p>
        <p class="mt-1 text-sm text-ink/60">Generated {{ policyData?.generated_at || '-' }}</p>
      </div>
      <button class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" type="button" @click="refreshPolicy()">
        Refresh
      </button>
    </div>
    <div class="mt-4 flex flex-wrap gap-2">
      <button
        v-for="item in actionTypes"
        :key="item"
        class="rounded-full px-4 py-2 text-sm font-black"
        :class="selectedActionType === item ? 'bg-ink text-paper' : 'bg-white text-ink'"
        type="button"
        @click="selectedActionType = item"
      >
        {{ item }}
      </button>
    </div>
    <details class="mt-4">
      <summary class="cursor-pointer text-sm font-black text-moss">Policy class counts</summary>
      <div class="mt-3 flex flex-wrap gap-2">
        <span v-for="(count, key) in policyClassCounts" :key="key" class="rounded-full bg-white px-3 py-1 text-xs font-bold text-ink/70">
          {{ key }}: {{ count }}
        </span>
      </div>
    </details>
  </section>

  <section class="mt-6 glass-panel rounded-3xl p-5">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.25em] text-ink/45">Policy evaluation</p>
        <h2 class="mt-2 text-2xl font-black">Realized forward-return evidence</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          Research-only metrics from matured event-policy rows joined to future Dhan daily closes. Use this to tighten or monitor policy classes; it does not auto-change live thresholds.
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ policyEvalData?.status || 'missing_table' }}
      </span>
    </div>
    <div v-if="policyEvalRows.length" class="mt-5 grid gap-3 lg:grid-cols-2">
      <article v-for="(row, idx) in policyEvalRows.slice(0, 8)" :key="idx" class="rounded-2xl bg-white/70 p-4">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ row.group_type }} · {{ row.horizon_days }}d</p>
            <p class="mt-1 text-lg font-black text-ink">{{ row.group_value }}</p>
          </div>
          <span class="rounded-full bg-ink px-3 py-1 text-xs font-bold text-paper">{{ row.recommendation || 'monitor' }}</span>
        </div>
        <div class="mt-3 grid gap-2 text-sm md:grid-cols-4">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Matured:</b> {{ row.matured_count || 0 }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Avg:</b> {{ pct(row.avg_forward_return_after_cost) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Hit:</b> {{ pct(row.hit_rate_after_cost) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Score:</b> {{ numberText(row.avg_policy_score) }}</p>
        </div>
      </article>
    </div>
    <p v-else class="mt-4 rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
      No event-policy evaluation summary yet. Run `python -m advisory.event_policy_evaluator --dry-run` first, then without `--dry-run` to persist research rows.
    </p>
  </section>

  <section class="mt-8 grid gap-4">
    <RecordCard
      v-for="(row, idx) in policyRows"
      :key="`${row.unique_id}-${row.setup_id}-${idx}`"
      :title="String(row.unique_id || 'Policy event')"
      :subtitle="String(row.action_reason || row.action_detail || '')"
      :record="row"
    >
      <template #badge>
        <div class="flex flex-wrap gap-2">
          <SymbolLink :symbol="row.symbol" subtle />
          <span class="rounded-full px-3 py-1 text-xs font-bold" :class="tone(row.action_type)">{{ row.action_type || 'POLICY' }}</span>
        </div>
      </template>

      <div class="mt-4 grid gap-3 md:grid-cols-4">
        <MetricTile label="Class" :value="String(row.policy_class || row.event_class || '-')" note="Policy class" />
        <MetricTile label="Status" :value="String(row.action_status || '-')" note="Policy status" />
        <MetricTile label="Score" :value="String(row.policy_score ?? '-')" note="Policy score" />
        <MetricTile label="LLM" :value="String(row.llm_review_status || '-')" note="Manual review refinement" />
      </div>

      <div v-if="Object.keys(notes(row)).length" class="mt-4 rounded-2xl bg-paper/70 p-4">
        <p class="text-xs font-black uppercase tracking-[0.24em] text-ink/45">Operator notes</p>
        <p class="mt-2 text-sm leading-6 text-ink/70">{{ notes(row).operator_summary || notes(row).rationale || '-' }}</p>
        <p v-if="notes(row).possible_action" class="mt-3 text-sm leading-6 text-ink/70"><b>Possible action:</b> {{ notes(row).possible_action }}</p>
        <p v-if="notes(row).downgrade_reason" class="mt-3 rounded-xl bg-white px-3 py-2 text-sm font-bold text-rust">
          Downgrade: {{ notes(row).downgrade_reason }}
        </p>
        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <div v-if="asList(notes(row).wait_for_events).length">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Wait for</p>
            <ul class="mt-2 space-y-1 text-sm text-ink/70">
              <li v-for="item in asList(notes(row).wait_for_events)" :key="String(item)">- {{ item }}</li>
            </ul>
          </div>
          <div v-if="asList(notes(row).operator_questions).length">
            <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">Questions</p>
            <ul class="mt-2 space-y-1 text-sm text-ink/70">
              <li v-for="item in asList(notes(row).operator_questions)" :key="String(item)">- {{ item }}</li>
            </ul>
          </div>
        </div>
      </div>

      <details v-if="checks(row).length" class="mt-4 rounded-2xl bg-white/70 p-4">
        <summary class="cursor-pointer text-sm font-black text-moss">Policy checks</summary>
        <pre class="mt-3 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(row.checks, null, 2) }}</pre>
      </details>

      <button
        class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
        type="button"
        @click="loadTrace(row)"
      >
        {{ loadingTrace[String(row.unique_id || '')] ? 'Loading trace...' : 'Load decision trace' }}
      </button>
      <NuxtLink
        v-if="row.symbol"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
      >
        Open symbol page
      </NuxtLink>
      <NuxtLink
        v-if="row.unique_id"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id || ''))}`"
      >
        Open trace page
      </NuxtLink>
      <TraceTimeline
        v-if="traces[String(row.unique_id || '')]"
        class="mt-4"
        :trace="traces[String(row.unique_id || '')]"
        title="Event trace"
      />
    </RecordCard>
    <p v-if="!policyRows.length" class="glass-panel rounded-3xl p-6 text-ink/60">No event-policy rows found. Run `python -m advisory.event_policy` or the advisory pipeline through the event_policy stage.</p>
  </section>

  <section class="mt-10">
    <p class="text-sm font-semibold uppercase tracking-[0.3em] text-ink/45">Raw Event Feed</p>
    <h2 class="mt-3 text-3xl font-black">Latest news and announcements</h2>
  </section>

  <ApiErrorBanner v-if="eventsError" class="mt-5" title="Raw event feed failed" :error="eventsError" />

  <section class="mt-5 glass-panel rounded-3xl p-5">
    <div class="grid gap-3 md:grid-cols-[0.8fr_0.7fr_1.4fr_auto]">
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Symbol
        <input v-model="eventSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyEventFilters" />
      </label>
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Status
        <select v-model="eventStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
          <option value="all">All</option>
          <option value="error">Error</option>
          <option value="warn">Warn</option>
          <option value="parsed">Parsed</option>
          <option value="review">Review</option>
        </select>
      </label>
      <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
        Search
        <input v-model="eventSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="subject, id, event class..." @keyup.enter="applyEventFilters" />
      </label>
      <button class="self-end rounded-full bg-ink px-5 py-2 text-sm font-black text-paper" type="button" @click="applyEventFilters">
        Apply
      </button>
    </div>
    <p class="mt-3 text-sm font-semibold text-ink/55">
      Showing {{ eventMeta.returned || events.length }} of {{ eventMeta.total ?? events.length }} rows. Offset {{ eventMeta.offset || 0 }}.
    </p>
  </section>

  <section class="mt-5 grid gap-4">
    <RecordCard
      v-for="(row, idx) in events"
      :key="idx"
      :title="String(row.unique_id || 'Event')"
      :subtitle="String(row.subject || row.concise_summary_text || '')"
      :record="row"
      :detail-path="eventDetailPath(row)"
    >
      <template #badge>
        <div class="flex flex-wrap gap-2">
          <SymbolLink :symbol="row.symbol || row.ticker" subtle />
          <span class="rounded-full bg-ember px-3 py-1 text-xs font-bold text-white">{{ row.event_status || row.parse_status || 'EVENT' }}</span>
        </div>
      </template>
      <button
        class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
        type="button"
        @click="loadTrace(row)"
      >
        {{ loadingTrace[String(row.unique_id || '')] ? 'Loading trace...' : 'Load decision trace' }}
      </button>
      <NuxtLink
        v-if="row.symbol || row.ticker"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/symbols/${encodeURIComponent(String(row.symbol || row.ticker || '').toUpperCase())}`"
      >
        Open symbol page
      </NuxtLink>
      <NuxtLink
        v-if="row.unique_id"
        class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
        :to="`/decision-trace?unique_id=${encodeURIComponent(String(row.unique_id || ''))}`"
      >
        Open trace page
      </NuxtLink>
      <TraceTimeline
        v-if="traces[String(row.unique_id || '')]"
        class="mt-4"
        :trace="traces[String(row.unique_id || '')]"
        title="Event trace"
      />
    </RecordCard>
    <p v-if="!events.length" class="glass-panel rounded-3xl p-6 text-ink/60">No event rows found.</p>
  </section>

  <div v-if="eventMeta.has_more" class="mt-5 flex justify-center">
    <button class="rounded-full bg-moss px-6 py-3 text-sm font-black text-paper" type="button" @click="loadNextEvents">
      Load next {{ eventLimit }} events
    </button>
  </div>
</template>
