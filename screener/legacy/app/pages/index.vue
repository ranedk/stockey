<script setup lang="ts">
import type { UniverseCompany, UniverseGroup, UniverseResponse } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('universe', () => api.get<UniverseResponse>('/api/universe'))

const GROUP_LABELS: Record<UniverseGroup, string> = {
  operating: 'Operating',
  lender: 'Lenders',
  other_financial: 'Other financial',
  realty_holding: 'Real estate & holding',
  unlabelled: 'Unlabelled',
}
const ALLOWED_BY_LABELS: Record<string, string> = {
  turnaround: 'Turnaround',
  scaling_growth: 'Scaling growth',
}

type SortKey = 'ticker' | 'company_name' | 'group' | 'cmp_rs' | 'p_e' | 'mar_cap_rscr' | 'roce_pct' | 'qtr_sales_var_pct'

const search = ref('')
const groupFilter = ref<UniverseGroup | 'all'>('all')
const sortKey = ref<SortKey>('mar_cap_rscr')
const sortDesc = ref(true)
const showQuery = ref(false)
const showExcluded = ref(false)

const groupCounts = computed(() => {
  const counts: Partial<Record<UniverseGroup, number>> = {}
  for (const c of data.value?.companies ?? []) {
    const g = (c.group ?? 'unlabelled') as UniverseGroup
    counts[g] = (counts[g] ?? 0) + 1
  }
  return counts
})

const rows = computed<UniverseCompany[]>(() => {
  const term = search.value.trim().toLowerCase()
  const list = (data.value?.companies ?? []).filter((c) => {
    if (groupFilter.value !== 'all' && (c.group ?? 'unlabelled') !== groupFilter.value) return false
    if (!term) return true
    return c.ticker.toLowerCase().includes(term) || (c.company_name ?? '').toLowerCase().includes(term)
  })
  const key = sortKey.value
  const dir = sortDesc.value ? -1 : 1
  return [...list].sort((a, b) => {
    const av = a[key] as string | number | null
    const bv = b[key] as string | number | null
    if (av === null || av === undefined) return 1 // missing values always last
    if (bv === null || bv === undefined) return -1
    if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir
    return String(av).localeCompare(String(bv)) * dir
  })
})

function sortBy(key: SortKey) {
  if (sortKey.value === key) {
    sortDesc.value = !sortDesc.value
  } else {
    sortKey.value = key
    sortDesc.value = !['ticker', 'company_name', 'group'].includes(key)
  }
}

function arrow(key: SortKey) {
  return sortKey.value === key ? (sortDesc.value ? ' ↓' : ' ↑') : ''
}

const columns: { key: SortKey; label: string; right?: boolean }[] = [
  { key: 'ticker', label: 'Ticker' },
  { key: 'company_name', label: 'Company' },
  { key: 'group', label: 'Group' },
  { key: 'cmp_rs', label: 'Price', right: true },
  { key: 'p_e', label: 'P/E', right: true },
  { key: 'mar_cap_rscr', label: 'Mkt cap (₹cr)', right: true },
  { key: 'roce_pct', label: 'ROCE', right: true },
  { key: 'qtr_sales_var_pct', label: 'Qtr sales var', right: true },
]
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Universe</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every company that currently passes both layers of the universe screen. This is the
      pool the rest of the pipeline (alerts, watchlist, narratives, portfolio) draws from.
    </p>
    <PipelineNote layer="L1" title="How the universe is built">
      <strong>Layer 1</strong>, the same for every stock: NSE main board (not SME, not trade-to-trade);
      not under serious NSE surveillance (any GSM stage, or ASM stage 2 and above); market cap of at
      least ₹300 cr; a median of at least ₹50 lakh traded a day over the last three months; and
      listed for at least a year. Each stock is then put in a group by business model.
      <strong>Layer 2</strong> removes fundamentally weak businesses, with checks that depend on the
      group: lenders on NPAs and return on assets; other financial companies and holding companies on
      losses and net worth; property developers on debt; operating companies on two years of losses,
      cash burn with a loss, or heavy debt with thin interest cover. Promoter pledge of 50% or more
      excludes in every group. Two rules let a loss-maker back in: a <em>turnaround</em> (profitable
      over the last twelve months and the last two quarters) and <em>scaling growth</em> (sales up
      20%+ with losses narrowing, low debt, large and heavily traded). Companies with an auditor
      change or a material related-party transaction are then excluded.
    </PipelineNote>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}). Is `uvicorn fundamentals.api.app:app` running?
    </div>

    <template v-else-if="data">
      <div class="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <button class="text-sm font-medium text-slate-700 hover:text-slate-900" @click="showQuery = !showQuery">
          {{ showQuery ? 'Hide' : 'Show' }} screen definition (v{{ data.query_version }}, as of {{ formatDate(data.run_date) }})
        </button>
        <pre v-if="showQuery" class="mt-3 overflow-x-auto rounded bg-slate-50 p-3 text-xs text-slate-700">{{ data.query_text }}</pre>
      </div>

      <div class="mt-4 flex flex-wrap items-center gap-2">
        <button
          class="rounded-full border px-3 py-1 text-xs"
          :class="groupFilter === 'all' ? 'border-slate-800 bg-slate-800 text-white' : 'border-slate-300 text-slate-700 hover:bg-slate-50'"
          @click="groupFilter = 'all'"
        >
          All {{ data.companies.length }}
        </button>
        <button
          v-for="(label, key) in GROUP_LABELS"
          v-show="groupCounts[key]"
          :key="key"
          class="rounded-full border px-3 py-1 text-xs"
          :class="groupFilter === key ? 'border-slate-800 bg-slate-800 text-white' : 'border-slate-300 text-slate-700 hover:bg-slate-50'"
          @click="groupFilter = key"
        >
          {{ label }} {{ groupCounts[key] }}
        </button>
        <input
          v-model="search"
          type="search"
          placeholder="Search ticker or company"
          class="ml-auto w-full rounded border border-slate-300 px-3 py-1.5 text-sm sm:w-64"
        >
      </div>

      <div class="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th
                v-for="col in columns"
                :key="col.key"
                class="cursor-pointer select-none whitespace-nowrap px-4 py-2 hover:text-slate-800"
                :class="col.right ? 'text-right' : ''"
                @click="sortBy(col.key)"
              >
                {{ col.label }}{{ arrow(col.key) }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in rows" :key="c.ticker" class="border-b border-slate-100 last:border-0">
              <td class="px-4 py-2 font-medium">{{ c.ticker }}</td>
              <td class="px-4 py-2 text-slate-700">{{ c.company_name }}</td>
              <td class="whitespace-nowrap px-4 py-2 text-slate-600">
                {{ c.group ? GROUP_LABELS[c.group] : '—' }}
                <span
                  v-if="c.allowed_by"
                  class="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-800"
                  :title="'Let through Layer 2 by the ' + ALLOWED_BY_LABELS[c.allowed_by] + ' rule'"
                >{{ ALLOWED_BY_LABELS[c.allowed_by] }}</span>
              </td>
              <td class="px-4 py-2 text-right">{{ formatPrice(c.cmp_rs) }}</td>
              <td class="px-4 py-2 text-right">{{ c.p_e ?? '—' }}</td>
              <td class="px-4 py-2 text-right">{{ c.mar_cap_rscr?.toLocaleString('en-IN') ?? '—' }}</td>
              <td class="px-4 py-2 text-right">{{ c.roce_pct !== null ? c.roce_pct + '%' : '—' }}</td>
              <td class="px-4 py-2 text-right">{{ formatPct(c.qtr_sales_var_pct) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-2 text-xs text-slate-500">Showing {{ rows.length }} of {{ data.companies.length }} companies</p>

      <div v-if="data.excluded.length" class="mt-6 rounded-lg border border-slate-200 bg-white p-4">
        <button class="text-sm font-medium text-slate-700 hover:text-slate-900" @click="showExcluded = !showExcluded">
          {{ showExcluded ? 'Hide' : 'Show' }} the {{ data.excluded.length }} companies Layer 2 excluded, and why
        </button>
        <table v-if="showExcluded" class="mt-3 w-full text-sm">
          <thead class="border-b border-slate-200 text-left text-xs uppercase text-slate-500">
            <tr>
              <th class="py-2 pr-4">Ticker</th>
              <th class="py-2 pr-4">Group</th>
              <th class="py-2">Reason</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="x in data.excluded" :key="x.ticker" class="border-b border-slate-100 last:border-0">
              <td class="py-2 pr-4 font-medium">{{ x.ticker }}</td>
              <td class="whitespace-nowrap py-2 pr-4 text-slate-600">{{ x.group ? GROUP_LABELS[x.group] : '—' }}</td>
              <td class="py-2 text-slate-700">{{ x.reasons.join('; ') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
