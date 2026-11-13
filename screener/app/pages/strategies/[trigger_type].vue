<script setup lang="ts">
import type { StratDetail } from '~/types/paperTracks'

const route = useRoute()
const trigger = computed(() => decodeURIComponent(String(route.params.trigger_type)))
const info = computed(() => stratInfo(trigger.value))
useHead({ title: () => info.value.label })

const { data, status, error } = useStockey<StratDetail>(() => `/api/strategies/${encodeURIComponent(trigger.value)}`,
  { key: `strategy-${trigger.value}`, watch: [trigger] })

const q = ref('')
const sortBy = ref<'recent' | 'name' | 'price'>('recent')
const limit = ref(40)
const companies = computed(() => {
  const f = q.value.trim().toLowerCase()
  const r = (data.value?.companies ?? []).filter(c => !f
    || stratSymbol(c.company_master_id).toLowerCase().includes(f)
    || (c.company_name ?? '').toLowerCase().includes(f)
    || (c.reasoning ?? '').toLowerCase().includes(f))
  if (sortBy.value === 'recent') r.sort((a, b) => (b.alert_date ?? '').localeCompare(a.alert_date ?? ''))
  else if (sortBy.value === 'price') r.sort((a, b) => (b.current_price ?? -1) - (a.current_price ?? -1))
  else r.sort((a, b) => stratSymbol(a.company_master_id).localeCompare(stratSymbol(b.company_master_id)))
  return r
})
watch([q, sortBy], () => { limit.value = 40 })

const total = computed(() => data.value?.companies.length ?? 0)
const last30 = computed(() => {
  const cut = Date.now() - 30 * 864e5
  return (data.value?.companies ?? []).filter(c => c.alert_date && new Date(c.alert_date).getTime() >= cut).length
})
const origins = computed(() => {
  const m = new Map<string, number>()
  for (const c of data.value?.companies ?? []) m.set(c.origin || 'unknown', (m.get(c.origin || 'unknown') ?? 0) + 1)
  return [...m.entries()].sort((a, b) => b[1] - a[1])
})
// Alerts per month, oldest first: when this strategy has been firing.
const months = computed(() => {
  const m = new Map<string, number>()
  for (const c of data.value?.companies ?? []) if (c.alert_date) m.set(c.alert_date.slice(0, 7), (m.get(c.alert_date.slice(0, 7)) ?? 0) + 1)
  return [...m.entries()].sort(([a], [b]) => a.localeCompare(b)).slice(-12)
})
const { t, base, axis, tooltip } = useChartTheme()
const monthOption = computed(() => ({
  ...base.value,
  grid: { left: 4, right: 4, top: 16, bottom: 4, containLabel: true },
  tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'shadow' } },
  xAxis: { type: 'category', data: months.value.map(([m]) => new Date(`${m}-01`).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' })), ...axis.value, splitLine: { show: false } },
  yAxis: { type: 'value', ...axis.value, axisLabel: { show: false }, splitLine: { show: false } },
  series: [{ name: 'Companies alerted', type: 'bar', barMaxWidth: 24, data: months.value.map(([, n]) => n),
    itemStyle: { color: t.value['series-1'], borderRadius: [4, 4, 0, 0] },
    label: { show: true, position: 'top', color: t.value['chart-text'], fontSize: 11 } }],
}))
const ORIGIN: Record<string, string> = { rule: 'Fixed rule', llm: 'AI read' }
</script>

<template>
  <UDashboardPanel id="strategy-detail">
    <template #header>
      <PageNavbar :title="info.label" icon="i-lucide-flask-conical">
        <template #right>
          <UButton to="/strategies" variant="ghost" color="neutral" size="sm" icon="i-lucide-arrow-left" label="All strategies" class="hidden sm:flex" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <UAlert v-if="error" :color="error.statusCode === 404 ? 'neutral' : 'error'" variant="subtle" icon="i-lucide-circle-alert"
          :title="`Could not load ${info.label}`"
          :description="error.statusCode === 404 ? 'No company has ever met this strategy.' : error.message" />

        <template v-else-if="status === 'pending' && !data">
          <USkeleton class="h-28 rounded-xl" />
          <USkeleton v-for="i in 5" :key="i" class="h-20 rounded-xl" />
        </template>

        <template v-else-if="data">
          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2" :ui="{ body: 'p-4 sm:p-5' }">
              <div class="flex items-start gap-4">
                <span class="grid size-11 shrink-0 place-items-center rounded-xl"
                  :class="info.tone === 'good' ? 'bg-up-soft text-up' : info.tone === 'bad' ? 'bg-down-soft text-down' : 'bg-elevated text-muted'">
                  <UIcon :name="info.icon" class="size-5" />
                </span>
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-2">
                    <h1 class="text-xl font-semibold text-highlighted">{{ info.label }}</h1>
                    <UBadge :color="STRAT_TONE_BADGE[info.tone].color" variant="subtle" :label="STRAT_TONE_BADGE[info.tone].label" />
                  </div>
                  <p v-if="info.blurb" class="mt-1 text-sm text-muted">{{ info.blurb }}</p>
                  <p class="mt-1 font-mono text-[11px] text-dimmed">{{ trigger }}</p>
                </div>
              </div>
              <div class="mt-4 grid grid-cols-3 gap-3">
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Companies now</div>
                  <div class="num mt-1 text-xl font-semibold text-highlighted">{{ num(total) }}</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Alerted in last 30 days</div>
                  <div class="num mt-1 text-xl font-semibold text-highlighted">{{ num(last30) }}</div>
                </div>
                <div class="rounded-lg bg-elevated/60 p-3">
                  <div class="text-xs text-muted">Raised by</div>
                  <div class="mt-1 flex flex-wrap gap-1">
                    <UBadge v-for="[o, n] in origins" :key="o" color="neutral" variant="subtle" size="sm" :label="`${ORIGIN[o] ?? o} · ${n}`" />
                  </div>
                </div>
              </div>
            </UCard>
            <UCard>
              <template #header>
                <h3 class="font-semibold text-highlighted">When they were alerted</h3>
                <p class="text-xs text-muted">Companies by month of the alert, last 12 months shown</p>
              </template>
              <EChart v-if="months.length" :option="monthOption" height="150px" />
              <UEmpty v-else icon="i-lucide-calendar" title="No dates" class="py-4" />
            </UCard>
          </div>

          <UEmpty v-if="!total" icon="i-lucide-inbox" title="No company meets this strategy right now" />

          <UCard v-else :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-2">
                <h3 class="font-semibold text-highlighted">{{ num(total) }} {{ total === 1 ? 'company' : 'companies' }} meet this now</h3>
                <div class="flex flex-wrap items-center gap-2">
                  <UInput v-model="q" icon="i-lucide-search" placeholder="Search name, symbol or reason" size="sm" class="w-60" />
                  <USelect v-model="sortBy" size="sm" class="w-40"
                    :items="[{ label: 'Newest alert first', value: 'recent' }, { label: 'A to Z', value: 'name' }, { label: 'Highest price', value: 'price' }]" />
                </div>
              </div>
            </template>
            <ul class="divide-y divide-default">
              <li v-for="c in companies.slice(0, limit)" :key="c.company_master_id" class="flex gap-4 px-4 py-3 hover:bg-elevated/40">
                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <NuxtLink :to="`/company/${encodeURIComponent(stratSymbol(c.company_master_id))}`" class="font-semibold text-highlighted hover:text-primary">
                      {{ stratSymbol(c.company_master_id) }}
                    </NuxtLink>
                    <span class="truncate text-sm text-muted">{{ c.company_name }}</span>
                    <UBadge v-if="c.origin" color="neutral" variant="outline" size="sm" :label="ORIGIN[c.origin] ?? c.origin" />
                  </div>
                  <p v-if="c.reasoning" class="mt-1 max-w-3xl text-sm text-muted">{{ c.reasoning }}</p>
                  <NuxtLink :to="`/watchlist/${encodeURIComponent(c.company_master_id)}`" class="mt-1 inline-flex items-center gap-1 text-[11px] text-dimmed hover:text-primary">
                    <UIcon name="i-lucide-list-checks" class="size-3" /> Watchlist entry
                  </NuxtLink>
                </div>
                <div class="shrink-0 text-right">
                  <div class="num text-sm font-medium text-highlighted">{{ inr(c.current_price) }}</div>
                  <div class="mt-1 text-[11px] text-dimmed">alerted {{ dateShort(c.alert_date) }}</div>
                </div>
              </li>
            </ul>
            <UEmpty v-if="!companies.length" icon="i-lucide-search-x" title="Nothing matches the search" class="py-8" />
            <div v-if="companies.length > limit" class="border-t border-default p-2 text-center">
              <UButton variant="ghost" color="neutral" size="sm" :label="`Show more (${companies.length - limit} left)`" @click="limit += 60" />
            </div>
          </UCard>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
