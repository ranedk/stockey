<script setup lang="ts">
import type { QualColumn, QualMatrix } from '~/types/systrader'

// Every stock that qualifies for any tracked strategy — or any variant inside a
// blend — and which ones. The operator's view across every tracked idea at
// once: the more columns a stock fills, the more of them agree on it. A
// combination track is left out by the API: its columns would repeat its
// parents'.
const props = defineProps<{ matrix: QualMatrix }>()

const filter = ref('')
const minCount = ref(1)

const columns = computed(() => props.matrix.columns ?? [])
const allRows = computed(() => props.matrix.rows ?? [])
const rows = computed(() => {
  const f = filter.value.trim().toUpperCase()
  return allRows.value.filter(r => r.count >= minCount.value && (!f || r.symbol.includes(f)))
})

function strategyTitle(c: QualColumn): string {
  return paperStrategyName(c.strategy)
}

// Column groups: one header cell per strategy spanning its variants.
const groups = computed(() => {
  const out: { strategy: string, span: number }[] = []
  for (const c of columns.value) {
    const last = out[out.length - 1]
    if (last && last.strategy === c.strategy) last.span++
    else out.push({ strategy: c.strategy, span: 1 })
  }
  return out
})
const hasVariants = computed(() => columns.value.some(c => c.label.includes(' · ')))
</script>

<template>
  <section class="mt-10">
    <h2 class="text-base font-semibold text-slate-900">Which strategies each stock qualifies for</h2>
    <p class="mt-1 max-w-3xl text-sm text-slate-600">
      Every stock in the top fifth of any tracked strategy — or of any variant inside a blend — on the latest
      decision day<template v-if="matrix.as_of"> ({{ formatDate(matrix.as_of) }})</template>. Qualifying is
      not holding: each book only trades on its own rebalance clock. The more columns a stock fills, the more
      of these strategies agree on it. A combination track is not shown separately — its columns are its
      parents'.
    </p>

    <div class="mt-3 flex flex-wrap items-center gap-3 text-sm">
      <input
        v-model="filter"
        type="text"
        placeholder="Filter by symbol"
        class="w-48 rounded-md border border-slate-300 px-2 py-1 text-sm"
      >
      <label class="flex items-center gap-2 text-slate-600">
        qualifies for at least
        <select v-model.number="minCount" class="rounded-md border border-slate-300 px-2 py-1 text-sm">
          <option v-for="n in columns.length" :key="n" :value="n">{{ n }}</option>
        </select>
        of {{ columns.length }}
      </label>
      <span class="text-xs text-slate-500">showing {{ rows.length }} of {{ allRows.length }} stocks</span>
    </div>

    <div class="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table class="min-w-full text-xs">
        <thead class="bg-slate-50 text-slate-600">
          <tr v-if="hasVariants">
            <th class="sticky left-0 bg-slate-50 px-3 py-1" />
            <th
              v-for="g in groups"
              :key="g.strategy"
              :colspan="g.span"
              class="border-l border-slate-200 px-2 py-1 text-center font-semibold"
            >
              {{ paperStrategyName(g.strategy) }}
            </th>
            <th class="px-3 py-1" />
          </tr>
          <tr>
            <th class="sticky left-0 bg-slate-50 px-3 py-2 text-left font-semibold">Stock</th>
            <th
              v-for="c in columns"
              :key="c.strategy + '/' + c.variant"
              class="border-l border-slate-200 px-2 py-2 text-center font-medium"
            >
              {{ hasVariants && c.label.includes(' · ') ? c.variant : strategyTitle(c) }}
            </th>
            <th class="px-3 py-2 text-right font-semibold">Count</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.symbol" class="border-t border-slate-100 hover:bg-slate-50">
            <td class="sticky left-0 bg-white px-3 py-1.5 font-medium text-slate-900">{{ r.symbol }}</td>
            <td
              v-for="(m, i) in r.marks"
              :key="i"
              class="border-l border-slate-100 px-2 py-1.5 text-center"
            >
              <span v-if="m" class="font-semibold text-emerald-600">✓</span>
              <span v-else class="text-slate-300">·</span>
            </td>
            <td class="px-3 py-1.5 text-right tabular-nums text-slate-700">{{ r.count }}/{{ columns.length }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
