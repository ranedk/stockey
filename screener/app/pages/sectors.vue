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
</script>

<template>
  <UDashboardPanel id="sectors">
    <template #header>
      <PageNavbar title="Sectors" icon="i-lucide-blocks" />
    </template>
    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
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
      </div>
    </template>
  </UDashboardPanel>
</template>
