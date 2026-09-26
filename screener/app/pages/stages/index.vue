<script setup lang="ts">
import type { StageListResponse } from '~/types/systrader'

const STAGE_TABS: { value: number | 'all'; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 1, label: 'Stage 1 (Basing)' },
  { value: 2, label: 'Stage 2 (Advancing)' },
  { value: 3, label: 'Stage 3 (Topping)' },
  { value: 4, label: 'Stage 4 (Declining)' },
]

const STAGE_TONE: Record<number, 'good' | 'bad' | 'neutral' | 'warn'> = {
  1: 'neutral',
  2: 'good',
  3: 'warn',
  4: 'bad',
}

const selectedStage = ref<number | 'all'>('all')
const tickerSearch = ref('')

const api = useSystraderApi()
const { data, status, error } = await useAsyncData(
  () => `stages-${selectedStage.value}`,
  () => api.get<StageListResponse>(`/api/stage${selectedStage.value === 'all' ? '' : `?stages=${selectedStage.value}`}`),
  { watch: [selectedStage] },
)

const filteredRows = computed(() => {
  const rows = data.value?.rows ?? []
  const query = tickerSearch.value.trim().toUpperCase()
  if (!query) return rows
  return rows.filter(r => r.ticker.includes(query))
})

function tabCount(value: number | 'all'): number | null {
  if (!data.value) return null
  if (value === 'all') return data.value.included_count
  return data.value.counts_by_stage[String(value)] ?? 0
}
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Stage Analysis</h1>
    <p class="mt-1 text-sm text-slate-600">
      Stan Weinstein's four-stage weekly regime read (30-week MA, 4-week slope) for every
      currently-traded NSE stock with enough history. Reporting only -- a discrete label for a
      human to scan, not a signal wired into any strategy or sizing decision.
    </p>

    <div v-if="data" class="mt-3 text-xs text-slate-400">
      {{ data.included_count }} of {{ data.total_universe }} classified (as of {{ formatDate(data.as_of_now) }})
      · {{ data.excluded_stale }} excluded as stale/delisted
      <template v-if="data.excluded_short">· {{ data.excluded_short }} too short for a first read</template>
    </div>

    <div class="mt-4 flex flex-wrap items-center gap-4">
      <div class="flex flex-wrap gap-1 border-b border-slate-200">
        <button
          v-for="tab in STAGE_TABS"
          :key="tab.value"
          class="border-b-2 px-3 py-2 text-sm font-medium transition"
          :class="selectedStage === tab.value ? 'border-slate-900 text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-700'"
          @click="selectedStage = tab.value"
        >
          {{ tab.label }}
          <span v-if="tabCount(tab.value) !== null" class="ml-1 text-xs text-slate-400">({{ tabCount(tab.value) }})</span>
        </button>
      </div>
      <input
        v-model="tickerSearch"
        type="text"
        placeholder="Filter by ticker…"
        class="ml-auto rounded-md border border-slate-200 px-3 py-1.5 text-sm focus:border-slate-400 focus:outline-none"
      >
    </div>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the systrader API ({{ error.message }}). Is `go run ./cmd/api` running?
    </div>

    <div v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>

    <div v-else-if="filteredRows.length === 0" class="mt-6 text-sm text-slate-500">
      No stocks match this filter.
    </div>

    <div v-else class="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table class="w-full text-sm">
        <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
          <tr>
            <th class="px-4 py-2">Ticker</th>
            <th class="px-4 py-2">Stage</th>
            <th class="px-4 py-2 text-right">Close</th>
            <th class="px-4 py-2 text-right">30w MA</th>
            <th class="px-4 py-2 text-right">4w Slope</th>
            <th class="px-4 py-2 text-right">Vol Ratio (10w)</th>
            <th class="px-4 py-2 text-right">As Of</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in filteredRows" :key="row.ticker" class="border-b border-slate-100 last:border-0 hover:bg-slate-50">
            <td class="px-4 py-2 font-medium">{{ row.ticker }}</td>
            <td class="px-4 py-2"><BadgePill :label="row.stage_label" :tone="STAGE_TONE[row.stage]" /></td>
            <td class="px-4 py-2 text-right">{{ formatPrice(row.close) }}</td>
            <td class="px-4 py-2 text-right">{{ formatPrice(row.ma30) }}</td>
            <td class="px-4 py-2 text-right">{{ formatPct(row.slope_pct) }}</td>
            <td class="px-4 py-2 text-right">{{ row.volume_ratio === null ? '—' : `${row.volume_ratio.toFixed(2)}x` }}</td>
            <td class="px-4 py-2 text-right text-slate-400">{{ formatDate(row.as_of) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
