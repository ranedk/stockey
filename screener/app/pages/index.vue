<script setup lang="ts">
import type { UniverseResponse } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('universe', () => api.get<UniverseResponse>('/api/universe'))

const showQuery = ref(false)
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Universe</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every company that currently passes the L1 fundamental screen. This is the
      pool the rest of the pipeline (alerts, watchlist, narratives) draws from.
    </p>
    <PipelineNote layer="L1" title="The universe filter">
      One screener.in query, applied at crawl time rather than as a filter over everything
      afterwards. A company is in the universe when <em>all</em> of these hold: market cap between
      ₹100 cr and ₹5,000 cr; one-month average traded value above ₹10 lakh a day; FII + DII holding
      under 20%; fewer than 50,000 shareholders; two-year cash conversion (cash from operations ÷
      operating profit) of at least 0.6; debtor days no higher than three years ago; and contingent
      liabilities under 25% of net worth. Companies with an auditor change or a problematic
      related-party record are then excluded. The intent is small, under-owned, cash-generating
      businesses — everything downstream only ever looks at names that pass here.
    </PipelineNote>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}). Is `uvicorn fundamentals.api.app:app` running?
    </div>

    <template v-else-if="data">
      <div class="mt-4 rounded-lg border border-slate-200 bg-white p-4">
        <button class="text-sm font-medium text-slate-700 hover:text-slate-900" @click="showQuery = !showQuery">
          {{ showQuery ? 'Hide' : 'Show' }} screening query (v{{ data.query_version }}, as of {{ formatDate(data.run_date) }})
        </button>
        <pre v-if="showQuery" class="mt-3 overflow-x-auto rounded bg-slate-50 p-3 text-xs text-slate-700">{{ data.query_text }}</pre>
      </div>

      <div class="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white">
        <table class="w-full text-sm">
          <thead class="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase text-slate-500">
            <tr>
              <th class="px-4 py-2">Ticker</th>
              <th class="px-4 py-2">Company</th>
              <th class="px-4 py-2 text-right">Price</th>
              <th class="px-4 py-2 text-right">P/E</th>
              <th class="px-4 py-2 text-right">Mkt cap (₹cr)</th>
              <th class="px-4 py-2 text-right">ROCE</th>
              <th class="px-4 py-2 text-right">Qtr sales var</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="c in data.companies" :key="c.ticker" class="border-b border-slate-100 last:border-0">
              <td class="px-4 py-2 font-medium">{{ c.ticker }}</td>
              <td class="px-4 py-2 text-slate-700">{{ c.company_name }}</td>
              <td class="px-4 py-2 text-right">{{ formatPrice(c.cmp_rs) }}</td>
              <td class="px-4 py-2 text-right">{{ c.p_e ?? '—' }}</td>
              <td class="px-4 py-2 text-right">{{ c.mar_cap_rscr ?? '—' }}</td>
              <td class="px-4 py-2 text-right">{{ c.roce_pct ?? '—' }}%</td>
              <td class="px-4 py-2 text-right">{{ formatPct(c.qtr_sales_var_pct) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="mt-2 text-xs text-slate-500">{{ data.companies.length }} companies</p>
    </template>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
