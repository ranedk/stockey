<script setup lang="ts">
useHead({ title: 'Sectors' })
const { data } = useApi<any>('market/overview')
const period = ref<'ret_1d' | 'ret_1m' | 'ret_1y'>('ret_1m')
const sortBy = ref<'ret' | 'mcap' | 'uptrend'>('ret')
const rows = computed(() => {
  const r = [...(data.value?.industries || [])]
  const key = sortBy.value === 'ret' ? period.value : sortBy.value === 'mcap' ? 'mcap' : 'share_uptrend'
  return r.sort((a: any, b: any) => (b[key] ?? -1e9) - (a[key] ?? -1e9))
})
const totalMcap = computed(() => (data.value?.industries || []).reduce((s: number, x: any) => s + (x.mcap || 0), 0))
const bars = computed(() => rows.value.map((r: any) => ({ label: r.sector, value: r[period.value] })))

// Second view, from stockey's sector cycle (fundamentals API): is capacity growing faster
// than sales? One of the watchlist's five scoring parts; it does not rank sectors.
const view = ref<'returns' | 'capacity'>('returns')
const { data: cycle } = useStockey<any[]>('/api/sectors', { lazy: true })
const PHASE: Record<string, { label: string, color: any, tip: string }> = {
  capacity_discipline: { label: 'Capacity held back', color: 'success', tip: 'Sales growing faster than capacity: room for better prices and margins' },
  balanced: { label: 'Balanced', color: 'neutral', tip: 'Capacity and sales growing at about the same pace' },
  capacity_expansion: { label: 'Capacity running ahead', color: 'error', tip: 'Capacity growing faster than sales: risk of price pressure' },
}
const GROWTH: Record<string, { label: string, color: any }> = {
  high_growth: { label: 'High growth', color: 'success' }, medium_growth: { label: 'Medium growth', color: 'neutral' },
  low_growth: { label: 'Low growth', color: 'warning' }, no_pattern: { label: 'No pattern', color: 'neutral' },
}
const cycleRows = computed(() => [...(cycle.value || [])].sort((a, b) =>
  (a.capacity_minus_demand_pts ?? 1e9) - (b.capacity_minus_demand_pts ?? 1e9)))
const scatter = computed(() => cycleRows.value
  .filter(r => r.phase && isNum(r.capacity_growth_pct) && isNum(r.demand_growth_pct))
  .map(r => ({ label: r.sector_name || r.sector_code, sales: r.demand_growth_pct, capacity: r.capacity_growth_pct, phase: r.phase })))
const shown = ref<Record<string, boolean>>({})
</script>

<template>
  <UDashboardPanel id="sectors">
    <template #header>
      <PageNavbar title="Sectors" icon="i-lucide-blocks" />
    </template>
    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <UTabs v-model="view" :items="[{ label: 'Returns and trend', value: 'returns', icon: 'i-lucide-trending-up' }, { label: 'Capacity vs demand', value: 'capacity', icon: 'i-lucide-factory' }]" :content="false" class="w-full sm:w-auto" />

        <template v-if="view === 'returns'">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <p class="text-sm text-muted">Median return of the stocks in each sector (trading over ₹1 Cr a day), and the share of them in an uptrend.</p>
          <div class="flex items-center gap-2">
            <UTabs v-model="period" :items="[{ label: '1D', value: 'ret_1d' }, { label: '1M', value: 'ret_1m' }, { label: '1Y', value: 'ret_1y' }]" size="xs" :content="false" />
            <USelect v-model="sortBy" :items="[{ label: 'Sort by return', value: 'ret' }, { label: 'Sort by size', value: 'mcap' }, { label: 'Sort by uptrend share', value: 'uptrend' }]" size="xs" class="w-44" />
          </div>
        </div>

        <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <NuxtLink v-for="r in rows" :key="r.sector" :to="{ path: '/screener', query: { sector: r.sector } }">
            <UCard class="h-full transition hover:ring-primary/40 hover:shadow-md" :ui="{ body: 'p-4 sm:p-4 space-y-3' }">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="truncate font-medium text-highlighted">{{ r.sector }}</div>
                  <div class="text-xs text-muted">{{ r.n }} companies · {{ crore(r.mcap) }} · {{ pct(r.mcap / totalMcap, 1) }} of market</div>
                </div>
                <Change :value="r[period]" pill class="text-sm" />
              </div>
              <div class="grid grid-cols-3 gap-2 text-xs">
                <div><div class="text-dimmed">1D</div><Change :value="r.ret_1d" /></div>
                <div><div class="text-dimmed">1M</div><Change :value="r.ret_1m" /></div>
                <div><div class="text-dimmed">1Y</div><Change :value="r.ret_1y" /></div>
              </div>
              <div>
                <div class="mb-1 flex justify-between text-xs"><span class="text-muted">In an uptrend (stage 2)</span><span class="num font-medium">{{ pct(r.share_uptrend, 0) }}</span></div>
                <UProgress :model-value="(r.share_uptrend || 0) * 100" size="xs" :color="(r.share_uptrend || 0) >= 0.4 ? 'success' : (r.share_uptrend || 0) >= 0.25 ? 'warning' : 'error'" />
              </div>
            </UCard>
          </NuxtLink>
        </div>

        <UCard>
          <template #header><h3 class="font-semibold text-highlighted">Median return by sector · {{ period.replace('ret_', '').toUpperCase() }}</h3></template>
          <DivergingBars v-if="bars.length" :items="bars" />
        </UCard>
        </template>

        <template v-else>
          <UAlert icon="i-lucide-info" color="neutral" variant="subtle"
            title="Is each sector adding capacity faster than its sales are growing?"
            description="For each sector, the median company's growth in gross block (plant and equipment) against its sales growth. When capacity runs ahead of demand, prices and margins usually come under pressure; when it is held back, they tend to firm up. This is one of the five parts of the watchlist score; it does not rank sectors. Financial services, IT and realty get no phase (no plant to measure) but keep the growth label." />
          <UCard v-if="scatter.length">
            <template #header>
              <h3 class="font-semibold text-highlighted">Capacity growth against sales growth</h3>
              <p class="text-xs text-muted">Above the dashed line, capacity is growing faster than sales. Sectors in balance are unlabelled: hover a dot for its name.</p>
            </template>
            <CapacityScatter :items="scatter" />
          </UCard>
          <div v-if="!cycle" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3"><USkeleton v-for="i in 6" :key="i" class="h-40" /></div>
          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <UCard v-for="r in cycleRows" :key="r.sector_code" :ui="{ body: 'p-4 sm:p-4 space-y-3' }">
              <div class="flex items-start justify-between gap-2">
                <div class="min-w-0">
                  <div class="truncate font-medium text-highlighted">{{ r.sector_name || r.sector_code }}</div>
                  <div class="text-xs text-muted">{{ r.n_companies_with_demand_data }} of {{ r.n_companies_in_l1 }} companies with data</div>
                </div>
                <UTooltip v-if="PHASE[r.phase]" :text="PHASE[r.phase].tip">
                  <UBadge :label="PHASE[r.phase].label" :color="PHASE[r.phase].color" variant="subtle" size="sm" class="shrink-0" />
                </UTooltip>
              </div>
              <div class="flex flex-wrap gap-1.5">
                <UBadge v-if="GROWTH[r.growth_classification]" :label="GROWTH[r.growth_classification].label" :color="GROWTH[r.growth_classification].color" variant="outline" size="sm" />
                <UBadge v-if="r.sample_size_confidence === 'low'" label="Few companies" icon="i-lucide-triangle-alert" color="warning" variant="outline" size="sm" />
              </div>
              <div class="grid grid-cols-3 gap-2 text-xs">
                <div><div class="text-dimmed">Capacity</div><div class="num font-medium text-highlighted">{{ isNum(r.capacity_growth_pct) ? r.capacity_growth_pct.toFixed(1) + '%' : '—' }}</div></div>
                <div><div class="text-dimmed">Sales</div><div class="num font-medium text-highlighted">{{ isNum(r.demand_growth_pct) ? r.demand_growth_pct.toFixed(1) + '%' : '—' }}</div></div>
                <div><div class="text-dimmed">Gap</div><div class="num font-medium text-highlighted">{{ isNum(r.capacity_minus_demand_pts) ? (r.capacity_minus_demand_pts > 0 ? '+' : '') + r.capacity_minus_demand_pts.toFixed(1) + ' pts' : '—' }}</div></div>
              </div>
              <div v-if="r.watched_companies?.length" class="border-t border-default pt-2">
                <div class="mb-1 text-xs text-dimmed">On the watchlist ({{ r.watched_companies.length }})</div>
                <div class="space-y-1.5">
                  <NuxtLink v-for="w in (shown[r.sector_code] ? r.watched_companies : r.watched_companies.slice(0, 3))" :key="w.company_master_id"
                    :to="`/watchlist/${encodeURIComponent(w.company_master_id)}`" class="block rounded-md p-1.5 -mx-1.5 hover:bg-elevated">
                    <div class="flex items-center gap-1.5 text-sm"><span class="font-medium text-highlighted">{{ w.company_master_id.replace(/^nse:/, '') }}</span><span class="text-xs text-dimmed">{{ w.alert_count }} {{ w.alert_count === 1 ? 'event' : 'events' }}</span></div>
                    <p class="line-clamp-2 text-xs text-muted">{{ w.narrative_snippet }}</p>
                  </NuxtLink>
                </div>
                <UButton v-if="r.watched_companies.length > 3" size="xs" variant="link" color="neutral" class="px-0"
                  :label="shown[r.sector_code] ? 'Show fewer' : `Show all ${r.watched_companies.length}`" @click="shown[r.sector_code] = !shown[r.sector_code]" />
              </div>
              <p v-else class="border-t border-default pt-2 text-xs text-dimmed">No watchlist companies in this sector.</p>
            </UCard>
          </div>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
