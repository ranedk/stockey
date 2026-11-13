<script setup lang="ts">
useHead({ title: 'Markets' })
const { data, status } = useApi<any>('market/overview')

const periods = [
  { label: '1D', value: '1d' }, { label: '1W', value: '1w' }, { label: '1M', value: '1m' },
  { label: '3M', value: '3m' }, { label: '1Y', value: '1y' },
]
const sectorPeriod = ref('1m')
const sectorBars = computed(() => (data.value?.sectors || []).map((s: any) => ({ label: s.name, value: s[`chg_${sectorPeriod.value}`] })))

const mapPeriod = ref('1m')
const mapItems = computed(() => (data.value?.industries || []).map((s: any) => ({
  name: s.sector, mcap: s.mcap, n: s.n, value: s[`ret_${mapPeriod.value}`],
})))
const mapCap = computed(() => ({ '1d': 0.03, '1m': 0.1, '1y': 0.4 } as Record<string, number>)[mapPeriod.value])

const b = computed(() => data.value?.breadth)
const breadthParts = computed(() => b.value ? [
  { label: 'Advancing', value: b.value.adv, color: 'var(--up)' },
  { label: 'Unchanged', value: b.value.unch, color: 'var(--chart-grid)' },
  { label: 'Declining', value: b.value.dec, color: 'var(--down)' },
] : [])
const STAGE_COLORS: Record<number, string> = { 1: 'var(--series-1)', 2: 'var(--up)', 3: 'var(--series-4)', 4: 'var(--down)' }
const stageParts = computed(() => (data.value?.stages || []).map((s: any) => ({
  label: STAGES[s.stage]?.short ?? `Stage ${s.stage}`, value: s.n, color: STAGE_COLORS[s.stage] ?? 'gray',
})))
const above200 = computed(() => b.value?.with_200 ? b.value.above_200 / b.value.with_200 : null)
const uptrendShare = computed(() => {
  const all = (data.value?.stages || []).reduce((s: number, x: any) => s + x.n, 0)
  const s2 = (data.value?.stages || []).find((x: any) => x.stage === 2)?.n ?? 0
  return all ? s2 / all : null
})
const mood = computed(() => {
  const a = above200.value
  if (a == null) return null
  if (a >= 0.6) return { label: 'Broad strength', color: 'success' as const, icon: 'i-lucide-sun' }
  if (a >= 0.4) return { label: 'Mixed', color: 'warning' as const, icon: 'i-lucide-cloud-sun' }
  return { label: 'Broad weakness', color: 'error' as const, icon: 'i-lucide-cloud-rain' }
})

const goSector = (name: string) => navigateTo({ path: '/screener', query: { sector: name } })
</script>

<template>
  <UDashboardPanel id="markets">
    <template #header>
      <PageNavbar title="Markets" icon="i-lucide-layout-dashboard">
        <template #right>
          <span v-if="b?.as_of" class="hidden sm:inline text-xs text-muted">Close of {{ dateShort(b.as_of) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <!-- indices -->
        <div class="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <template v-if="status === 'pending'">
            <USkeleton v-for="i in 6" :key="i" class="h-28 rounded-xl" />
          </template>
          <UCard v-for="ix in data?.indices" v-else :key="ix.name" :ui="{ body: 'p-3 sm:p-4' }" class="overflow-hidden">
            <div class="truncate text-xs font-medium text-muted">{{ ix.name }}</div>
            <div class="num mt-1 text-lg font-semibold text-highlighted">{{ num(ix.close, ix.close < 100 ? 2 : 0) }}</div>
            <div class="mt-0.5 flex items-center gap-2 text-xs">
              <Change :value="ix.chg_1d" :digits="2" pill />
              <span class="text-dimmed">1Y <Change :value="ix.chg_1y" /></span>
            </div>
            <Sparkline :data="ix.spark" :width="160" :height="30" class="mt-2 w-full" />
          </UCard>
        </div>

        <!-- breadth + mood -->
        <div class="grid items-start gap-4 lg:grid-cols-3">
          <UCard class="lg:col-span-2">
            <template #header>
              <div class="flex items-center justify-between gap-2">
                <div>
                  <h3 class="font-semibold text-highlighted">Market breadth</h3>
                  <p class="text-xs text-muted">Stocks trading over ₹1 Cr a day</p>
                </div>
                <UBadge v-if="mood" :color="mood.color" variant="subtle" :icon="mood.icon" :label="mood.label" />
              </div>
            </template>
            <div v-if="b" class="space-y-6">
              <div>
                <div class="mb-2 flex items-baseline justify-between text-sm">
                  <span class="font-medium">Today</span>
                  <span class="num text-xs text-muted">{{ b.adv }} up · {{ b.dec }} down</span>
                </div>
                <SegmentedBar :parts="breadthParts" :height="12" />
              </div>
              <div>
                <div class="mb-2 flex items-baseline justify-between text-sm">
                  <span class="font-medium">Trend stage of each stock</span>
                  <span class="text-xs text-muted">{{ pct(uptrendShare, 0) }} in an uptrend</span>
                </div>
                <SegmentedBar :parts="stageParts" :height="12" />
              </div>
              <div class="grid grid-cols-3 gap-3">
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Above 200-day average</div>
                  <div class="num mt-1 text-xl font-semibold text-highlighted">{{ pct(above200, 0) }}</div>
                  <UProgress :model-value="(above200 ?? 0) * 100" size="xs" class="mt-2" :color="(above200 ?? 0) >= 0.5 ? 'success' : 'error'" />
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Near 52-week high</div>
                  <div class="num mt-1 text-xl font-semibold text-up">{{ b.near_high }}</div>
                  <div class="mt-1 text-[11px] text-dimmed">within 2%</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Near 52-week low</div>
                  <div class="num mt-1 text-xl font-semibold text-down">{{ b.near_low }}</div>
                  <div class="mt-1 text-[11px] text-dimmed">within 2%</div>
                </div>
              </div>
            </div>
            <USkeleton v-else class="h-48" />
          </UCard>

          <UCard>
            <template #header>
              <div class="flex items-center justify-between gap-2">
                <h3 class="font-semibold text-highlighted">Sector indices</h3>
                <UTabs v-model="sectorPeriod" :items="periods" size="xs" :content="false" />
              </div>
            </template>
            <DivergingBars v-if="sectorBars.length" :items="sectorBars" />
            <USkeleton v-else class="h-96" />
          </UCard>
        </div>

        <!-- sector map -->
        <UCard>
          <template #header>
            <div class="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 class="font-semibold text-highlighted">Sector map</h3>
                <p class="text-xs text-muted">Size = market cap · colour = median stock return · click a sector to screen it</p>
              </div>
              <UTabs v-model="mapPeriod" :items="periods.filter(p => ['1d','1m','1y'].includes(p.value))" size="xs" :content="false" />
            </div>
          </template>
          <SectorMap v-if="mapItems.length" :items="mapItems" :metric="mapPeriod.toUpperCase()" :cap="mapCap" @pick="goSector" />
          <USkeleton v-else class="h-[360px]" />
        </UCard>

        <!-- movers -->
        <div class="grid gap-4 lg:grid-cols-2">
          <UCard v-for="side in (['gainers', 'losers'] as const)" :key="side" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex items-center gap-2">
                <UIcon :name="side === 'gainers' ? 'i-lucide-trending-up' : 'i-lucide-trending-down'" :class="side === 'gainers' ? 'text-up' : 'text-down'" />
                <h3 class="font-semibold text-highlighted">Top {{ side }} today</h3>
                <span class="text-xs text-muted">· trading over ₹5 Cr a day</span>
              </div>
            </template>
            <ul class="divide-y divide-default">
              <li v-for="r in data?.[side]" :key="r.symbol">
                <NuxtLink :to="`/company/${encodeURIComponent(r.symbol)}`" class="flex items-center gap-3 px-4 py-2.5 hover:bg-elevated/50 transition-colors">
                  <div class="min-w-0 flex-1">
                    <div class="truncate text-sm font-medium text-highlighted">{{ r.name }}</div>
                    <div class="truncate text-xs text-muted">{{ r.symbol }} · {{ r.industry || '—' }}</div>
                  </div>
                  <Sparkline :data="r.spark" :width="72" :height="24" class="hidden sm:block" />
                  <div class="w-24 text-right">
                    <div class="num text-sm">{{ inr(r.price) }}</div>
                    <Change :value="r.ret_1d" pill class="text-xs" />
                  </div>
                </NuxtLink>
              </li>
            </ul>
          </UCard>
        </div>
      </div>
    </template>
  </UDashboardPanel>
</template>
