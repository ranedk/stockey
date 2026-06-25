<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const actionFilter = ref('')
const onlyConflicts = ref(false)
const offset = ref(0)
const limit = 50

const queryParams = computed(() => ({
  action: actionFilter.value.trim() || undefined,
  only_conflicts: onlyConflicts.value || undefined,
  limit,
  offset: offset.value
}))

const { data, pending, error: loadError, refresh } = await useAsyncData(
  'recommendations-unified',
  () => api.getRecommendationsUnified(queryParams.value),
  { watch: [queryParams] }
)

const rows = computed(() => asList(data.value?.recommendations))
const summary = computed(() => asDict(data.value?.summary))
const page = computed(() => asDict(data.value?.page))
const byAction = computed(() => asDict(summary.value.by_action))
const nextOffset = computed(() => typeof page.value.next_offset === 'number' ? page.value.next_offset as number : null)

const whySymbol = ref<string | null>(null)
const dismissed = ref<Set<string>>(new Set())
const busy = ref<string | null>(null)

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
const visibleRows = computed(() => rows.value.filter((r) => !dismissed.value.has(String(r.symbol))))

watch([actionFilter, onlyConflicts], () => { offset.value = 0 })

async function take(row: Dict) {
  const symbol = String(row.symbol)
  busy.value = symbol
  try {
    await api.takePosition({
      symbol,
      action: row.action,
      entry_price: row.reference_price,
      source: 'recommendations-unified'
    })
    await refresh()
  } catch {
    // surfaced via refresh / error state
  } finally {
    busy.value = null
  }
}
function dismiss(row: Dict) {
  dismissed.value = new Set([...dismissed.value, String(row.symbol)])
}
function toggleActionFilter(action: string) {
  const current = actionFilter.value.trim().toUpperCase()
  actionFilter.value = current === action.toUpperCase() ? '' : action
  offset.value = 0
}
function agreeClass(row: Dict) {
  if (row.conflict) return 'border-rust/30 bg-rust/10 text-rust'
  if (row.agree) return 'border-moss/25 bg-moss/10 text-moss'
  return 'border-ink/10 bg-white/60 text-ink/45'
}
function goNext() { if (nextOffset.value !== null) offset.value = nextOffset.value }
function goPrev() { offset.value = Math.max(0, offset.value - limit) }
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · one row per symbol</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Recommendations</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        The deterministic action and the LLM decision for each symbol, merged. Take a recommendation to
        track it as a holding; dismiss to clear it from the queue. Nothing here submits a broker order.
      </p>
    </header>

    <div v-if="loadError" class="rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">
      Could not load recommendations. The API may be down, or none have been generated yet.
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <span class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 text-sm font-black text-ink/70">
        {{ display(summary.total) }} recommendations
      </span>
      <span class="rounded-full border border-moss/25 bg-moss/10 px-4 py-1.5 text-sm font-black text-moss">
        {{ display(summary.agree) }} agree
      </span>
      <span class="rounded-full border border-rust/25 bg-rust/10 px-4 py-1.5 text-sm font-black text-rust">
        {{ display(summary.conflict) }} conflict
      </span>
      <button v-for="(count, action) in byAction" :key="action"
              class="rounded-full border px-3 py-1.5 text-xs font-semibold transition"
              :class="actionFilter.trim().toUpperCase() === String(action).toUpperCase()
                ? 'border-ink/40 bg-ink text-paper'
                : 'border-ink/10 bg-white/60 text-ink/55 hover:bg-white'"
              @click="toggleActionFilter(String(action))">
        {{ action }}: {{ display(count) }}
      </button>
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <input v-model="actionFilter" placeholder="action (BUY/SELL/WATCH)"
             class="rounded-full border border-ink/15 bg-white px-4 py-2 text-sm" />
      <label class="flex items-center gap-2 text-sm text-ink/60">
        <input v-model="onlyConflicts" type="checkbox" /> only conflicts
      </label>
      <button class="rounded-full border border-ink/15 bg-white/70 px-4 py-2 text-sm font-semibold hover:bg-white"
              @click="refresh()">Refresh</button>
      <span v-if="pending" class="text-sm text-ink/40">loading…</span>
    </div>

    <div class="overflow-x-auto rounded-2xl border border-ink/10 bg-white/60">
      <table class="w-full text-sm">
        <thead class="text-left text-xs font-black uppercase tracking-wide text-ink/45">
          <tr class="border-b border-ink/10">
            <th class="px-4 py-3">Symbol</th>
            <th class="px-4 py-3">Action</th>
            <th class="px-4 py-3">LLM</th>
            <th class="px-4 py-3">Mode</th>
            <th class="px-4 py-3">Agreement</th>
            <th class="px-4 py-3">Ref price</th>
            <th class="px-4 py-3"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in visibleRows" :key="idx" class="border-b border-ink/5 hover:bg-white/80">
            <td class="px-4 py-3 font-black text-ink">
              <button class="underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
            </td>
            <td class="px-4 py-3 font-semibold">{{ display(row.action) }}</td>
            <td class="px-4 py-3 text-ink/70">{{ display(row.llm_action) }}</td>
            <td class="px-4 py-3 text-ink/55">{{ display(row.llm_mode) }}</td>
            <td class="px-4 py-3">
              <span class="rounded-full border px-2.5 py-1 text-xs font-semibold" :class="agreeClass(row)">
                {{ row.conflict ? 'conflict' : row.agree ? 'agree' : '—' }}
              </span>
            </td>
            <td class="px-4 py-3 text-ink/60">{{ display(row.reference_price) }}</td>
            <td class="px-4 py-3">
              <div class="flex justify-end gap-2">
                <button class="rounded-full border border-moss/30 bg-moss/10 px-3 py-1.5 text-xs font-semibold text-moss hover:bg-moss/20 disabled:opacity-40"
                        :disabled="busy === String(row.symbol)" @click="take(row)">Take</button>
                <button class="rounded-full border border-ink/15 bg-white/70 px-3 py-1.5 text-xs font-semibold text-ink/55 hover:bg-white"
                        @click="dismiss(row)">Dismiss</button>
              </div>
            </td>
          </tr>
          <tr v-if="!visibleRows.length && !pending">
            <td colspan="7" class="px-4 py-8 text-center text-ink/40">No recommendations for this filter.</td>
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

    <DecisionWhy v-if="whySymbol" :symbol="whySymbol" @close="whySymbol = null" />
  </section>
</template>
