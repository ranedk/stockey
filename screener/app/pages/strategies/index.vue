<script setup lang="ts">
import type { StratSummary } from '~/types/paperTracks'
useHead({ title: 'Strategies' })

const { data, status, error } = useStockey<StratSummary[]>('/api/strategies')

const tone = ref<'all' | 'good' | 'bad' | 'neutral'>('all')
const sortBy = ref<'count' | 'recent' | 'name'>('count')
const rows = computed(() => {
  const r = (data.value ?? []).filter(s => tone.value === 'all' || stratInfo(s.trigger_type).tone === tone.value)
  if (sortBy.value === 'count') r.sort((a, b) => b.company_count - a.company_count)
  else if (sortBy.value === 'recent') r.sort((a, b) => (b.last_alert_date ?? '').localeCompare(a.last_alert_date ?? ''))
  else r.sort((a, b) => stratInfo(a.trigger_type).label.localeCompare(stratInfo(b.trigger_type).label))
  return r
})
const max = computed(() => Math.max(1, ...(data.value ?? []).map(s => s.company_count)))
const total = computed(() => (data.value ?? []).reduce((s, x) => s + x.company_count, 0))
const byTone = (t: string) => (data.value ?? []).filter(s => stratInfo(s.trigger_type).tone === t)
const latest = computed(() => (data.value ?? []).map(s => s.last_alert_date ?? '').sort().at(-1))
const daysAgo = (d: string | null) => {
  if (!d) return null
  const n = Math.round((Date.now() - new Date(d).getTime()) / 864e5)
  return n <= 0 ? 'today' : n === 1 ? 'yesterday' : `${n} days ago`
}
const toneItems = [
  { label: 'All', value: 'all' }, { label: 'Positive', value: 'good' }, { label: 'Warning', value: 'bad' }, { label: 'Neutral', value: 'neutral' },
]
</script>

<template>
  <UDashboardPanel id="strategies">
    <template #header>
      <PageNavbar title="Strategies" icon="i-lucide-flask-conical">
        <template #right>
          <span v-if="latest" class="hidden text-xs text-muted sm:inline">Latest alert {{ dateShort(latest) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <div class="flex flex-wrap items-start gap-3 rounded-xl bg-elevated/50 p-4 ring ring-default">
          <UIcon name="i-lucide-info" class="mt-0.5 size-5 shrink-0 text-primary" />
          <div class="min-w-0 flex-1 text-sm">
            <p class="font-medium text-highlighted">Events that put a company on the watchlist.</p>
            <p class="mt-1 text-muted">
              Each strategy watches for one kind of event in filings or trading (a rating change, an insider or bulk deal,
              a promoter pledge, a results surprise) and raises an alert only when the company was already in a state
              that makes the event worth acting on. The rule never relaxes: the same event at a company that was not ready
              raises nothing. A company can be under several strategies at once. Counts are companies currently alerted,
              not past success rates, and none of this sizes or places a trade.
            </p>
          </div>
        </div>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-plug-zap" title="Could not reach the stockey API"
          :description="error.message" />

        <div v-else-if="status === 'pending'" class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          <USkeleton v-for="i in 9" :key="i" class="h-36 rounded-xl" />
        </div>

        <UEmpty v-else-if="!data?.length" icon="i-lucide-bell-off" title="No strategy has alerted yet" />

        <template v-else>
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Strategies running</div>
              <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ data.length }}</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Alerts open</div>
              <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ num(total) }}</div>
              <div class="mt-1 text-[11px] text-dimmed">a company under two strategies counts twice</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Positive-sign strategies</div>
              <div class="num mt-1 text-2xl font-semibold text-up">▲ {{ byTone('good').length }}</div>
              <div class="mt-1 text-[11px] text-dimmed">{{ num(byTone('good').reduce((s, x) => s + x.company_count, 0)) }} alerts</div>
            </UCard>
            <UCard :ui="{ body: 'p-4 sm:p-4' }">
              <div class="text-xs text-muted">Warning-sign strategies</div>
              <div class="num mt-1 text-2xl font-semibold text-down">▼ {{ byTone('bad').length }}</div>
              <div class="mt-1 text-[11px] text-dimmed">{{ num(byTone('bad').reduce((s, x) => s + x.company_count, 0)) }} alerts</div>
            </UCard>
          </div>

          <div class="flex flex-wrap items-center justify-between gap-2">
            <UTabs v-model="tone" :items="toneItems" size="xs" :content="false" />
            <USelect v-model="sortBy" size="xs" class="w-48"
              :items="[{ label: 'Most companies first', value: 'count' }, { label: 'Most recent alert first', value: 'recent' }, { label: 'A to Z', value: 'name' }]" />
          </div>

          <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
            <NuxtLink v-for="s in rows" :key="s.trigger_type" :to="`/strategies/${encodeURIComponent(s.trigger_type)}`" class="block h-full">
              <UCard class="h-full transition hover:shadow-md hover:ring-primary/40" :ui="{ body: 'p-4 sm:p-4 flex h-full flex-col gap-3' }">
                <div class="flex items-start gap-3">
                  <span class="grid size-9 shrink-0 place-items-center rounded-lg"
                    :class="stratInfo(s.trigger_type).tone === 'good' ? 'bg-up-soft text-up' : stratInfo(s.trigger_type).tone === 'bad' ? 'bg-down-soft text-down' : 'bg-elevated text-muted'">
                    <UIcon :name="stratInfo(s.trigger_type).icon" class="size-4.5" />
                  </span>
                  <div class="min-w-0 flex-1">
                    <div class="font-semibold text-highlighted">{{ stratInfo(s.trigger_type).label }}</div>
                    <UBadge :color="STRAT_TONE_BADGE[stratInfo(s.trigger_type).tone].color" variant="subtle" size="sm"
                      :label="STRAT_TONE_BADGE[stratInfo(s.trigger_type).tone].label" class="mt-1" />
                  </div>
                  <div class="text-right">
                    <div class="num text-xl font-semibold text-highlighted">{{ num(s.company_count) }}</div>
                    <div class="text-[11px] text-muted">{{ s.company_count === 1 ? 'company' : 'companies' }}</div>
                  </div>
                </div>
                <p v-if="stratInfo(s.trigger_type).blurb" class="text-xs text-muted">{{ stratInfo(s.trigger_type).blurb }}</p>
                <div class="mt-auto space-y-1.5">
                  <UProgress :model-value="(s.company_count / max) * 100" size="xs" color="neutral" />
                  <div class="flex items-center gap-1 text-[11px] text-dimmed">
                    <UIcon name="i-lucide-clock" class="size-3" />
                    Last alert {{ dateShort(s.last_alert_date) }}<template v-if="daysAgo(s.last_alert_date)"> · {{ daysAgo(s.last_alert_date) }}</template>
                  </div>
                </div>
              </UCard>
            </NuxtLink>
          </div>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
