<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const statusFilter = ref('open')

const { data, pending, error: loadError, refresh } = await useAsyncData(
  'positions',
  () => api.getPositions({ status: statusFilter.value || undefined }),
  { watch: [statusFilter] }
)

const rows = computed(() => asList(data.value?.positions))
const summary = computed(() => asDict(data.value?.summary))
const whySymbol = ref<string | null>(null)
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
function fmtDate(value: unknown) {
  const text = String(value || '')
  return text ? text.slice(0, 10) : '-'
}
function changeClass(pct: unknown) {
  if (typeof pct !== 'number') return 'text-ink/50'
  if (pct > 0) return 'text-moss font-semibold'
  if (pct < 0) return 'text-rust font-semibold'
  return 'text-ink/50'
}

async function exitPosition(row: Dict) {
  const symbol = String(row.symbol)
  busy.value = symbol
  try {
    await api.exitPosition({
      symbol,
      entry_date: fmtDate(row.entry_date),
      exit_price: row.current_price
    })
    await refresh()
  } catch {
    // surfaced via refresh / error state
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="space-y-6">
    <header class="rounded-3xl border border-ink/10 bg-ink px-8 py-7 text-paper">
      <p class="text-xs font-black uppercase tracking-[0.2em] text-paper/50">Review-only · tracked monitoring, not orders</p>
      <h1 class="mt-2 text-3xl font-black tracking-tight">Positions</h1>
      <p class="mt-2 max-w-2xl text-paper/70">
        Recommendations you marked as taken, tracked from entry. The move since entry is informational —
        there is no simulated P&L and no broker order is ever submitted.
      </p>
    </header>

    <div v-if="loadError" class="rounded-2xl border border-rust/25 bg-rust/10 px-5 py-4 text-sm text-rust">
      Could not load positions.
    </div>

    <div class="flex flex-wrap items-center gap-3">
      <span class="rounded-full border border-ink/15 bg-white/70 px-4 py-1.5 text-sm font-black text-ink/70">
        {{ display(summary.open) }} open · {{ display(summary.exited) }} exited
      </span>
      <div class="ml-auto flex gap-2">
        <button v-for="opt in ['open', 'exited', '']" :key="opt || 'all'"
                class="rounded-full border px-4 py-1.5 text-sm font-semibold"
                :class="statusFilter === opt ? 'border-ink/40 bg-ink text-paper' : 'border-ink/15 bg-white/70 text-ink/55 hover:bg-white'"
                @click="statusFilter = opt">{{ opt || 'all' }}</button>
      </div>
      <span v-if="pending" class="text-sm text-ink/40">loading…</span>
    </div>

    <div class="overflow-x-auto rounded-2xl border border-ink/10 bg-white/60">
      <table class="w-full text-sm">
        <thead class="text-left text-xs font-black uppercase tracking-wide text-ink/45">
          <tr class="border-b border-ink/10">
            <th class="px-4 py-3">Symbol</th>
            <th class="px-4 py-3">Action</th>
            <th class="px-4 py-3">Entry</th>
            <th class="px-4 py-3">Entry date</th>
            <th class="px-4 py-3">Current</th>
            <th class="px-4 py-3">Change</th>
            <th class="px-4 py-3">Status</th>
            <th class="px-4 py-3"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, idx) in rows" :key="idx" class="border-b border-ink/5 hover:bg-white/80">
            <td class="px-4 py-3 font-black text-ink">
              <button class="underline-offset-2 hover:underline" @click="whySymbol = String(row.symbol)">{{ display(row.symbol) }}</button>
            </td>
            <td class="px-4 py-3 font-semibold">{{ display(row.action) }}</td>
            <td class="px-4 py-3 text-ink/70">{{ display(row.entry_price) }}</td>
            <td class="px-4 py-3 text-ink/55">{{ fmtDate(row.entry_date) }}</td>
            <td class="px-4 py-3 text-ink/70">{{ display(row.current_price) }}</td>
            <td class="px-4 py-3" :class="changeClass(row.change_pct_since_entry)">
              {{ typeof row.change_pct_since_entry === 'number' ? `${row.change_pct_since_entry > 0 ? '+' : ''}${row.change_pct_since_entry}%` : '-' }}
            </td>
            <td class="px-4 py-3 text-ink/55">{{ display(row.status) }}</td>
            <td class="px-4 py-3 text-right">
              <button v-if="String(row.status) === 'open'"
                      class="rounded-full border border-rust/30 bg-rust/10 px-3 py-1.5 text-xs font-semibold text-rust hover:bg-rust/20 disabled:opacity-40"
                      :disabled="busy === String(row.symbol)" @click="exitPosition(row)">Exit</button>
            </td>
          </tr>
          <tr v-if="!rows.length && !pending">
            <td colspan="8" class="px-4 py-8 text-center text-ink/40">No positions for this filter.</td>
          </tr>
        </tbody>
      </table>
    </div>

    <DecisionWhy v-if="whySymbol" :symbol="whySymbol" @close="whySymbol = null" />
  </section>
</template>
