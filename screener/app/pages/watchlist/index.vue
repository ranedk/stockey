<script setup lang="ts">
import type { WatchlistItem, WatchlistReturnSummary, WatchlistStatus } from '~/types/watchlist'

useHead({ title: 'Watchlist' })

const STATUS_TABS: { value: WatchlistStatus | 'all', label: string }[] = [
  ...(Object.keys(WL_STATUS) as WatchlistStatus[]).map(k => ({ value: k, label: WL_STATUS[k].label })),
  { value: 'all', label: 'All' },
]
const selected = ref<WatchlistStatus | 'all'>('active')
const minScore = ref(0)
const q = ref('')
const sortBy = ref<'score' | 'ret' | 'recent' | 'events'>('score')
const page = ref(1)
const PAGE = 25

const { data, status, error } = useStockey<WatchlistItem[]>(() => `/api/watchlist?status=${selected.value}`)
// Equal-weight average return over this tab: a directional gut-check, computed server-side so the
// exclusion rules (no fresh price yet) live in one place.
const { data: summary } = useStockey<WatchlistReturnSummary>(() => `/api/watchlist/summary?status=${selected.value}`)

const ret = (i: WatchlistItem) => (i.no_fresh_price_yet ? null : wlChange(i.first_seen_price, i.current_price))
const filtered = computed(() => {
  const term = q.value.trim().toLowerCase()
  let rows = (data.value ?? []).filter(i => (minScore.value === 0 || (i.story_score ?? -1) >= minScore.value)
    && (!term || i.company_master_id.toLowerCase().includes(term) || (i.company_name ?? '').toLowerCase().includes(term)))
  const by: Record<string, (i: WatchlistItem) => number | string> = {
    score: i => i.story_score ?? -1, ret: i => ret(i) ?? -1e9, recent: i => i.first_seen_at ?? '', events: i => i.alert_count,
  }
  if (sortBy.value !== 'score') rows = [...rows].sort((a, b) => (by[sortBy.value]!(b) > by[sortBy.value]!(a) ? 1 : -1))
  return rows
})
watch([selected, minScore, q, sortBy], () => { page.value = 1 })
const pageRows = computed(() => filtered.value.slice((page.value - 1) * PAGE, page.value * PAGE))

const stats = computed(() => {
  let up = 0, down = 0, held = 0
  for (const i of filtered.value) {
    const r = ret(i)
    if (isNum(r)) r >= 0 ? up++ : down++
    if (i.checks?.in_portfolio) held++
  }
  return { up, down, held }
})

// Distribution of return since added, in 10-point buckets clipped at ±50%.
const { t, base, axis, tooltip, resolve } = useChartTheme()
const hist = computed(() => {
  const edges = [-0.5, -0.4, -0.3, -0.2, -0.1, 0, 0.1, 0.2, 0.3, 0.4, 0.5]
  const counts = new Array(edges.length).fill(0)
  for (const i of filtered.value) {
    const r = ret(i)
    if (!isNum(r)) continue
    const k = Math.floor((Math.max(-0.5, Math.min(0.4999, r)) + 0.5) / 0.1)
    counts[k]++
  }
  const sg = (x: number) => (x > 0 ? `+${x}` : x < 0 ? `−${-x}` : '0')
  const labels = edges.slice(0, -1).map((e, k) => {
    const lo = Math.round(e * 100), hi = lo + 10
    return k === 0 ? '−40% or less' : k === 9 ? '+40% or more' : `${sg(lo)} to ${sg(hi)}%`
  })
  return { labels, counts: counts.slice(0, 10) }
})
const histOption = computed(() => ({
  ...base.value,
  grid: { left: 4, right: 8, top: 16, bottom: 4, containLabel: true },
  tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: number) => `${v} companies` },
  xAxis: { type: 'category', data: hist.value.labels, ...axis.value, splitLine: { show: false }, axisLabel: { ...axis.value.axisLabel, fontSize: 10, hideOverlap: true } },
  yAxis: { type: 'value', ...axis.value, axisLine: { show: false }, minInterval: 1, splitNumber: 3 },
  series: [{ type: 'bar', barMaxWidth: 24, data: hist.value.counts.map((v, k) => ({ value: v, itemStyle: { color: resolve(k < 5 ? 'var(--down)' : 'var(--up)'), borderRadius: [4, 4, 0, 0] } })),
    label: { show: true, position: 'top', color: t.value['chart-text'], fontSize: 10, formatter: (p: any) => (p.value ? p.value : '') } }],
}))

const howOpen = ref(false)
</script>

<template>
  <UDashboardPanel id="watchlist">
    <template #header>
      <PageNavbar title="Watchlist" icon="i-lucide-list-checks" />
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-4">
        <div class="space-y-2">
          <p class="max-w-4xl text-sm text-muted">
            Companies with a strong fundamental story (story score 80+, no deal-breaker flaw), or a recent positive event while
            their score is above the median. Added and removed automatically, strongest story first. A company leaves the default
            view when a flaw appears, the story fades below 65, or (for event-only names) on a timeout, a contradicting event or a
            large price move. Nothing is deleted; other tabs show where it went.
          </p>
          <UButton size="xs" variant="link" color="neutral" class="px-0" icon="i-lucide-info" :label="howOpen ? 'Hide how this list is built' : 'How this list is built'" @click="howOpen = !howOpen" />
          <div v-if="howOpen" class="max-w-4xl space-y-2 rounded-lg border border-default bg-elevated/40 p-3 text-xs leading-relaxed text-muted">
            <p><strong class="text-highlighted">Company state.</strong> For every company in the universe we keep a running picture: debt trend, capital work in progress, promoter and institutional stake, pledged shares, valuation against its own sector. It answers one question in advance: <em>if something happens to this company, does it matter?</em></p>
            <p><strong class="text-highlighted">Event rule.</strong> An alert fires when an event lines up with, or flatly contradicts, what that state already suggested; never on an event alone. A rating action or an insider trade is not interesting by itself. Names arrive automatically and leave on a timeout or a contradicting signal.</p>
          </div>
        </div>

        <!-- KPIs -->
        <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <UCard :ui="{ body: 'p-3 sm:p-4' }">
            <div class="text-xs text-muted">Companies in this view</div>
            <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ data ? num(filtered.length) : '—' }}</div>
            <div class="text-[11px] text-dimmed">{{ STATUS_TABS.find(s => s.value === selected)?.label }}<template v-if="minScore"> · score {{ minScore }}+</template></div>
          </UCard>
          <UCard :ui="{ body: 'p-3 sm:p-4' }">
            <UTooltip text="Average of each company's price change since it was added, every company weighted the same. Directional only: not time-weighted, and a company drops out once it leaves this tab.">
              <div class="text-xs text-muted underline decoration-dotted underline-offset-2">Average return since added</div>
            </UTooltip>
            <div class="mt-1 text-2xl"><Change v-if="summary && summary.included_count > 0" :value="wlFrac(summary.net_return_pct)" :digits="1" /><span v-else class="text-dimmed">—</span></div>
            <div v-if="summary" class="text-[11px] text-dimmed">{{ summary.included_count }} of {{ summary.total_count }} priced<template v-if="summary.excluded_count"> · {{ summary.excluded_count }} no fresh price yet</template></div>
          </UCard>
          <UCard :ui="{ body: 'p-3 sm:p-4' }">
            <div class="text-xs text-muted">Up / down since added</div>
            <div class="num mt-1 text-xl font-semibold sm:text-2xl"><span class="text-up">▲ {{ stats.up }}</span><span class="text-dimmed"> / </span><span class="text-down">▼ {{ stats.down }}</span></div>
            <div class="text-[11px] text-dimmed">companies with a fresh price</div>
          </UCard>
          <UCard :ui="{ body: 'p-3 sm:p-4' }">
            <div class="text-xs text-muted">Held in the portfolio</div>
            <div class="num mt-1 text-2xl font-semibold text-highlighted">{{ stats.held }}</div>
            <div class="text-[11px] text-dimmed">of the companies in this view</div>
          </UCard>
        </div>

        <UCard v-if="filtered.length">
          <template #header>
            <h3 class="font-semibold text-highlighted">Price change since added</h3>
            <p class="text-xs text-muted">Number of companies in each band of return since they joined the watchlist</p>
          </template>
          <EChart :option="histOption" height="160px" />
        </UCard>

        <WatchlistDrafts />

        <!-- list -->
        <UCard :ui="{ body: 'p-0 sm:p-0', header: 'p-3 sm:px-4' }">
          <template #header>
            <div class="space-y-3">
              <UTabs v-model="selected" :items="STATUS_TABS" size="xs" :content="false" class="hidden md:flex" />
              <USelect v-model="selected" :items="STATUS_TABS" size="sm" class="w-full md:hidden" />
              <div class="flex flex-wrap items-center gap-2">
                <UInput v-model="q" icon="i-lucide-search" placeholder="Find a company" size="sm" :ui="{ root: 'w-full sm:w-56' }" />
                <USelect v-model="minScore" :items="[{ label: 'Any story score', value: 0 }, ...[50, 65, 80, 90].map(n => ({ label: `Story score ${n}+`, value: n }))]" size="sm" class="w-40" />
                <USelect v-model="sortBy" :items="[{ label: 'Strongest story first', value: 'score' }, { label: 'Best return first', value: 'ret' }, { label: 'Newest first', value: 'recent' }, { label: 'Most events first', value: 'events' }]" size="sm" class="w-48" />
                <UIcon v-if="status === 'pending'" name="i-lucide-loader-circle" class="size-4 animate-spin text-muted" />
              </div>
            </div>
          </template>

          <UAlert v-if="error" class="m-4" color="error" variant="subtle" icon="i-lucide-circle-alert" title="Could not reach the watchlist API" :description="error.message" />
          <div v-else-if="!data && status === 'pending'" class="space-y-3 p-4">
            <USkeleton v-for="i in 5" :key="i" class="h-24" />
          </div>
          <UEmpty v-else-if="!filtered.length" icon="i-lucide-list-x" class="py-12"
            :title="minScore ? `Nothing scoring ${minScore}+` : q ? 'No company matches' : selected === 'all' ? 'Nothing on the watchlist yet' : `Nothing ${WL_STATUS[selected as WatchlistStatus]?.label.toLowerCase()}`"
            :description="selected !== 'all' && selected !== 'active' ? WL_STATUS[selected as WatchlistStatus]?.desc : undefined" />

          <ul v-else class="divide-y divide-default" :class="status === 'pending' ? 'opacity-60' : ''">
            <li v-for="i in pageRows" :key="i.company_master_id">
              <NuxtLink :to="`/watchlist/${encodeURIComponent(i.company_master_id)}`" class="group flex flex-col gap-3 px-4 py-3.5 transition-colors hover:bg-elevated/40 md:flex-row md:items-start md:justify-between">
                <div class="min-w-0 flex-1 space-y-1.5">
                  <div class="flex flex-wrap items-baseline gap-x-2">
                    <span class="font-mono font-semibold text-highlighted group-hover:text-primary">{{ wlTicker(i.company_master_id) }}</span>
                    <span class="truncate text-sm text-muted">{{ i.company_name }}</span>
                  </div>
                  <div class="flex flex-wrap items-center gap-1">
                    <UTooltip v-if="i.story_score !== null" :text="`Story score ${i.story_score.toFixed(1)} of 100: how strong the fundamental case is${wlDimension(i.primary_dimension) ? `; strongest on ${wlDimension(i.primary_dimension)!.toLowerCase()}` : ''}. 80+ qualifies.`">
                      <UBadge :color="i.story_score >= 80 ? 'success' : 'neutral'" variant="subtle" size="sm" icon="i-lucide-book-open" :label="`Story ${Math.round(i.story_score)}${wlDimension(i.primary_dimension) ? ' · ' + wlDimension(i.primary_dimension) : ''}`" />
                    </UTooltip>
                    <UTooltip v-if="i.entry_basis && WL_ENTRY_BASIS[i.entry_basis]" :text="WL_ENTRY_BASIS[i.entry_basis]!.desc">
                      <UBadge color="neutral" variant="outline" size="sm" :label="`Via ${WL_ENTRY_BASIS[i.entry_basis]!.label.toLowerCase()}`" />
                    </UTooltip>
                    <UBadge color="neutral" variant="outline" size="sm" icon="i-lucide-bell" :label="`${i.alert_count} event${i.alert_count === 1 ? '' : 's'}`" />
                    <UTooltip v-if="i.status !== 'active'" :text="WL_STATUS[i.status].desc">
                      <UBadge :color="WL_STATUS[i.status].color" variant="soft" size="sm" :label="WL_STATUS[i.status].label" />
                    </UTooltip>
                    <WatchlistConfluenceBadge v-if="i.confluence_count !== null" :supportive="i.confluence_count" :contradicting="i.contradicting_count" :evaluable="i.evaluable_count" />
                  </div>
                  <WatchlistChecks :checks="i.checks" :flaws="i.flaws" compact />
                  <div v-if="i.strategies.length" class="flex flex-wrap gap-1">
                    <UBadge v-for="s in i.strategies" :key="s" :color="wlEventColor(s)" variant="subtle" size="xs" :label="wlEventLabel(s)" />
                  </div>
                  <p v-if="i.status_reason" class="text-xs text-muted"><UIcon name="i-lucide-info" class="mr-1 inline size-3 align-[-1px]" />{{ i.status_reason }}</p>
                  <p v-if="i.narrative_text" class="line-clamp-2 max-w-3xl text-sm text-default">{{ i.narrative_text }}</p>
                  <p v-else class="text-sm italic text-dimmed">Summary not written yet.</p>
                </div>
                <div class="flex shrink-0 flex-wrap items-center gap-x-4 gap-y-1 md:w-56 md:flex-col md:items-end md:gap-1 md:text-right">
                  <div class="flex items-center gap-2">
                    <span class="num text-sm text-muted">{{ inr(i.first_seen_price) }} → <span class="text-highlighted">{{ inr(i.current_price) }}</span></span>
                  </div>
                  <div class="flex items-center gap-1.5">
                    <UBadge v-if="i.no_fresh_price_yet" color="neutral" variant="outline" size="sm" label="No fresh price yet" />
                    <Change v-else :value="ret(i)" pill class="text-sm" />
                    <UTooltip v-if="i.price_data_stale" text="The latest price is old: no new trading data for a while">
                      <UBadge color="warning" variant="soft" size="sm" label="Stale price" />
                    </UTooltip>
                  </div>
                  <div class="text-xs text-dimmed">added {{ dateShort(i.first_seen_at) }}</div>
                  <div v-if="i.suggested_watch_until" class="text-xs text-dimmed">watch until {{ dateShort(i.suggested_watch_until) }}</div>
                </div>
              </NuxtLink>
            </li>
          </ul>

          <template v-if="filtered.length > PAGE" #footer>
            <div class="flex flex-wrap items-center justify-between gap-2">
              <span class="text-xs text-muted">{{ (page - 1) * PAGE + 1 }}–{{ Math.min(page * PAGE, filtered.length) }} of {{ filtered.length }}</span>
              <UPagination v-model:page="page" :total="filtered.length" :items-per-page="PAGE" size="xs" />
            </div>
          </template>
        </UCard>
      </div>
    </template>
  </UDashboardPanel>
</template>
