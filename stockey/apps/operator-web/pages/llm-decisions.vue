<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const symbolFilter = ref('')
const dateFilter = ref('')
const offset = ref(0)
const limit = 50

const queryParams = computed(() => ({
  symbol: symbolFilter.value.trim() || undefined,
  asof_date: dateFilter.value.trim() || undefined,
  limit,
  offset: offset.value
}))

const { data, pending, error: loadError, refresh } = await useAsyncData(
  'llm-decisions',
  () => api.getLlmDecisions(queryParams.value),
  { watch: [queryParams] }
)

const decisions = computed(() => asList(data.value?.decisions))
const summary = computed(() => asDict(data.value?.summary))
const page = computed(() => asDict(data.value?.page))
const byAction = computed(() => asDict(summary.value.by_action))
const skipped = computed(() => asList(data.value?.skipped))
const reviewOnly = computed(() => data.value ? Boolean(data.value.review_only) : true)
const nextOffset = computed(() => typeof page.value.next_offset === 'number' ? page.value.next_offset as number : null)
const llmDisabled = computed(() => decisions.value.length > 0 && decisions.value.every((row) => String(row.llm_status) === 'disabled'))

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
function asList(value: unknown): Dict[] {
  return Array.isArray(value) ? value.filter((item) => typeof item === 'object' && item !== null) as Dict[] : []
}
function display(value: unknown) {
  if (value === null || value === undefined || value === '') return '-'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  return String(value)
}
function fmtDate(value: unknown) {
  const text = String(value || '')
  return text ? text.slice(0, 10) : '-'
}
function modeClass(mode: unknown) {
  const m = String(mode || '')
  if (m === 'alpha') return 'border-moss/25 bg-moss/10 text-moss'
  if (m === 'participation') return 'border-ink/15 bg-white/70 text-ink/60'
  return 'border-ink/10 bg-white/50 text-ink/40'
}
function goNext() { if (nextOffset.value !== null) offset.value = nextOffset.value }
function goPrev() { offset.value = Math.max(0, offset.value - limit) }
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · nothing here trades</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Daily Decisions</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        What the LLM decision policy proposed for each symbol, graded and sized. Recorded so outcomes can
        mature; no broker order is ever submitted from this view.
      </p>
    </header>

    <ApiErrorBanner v-if="loadError" :error="loadError" title="Could not load decisions" />

    <div v-if="llmDisabled" class="rounded-2xl border border-ink/15 bg-white/70 px-5 py-4 text-sm text-ink/65">
      <span class="font-black text-ink/80">LLM direct authority is OFF</span> (master flag defaults off).
      These rows are the <span class="font-semibold">deterministic review-only fallback</span> — every
      decision shows <code class="rounded bg-ink/5 px-1.5 py-0.5">llm = disabled</code> and resolves to
      <code class="rounded bg-ink/5 px-1.5 py-0.5">WATCH</code>. This is expected, not an error: enable
      the flag and re-run with the LLM to get graded BUY/SELL/size proposals.
    </div>

    <div v-if="skipped.length" class="rounded-2xl border border-ink/15 bg-white/70 px-5 py-4 text-sm text-ink/60">
      No decisions yet. Run the daily decision runner to populate them:
      <code class="rounded bg-ink/5 px-1.5 py-0.5 text-ink/80">python -m advisory.llm_decision_runner --date YYYY-MM-DD --persist</code>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <span class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 text-sm font-black text-ink/70">
        {{ display(summary.total) }} decisions
      </span>
      <span class="rounded-full border border-moss/25 bg-moss/10 px-4 py-1.5 text-sm font-black text-moss">
        {{ display(summary.grounded_for_live) }} grounded
      </span>
      <span v-for="(count, action) in byAction" :key="action"
            class="rounded-full border border-ink/10 bg-white/60 px-3 py-1.5 text-xs font-semibold text-ink/55">
        {{ action }}: {{ display(count) }}
      </span>
      <span v-if="reviewOnly" class="ml-auto rounded-full border border-ink/15 bg-white/70 px-3 py-1.5 text-xs font-semibold text-ink/45">
        broker_execution_allowed = false
      </span>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <input v-model="symbolFilter" placeholder="symbol"
             class="rounded-full border border-ink/15 bg-white px-4 py-2 text-sm" />
      <input v-model="dateFilter" placeholder="YYYY-MM-DD"
             class="rounded-full border border-ink/15 bg-white px-4 py-2 text-sm" />
      <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white"
              @click="refresh()">Refresh</button>
      <span v-if="pending" class="text-sm text-ink/40">loading…</span>
    </div>

    <div class="overflow-x-auto rounded-2xl border border-ink/10 bg-white/60">
      <table class="w-full text-sm">
        <thead class="text-left text-xs font-black uppercase tracking-wide text-ink/45">
          <tr class="border-b border-ink/10">
            <th class="px-4 py-3">Date</th>
            <th class="px-4 py-3">Symbol</th>
            <th class="px-4 py-3">Action</th>
            <th class="px-4 py-3">Mode</th>
            <th class="px-4 py-3">Conviction</th>
            <th class="px-4 py-3">Grounded</th>
            <th class="px-4 py-3">Path</th>
            <th class="px-4 py-3">LLM</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in decisions" :key="idx" class="border-b border-ink/5 hover:bg-white/80">
            <td class="px-4 py-3 text-ink/60">{{ fmtDate(row.asof_date) }}</td>
            <td class="px-4 py-3 font-black text-ink">{{ display(row.symbol) }}</td>
            <td class="px-4 py-3 font-semibold">{{ display(row.proposed_action) }}</td>
            <td class="px-4 py-3">
              <span class="rounded-full border px-2.5 py-1 text-xs font-semibold" :class="modeClass(row.decision_mode)">
                {{ display(row.decision_mode) }}
              </span>
            </td>
            <td class="px-4 py-3 text-ink/70">{{ display(row.conviction) }}</td>
            <td class="px-4 py-3">{{ display(row.meets_data_grounding_for_live) }}</td>
            <td class="px-4 py-3 text-ink/55">{{ display(row.sufficiency_path) }}</td>
            <td class="px-4 py-3 text-ink/50">{{ display(row.llm_status) }}</td>
          </tr>
          <tr v-if="!decisions.length && !pending">
            <td colspan="8" class="px-4 py-8 text-center text-ink/40">No decisions for this filter.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="flex items-center justify-between text-sm text-ink/50">
      <span>Showing {{ display(page.returned) }} of {{ display(page.total) }} · offset {{ display(page.offset) }}</span>
      <div class="flex gap-2">
        <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 font-semibold disabled:opacity-40"
                :disabled="offset === 0" @click="goPrev()">Prev</button>
        <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 font-semibold disabled:opacity-40"
                :disabled="nextOffset === null" @click="goNext()">Next</button>
      </div>
    </div>
  </section>
</template>
