<script setup lang="ts">
import type { CoverageReportResponse } from '~/types/api'

// data_coverage_report has been written nightly for 16 runs and read only as a
// markdown file. The trend is the point: rising staleness precedes outright failure.
const api = useApi()
const { data, status, error } = await useAsyncData(
  'coverage-report',
  () => api.get<CoverageReportResponse>('/api/coverage-report'),
)

const showAll = ref(false)

const visible = computed(() => {
  const tables = data.value?.tables ?? []
  return showAll.value ? tables : tables.filter(t => t.status !== 'ok')
})

// A tiny inline sparkline of staleness_days. Nulls are gaps, not zeros -- a table with
// no date column has no staleness, which is not the same as being perfectly fresh.
function sparkPoints(name: string): string {
  const series = (data.value?.staleness_history?.[name] ?? []).filter(p => p.staleness_days !== null)
  if (series.length < 2) return ''
  const vals = series.map(p => p.staleness_days as number)
  const max = Math.max(...vals, 1)
  const w = 90
  const h = 18
  return vals
    .map((v, i) => `${(i / (vals.length - 1)) * w},${h - (v / max) * h}`)
    .join(' ')
}
</script>

<template>
  <section class="mt-6">
    <div class="flex flex-wrap items-center gap-3">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Table coverage</h2>
      <BadgePill
        v-if="data"
        :label="data.not_ok_count === 0 ? 'all tables ok' : `${data.not_ok_count} not ok`"
        :tone="data.not_ok_count === 0 ? 'good' : 'warn'"
      />
      <span v-if="data?.report_date" class="text-xs text-slate-400">
        nightly report of {{ formatDate(data.report_date) }}
      </span>
    </div>

    <p class="mt-1 text-xs text-slate-400">
      Per-table row counts and staleness from the nightly coverage job. The sparkline is
      staleness in days — a rising line is a feed degrading before it fails outright.
    </p>

    <div v-if="error" class="mt-3 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
      Could not load the coverage report ({{ error.message }}).
    </div>
    <div v-else-if="status === 'pending'" class="mt-3 text-sm text-slate-500">Loading…</div>

    <template v-else-if="data">
      <div class="mt-3 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-xs">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-slate-500">
            <tr>
              <th class="px-3 py-2 font-medium">Table</th>
              <th class="px-3 py-2 font-medium">Category</th>
              <th class="px-3 py-2 text-right font-medium">Rows</th>
              <th class="px-3 py-2 text-right font-medium">Stale (d)</th>
              <th class="px-3 py-2 font-medium">Trend</th>
              <th class="px-3 py-2 font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in visible" :key="t.table_name" class="border-b border-slate-100 last:border-0">
              <td class="px-3 py-1.5 font-medium text-slate-800">{{ t.table_name }}</td>
              <td class="px-3 py-1.5 text-slate-500">{{ t.category || '—' }}</td>
              <td class="px-3 py-1.5 text-right text-slate-600">{{ t.rows === null ? '—' : t.rows.toLocaleString('en-IN') }}</td>
              <td class="px-3 py-1.5 text-right text-slate-600">{{ t.staleness_days === null ? '—' : t.staleness_days }}</td>
              <td class="px-3 py-1.5">
                <svg v-if="sparkPoints(t.table_name)" width="90" height="18" class="text-slate-400">
                  <polyline :points="sparkPoints(t.table_name)" fill="none" stroke="currentColor" stroke-width="1.2" />
                </svg>
                <span v-else class="text-slate-300">—</span>
              </td>
              <td class="px-3 py-1.5">
                <BadgePill :label="t.status" :tone="t.status === 'ok' ? 'good' : 'warn'" />
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <button class="mt-2 text-xs font-medium text-slate-500 hover:text-slate-900" @click="showAll = !showAll">
        {{ showAll ? 'Show only problems' : `Show all ${data.tables.length} tables` }}
      </button>
    </template>
  </section>
</template>
