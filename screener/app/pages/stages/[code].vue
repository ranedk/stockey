<script setup lang="ts">
// One industry in the weekly rotation view: its rank and trend, the stage mix of its members,
// and every member with its trend numbers and the fundamentals check.
const route = useRoute()
const code = computed(() => String(route.params.code))
const { data, status, error } = useSystrader<StagesIndustryResponse>(() => `/api/rotation/industry/${code.value}`)
const { bySymbol, median, filterOf } = useStagesStories()
const ind = computed(() => data.value?.industry)
useHead({ title: () => ind.value?.name || 'Industry' })

const liquidOnly = ref(true)
const members = computed(() => data.value?.stocks ?? [])
const stocks = computed(() => members.value.filter(s => !liquidOnly.value || s.eligible)
  .sort((a, b) => (b.rs26_pct ?? -1e9) - (a.rs26_pct ?? -1e9)))
const mix = computed(() => {
  const n: Record<number, number> = { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0 }
  for (const s of stocks.value) n[STAGES[s.stage] ? s.stage : 0]!++
  return [2, 1, 3, 4, 0].map(k => ({ label: k ? STAGES[k]!.short : 'No stage yet', value: n[k]!, color: STAGES_COLOR_VAR[k]! })).filter(p => p.value)
})
const candidates = computed(() => members.value.filter(s => s.candidate).length)
// chart scale = how many industries are ranked this week (from the overview snapshot)
const { data: overview } = useSystrader<StagesResponse>('/api/rotation')
const rankScale = computed(() => {
  const n = (overview.value?.industries ?? []).filter(i => i.rank > 0).length
  return Math.max(2, n, ...(ind.value?.rank_history ?? []))
})
const breadthColor = (v: number) => (v >= 50 ? 'success' : v >= 30 ? 'warning' : 'error')
</script>

<template>
  <UDashboardPanel id="stage-industry">
    <template #header>
      <PageNavbar :title="ind?.name || 'Industry'" icon="i-lucide-chart-no-axes-combined">
        <template #right>
          <UButton to="/stages" icon="i-lucide-arrow-left" label="All industries" color="neutral" variant="ghost" size="sm" class="hidden sm:flex" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-cloud-off" title="Could not load this industry" :description="error.message">
          <template #actions><UButton to="/stages" label="Back to all industries" color="neutral" variant="outline" size="xs" /></template>
        </UAlert>
        <template v-else-if="!ind && status === 'pending'">
          <USkeleton class="h-32 rounded-xl" />
          <USkeleton class="h-96 rounded-xl" />
        </template>

        <template v-else-if="ind">
          <div class="space-y-2">
            <NuxtLink to="/stages" class="inline-flex items-center gap-1 text-xs text-muted hover:text-primary sm:hidden"><UIcon name="i-lucide-arrow-left" />All industries</NuxtLink>
            <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
              <h1 class="text-2xl font-semibold tracking-tight text-highlighted sm:text-3xl">{{ ind.name || ind.code }}</h1>
              <UTooltip v-if="ind.leading" :text="STAGES_HELP.leading">
                <UBadge :label="`Leading for ${ind.weeks_leading} wk`" icon="i-lucide-trophy" color="success" variant="subtle" />
              </UTooltip>
              <StageBadge :stage="ind.stage" />
            </div>
            <div class="text-xs text-muted">{{ ind.sector_name }} · week ending {{ dateShort(data?.as_of) }}</div>
          </div>

          <!-- key numbers -->
          <div class="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-default bg-[var(--ui-border)] sm:grid-cols-3 lg:grid-cols-5">
            <div class="bg-default p-3">
              <UTooltip :text="STAGES_HELP.rank"><div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">Rank</div></UTooltip>
              <div class="num mt-1 text-xl font-semibold text-highlighted">{{ ind.rank || 'Not ranked' }}</div>
              <div class="mt-0.5 text-[11px] text-dimmed">1 = strongest</div>
            </div>
            <div class="bg-default p-3">
              <div class="text-xs text-muted">Rank change</div>
              <div class="mt-1 flex items-center gap-3 text-sm">
                <span><span class="text-[11px] text-dimmed">4 wk </span><StagesMove :now="ind.rank" :before="ind.rank_4w" /></span>
                <span><span class="text-[11px] text-dimmed">13 wk </span><StagesMove :now="ind.rank" :before="ind.rank_13w" /></span>
              </div>
              <div class="mt-0.5 text-[11px] text-dimmed">was {{ ind.rank_4w || '—' }} and {{ ind.rank_13w || '—' }}</div>
            </div>
            <div class="bg-default p-3">
              <UTooltip :text="STAGES_HELP.rs26"><div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">vs market, 26 wk</div></UTooltip>
              <div class="mt-1 text-xl"><Change :value="themesFrac(ind.rs26_pct)" /></div>
            </div>
            <div class="bg-default p-3">
              <div class="text-xs text-muted">Liquid members in an uptrend</div>
              <div class="num mt-1 text-xl font-semibold text-highlighted">{{ Math.round(ind.stage2_pct) }}%</div>
              <UProgress :model-value="ind.stage2_pct" size="2xs" class="mt-1.5" :color="breadthColor(ind.stage2_pct)" />
            </div>
            <div class="col-span-2 bg-default p-3 sm:col-span-1">
              <UTooltip :text="STAGES_HELP.liquid"><div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">Members</div></UTooltip>
              <div class="num mt-1 text-xl font-semibold text-highlighted">{{ ind.eligible_members }} <span class="text-sm font-normal text-muted">liquid of {{ ind.members }}</span></div>
              <div class="mt-0.5 text-[11px] text-dimmed">{{ candidates }} {{ candidates === 1 ? 'candidate' : 'candidates' }}</div>
            </div>
          </div>

          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2">
              <template #header>
                <h3 class="font-semibold text-highlighted">Rank over the last {{ ind.rank_history.length }} weeks</h3>
                <p class="text-xs text-muted">Higher on the chart = stronger against the market · gaps = not ranked that week</p>
              </template>
              <StagesRankChart v-if="ind.rank_history.some(r => r > 0)" :history="ind.rank_history" :ranked="rankScale" />
              <UEmpty v-else icon="i-lucide-chart-line" title="Never ranked in this window" description="Fewer than 5 liquid members." class="py-8" />
            </UCard>
            <UCard>
              <template #header>
                <h3 class="font-semibold text-highlighted">Trend stage of each member</h3>
                <p class="text-xs text-muted">{{ liquidOnly ? 'Liquid members (₹10 Cr+ a day)' : 'All members' }} · {{ stocks.length }} stocks</p>
              </template>
              <SegmentedBar v-if="mix.length" :parts="mix" :height="14" />
              <p v-else class="text-sm text-muted">No members to show.</p>
              <ul class="mt-4 space-y-2 text-xs text-muted">
                <li v-for="k in [2, 1, 3, 4]" :key="k" class="flex gap-2"><StageBadge :stage="k" size="xs" short class="shrink-0" /><span>{{ STAGES[k]!.desc }}</span></li>
              </ul>
            </UCard>
          </div>

          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 class="font-semibold text-highlighted">Members <span class="num text-muted">· {{ stocks.length }}</span></h3>
                  <p class="text-xs text-muted">Strongest against the market first · highlighted rows are candidates (liquid, in an uptrend, leading inside a leading industry)</p>
                </div>
                <USwitch v-model="liquidOnly" label="Liquid only (₹10 Cr+ a day)" size="sm" />
              </div>
              <p class="mt-2 text-xs text-muted">Fundamentals check: no flaw and a story score at or above the median ({{ isNum(median) ? Math.round(median) : '—' }}); tested forward only.</p>
            </template>
            <div class="overflow-x-auto">
              <table class="w-full min-w-[980px] text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="px-4 py-2.5 text-left font-medium">Stock</th>
                    <th class="px-3 py-2.5 text-left font-medium">Stage</th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.rs26"><span class="cursor-help underline decoration-dotted underline-offset-2">vs market, 26 wk</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium">Weeks in uptrend</th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.aboveMa"><span class="cursor-help underline decoration-dotted underline-offset-2">Above 30-wk avg</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.slope"><span class="cursor-help underline decoration-dotted underline-offset-2">Average, 4-wk change</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.volume"><span class="cursor-help underline decoration-dotted underline-offset-2">Volume vs usual</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium">Close</th>
                    <th class="px-4 py-2.5 text-left font-medium"><UTooltip :text="STAGES_HELP.filter"><span class="cursor-help underline decoration-dotted underline-offset-2">Fundamentals</span></UTooltip></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="s in stocks" :key="s.symbol" class="group border-b border-default/60 last:border-0 hover:bg-elevated/40" :class="s.candidate ? 'bg-success/5' : ''">
                    <td class="px-4 py-2">
                      <div class="flex items-center gap-2">
                        <NuxtLink :to="`/company/${encodeURIComponent(s.symbol)}`" class="font-mono font-medium text-highlighted group-hover:text-primary">{{ s.symbol }}</NuxtLink>
                        <UBadge v-if="s.candidate" label="Candidate" color="success" variant="subtle" size="xs" />
                        <UTooltip v-if="!s.eligible" :text="STAGES_HELP.liquid"><UBadge label="Thinly traded" color="neutral" variant="outline" size="xs" /></UTooltip>
                      </div>
                    </td>
                    <td class="px-3 py-2"><StageBadge :stage="s.stage" size="xs" short /></td>
                    <td class="px-3 py-2 text-right"><Change :value="themesFrac(s.rs26_pct)" /></td>
                    <td class="num px-3 py-2 text-right" :class="s.weeks_in_stage2 ? '' : 'text-dimmed'">{{ s.weeks_in_stage2 || '—' }}</td>
                    <td class="px-3 py-2 text-right"><Change :value="themesFrac(s.above_ma30_pct)" /></td>
                    <td class="px-3 py-2 text-right"><Change :value="themesFrac(s.slope_pct)" /></td>
                    <td class="num px-3 py-2 text-right" :class="isNum(s.volume_ratio) && s.volume_ratio >= 1.5 ? 'font-medium text-highlighted' : 'text-muted'">{{ mult(s.volume_ratio, 2) }}</td>
                    <td class="num px-3 py-2 text-right">{{ inr(s.close) }}</td>
                    <td class="px-4 py-2"><StagesStoryCell :row="bySymbol.get(s.symbol)" :verdict="filterOf(s.symbol)" /></td>
                  </tr>
                </tbody>
              </table>
              <UEmpty v-if="!stocks.length" icon="i-lucide-filter-x" :title="liquidOnly ? 'No liquid member' : 'No member'" :description="liquidOnly ? 'Turn off Liquid only to see every member.' : undefined" class="py-10" />
            </div>
          </UCard>
        </template>
        <UEmpty v-else icon="i-lucide-search-x" :title="`No industry ${code}`" description="Pick one from the list of industries." />
      </div>
    </template>
  </UDashboardPanel>
</template>
