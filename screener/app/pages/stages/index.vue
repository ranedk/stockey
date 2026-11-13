<script setup lang="ts">
// Industry rotation (systrade/docs/SECTOR_ROTATION_PRD.md): market health, industries ranked on
// 26-week return against the market with their trend stage, and the liquid uptrend stocks that
// lead inside the leading industries. Reporting only (LEDGER row 53).
useHead({ title: 'Stages' })
const { data, status, error } = useSystrader<StagesResponse>('/api/rotation')
const { bySymbol, median, filterOf, storiesError } = useStagesStories()

const all = computed(() => data.value?.industries ?? [])
const rankedCount = computed(() => all.value.filter(i => i.rank > 0).length)
const leaders = computed(() => all.value.filter(i => i.leading).sort((a, b) => a.rank - b.rank))
const nameOf = computed(() => new Map(all.value.map(i => [i.code, i.name || i.code])))

const leadersOnly = ref(false)
const sector = ref('all')
const sectors = computed(() => {
  const seen = new Map<string, string>()
  for (const i of all.value) seen.set(i.sector_code, i.sector_name || i.sector_code)
  return [{ label: 'All sectors', value: 'all' }, ...[...seen.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([value, label]) => ({ label, value }))]
})
const industries = computed(() => all.value
  .filter(i => (!leadersOnly.value || i.leading) && (sector.value === 'all' || i.sector_code === sector.value))
  .sort((a, b) => (a.rank || 1e6) - (b.rank || 1e6)))
const rsMax = computed(() => Math.max(1, ...all.value.map(i => Math.abs(i.rs26_pct ?? 0))))

const filterMode = ref<'all' | 'pass'>('all')
const search = ref('')
const candidates = computed(() => {
  const q = search.value.trim().toUpperCase()
  return (data.value?.stocks ?? []).filter(s => (!q || s.symbol.includes(q)) && (filterMode.value === 'all' || filterOf(s.symbol) === 'pass'))
})

const m = computed(() => data.value?.market)
const st = computed(() => data.value?.stats)
const wk = (v: number | null | undefined) => (isNum(v) ? `${Math.round(v)} wk` : '—')
const breadthColor = (v: number) => (v >= 50 ? 'success' : v >= 30 ? 'warning' : 'error')
</script>

<template>
  <UDashboardPanel id="stages">
    <template #header>
      <PageNavbar title="Stages" icon="i-lucide-chart-no-axes-combined">
        <template #right>
          <span v-if="data?.as_of" class="hidden text-xs text-muted sm:inline">Week ending {{ dateShort(data.as_of) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <div class="space-y-2">
          <p class="max-w-3xl text-sm text-muted">
            A weekly top-down view: is the market healthy, which industries are beating the market over 26 weeks while
            their own index is in an uptrend, and which liquid stocks in an uptrend lead inside them.
            For reading only: nothing trades on this page.
          </p>
          <UCollapsible class="max-w-3xl">
            <UButton label="Why nothing trades on it, and what does" variant="link" color="neutral" size="xs" trailing-icon="i-lucide-chevron-down" class="group px-0" :ui="{ trailingIcon: 'group-data-[state=open]:rotate-180 transition-transform' }" />
            <template #content>
              <div class="mt-2 space-y-2 text-sm text-muted">
                <p>The strategy built on this view failed its test set in advance (research ledger row 53, 2013-2021): buying only inside the leading industries was calmer but earned less than buying every uptrend stock.</p>
                <p>
                  Uptrend stocks ranked by their return against the market run as their own forward paper track:
                  <NuxtLink to="/paper/stage2-rs-leaders" class="font-medium text-primary hover:underline">stage2-rs-leaders</NuxtLink>,
                  and with the fundamentals check added as
                  <NuxtLink to="/paper/stage2-rs-leaders-clean" class="font-medium text-primary hover:underline">stage2-rs-leaders-clean</NuxtLink>.
                </p>
              </div>
            </template>
          </UCollapsible>
        </div>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-cloud-off" title="Could not load the weekly rotation snapshot" :description="error.message" />
        <template v-else-if="status === 'pending' && !data">
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-5"><USkeleton v-for="i in 5" :key="i" class="h-24 rounded-xl" /></div>
          <USkeleton class="h-96 rounded-xl" />
        </template>

        <template v-else-if="data && m && st">
          <!-- market -->
          <div class="grid grid-cols-2 gap-3 lg:grid-cols-5">
            <UCard :ui="{ body: 'p-3 sm:p-4' }" class="col-span-2 lg:col-span-1">
              <div class="text-xs text-muted">Market trend stage</div>
              <div class="mt-2"><StageBadge :stage="m.stage" size="md" /></div>
              <div class="mt-2 text-[11px] text-dimmed">Equal-weight index of stocks trading ₹10 Cr+ a day</div>
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <div class="text-xs text-muted">Stocks in an uptrend</div>
              <div class="num mt-1 text-xl font-semibold text-highlighted">{{ Math.round(m.breadth_pct) }}%</div>
              <UProgress :model-value="m.breadth_pct" size="xs" class="mt-2" :color="breadthColor(m.breadth_pct)" />
              <div class="mt-1 text-[11px] text-dimmed">of {{ num(m.eligible_count) }} liquid stocks</div>
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <div class="text-xs text-muted">Market return, 26 weeks</div>
              <div class="mt-1 text-xl"><Change :value="themesFrac(m.return_26w_pct)" /></div>
              <div class="mt-1 text-[11px] text-dimmed">equal-weight, liquid stocks</div>
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <UTooltip :text="STAGES_HELP.leading">
                <div class="cursor-help text-xs text-muted underline decoration-dotted underline-offset-2">Leading industries</div>
              </UTooltip>
              <div class="num mt-1 text-xl font-semibold text-highlighted">{{ leaders.length }} <span class="text-sm font-normal text-muted">of {{ rankedCount }}</span></div>
              <UProgress :model-value="rankedCount ? leaders.length / rankedCount * 100 : 0" size="xs" class="mt-2" color="primary" />
            </UCard>
            <UCard :ui="{ body: 'p-3 sm:p-4' }">
              <div class="text-xs text-muted">Week ending</div>
              <div class="mt-1 text-xl font-semibold text-highlighted">{{ dateShort(data.as_of) }}</div>
              <div class="mt-1 text-[11px] text-dimmed">updated weekly from closing prices</div>
            </UCard>
          </div>

          <!-- durations -->
          <UCard>
            <template #header>
              <h3 class="font-semibold text-highlighted">How long things last</h3>
              <p class="text-xs text-muted">Measured since {{ dateShort(st.from) }}. Most moves are short and a few run long, so trading this weekly would mostly chase noise.</p>
            </template>
            <div class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <div class="rounded-lg bg-elevated/60 p-3">
                <div class="text-xs text-muted">A stock's uptrend lasts</div>
                <div class="num mt-1 text-xl font-semibold text-highlighted">{{ wk(st.stage2_weeks.p50) }}</div>
                <div class="mt-1 text-[11px] text-dimmed">typical (median) · {{ wk(st.stage2_weeks.mean) }} on average · 1 in 10 runs {{ wk(st.stage2_weeks.p90) }}+ · {{ num(st.stage2_runs) }} runs</div>
              </div>
              <div class="rounded-lg bg-elevated/60 p-3">
                <div class="text-xs text-muted">An industry stays in the top fifth</div>
                <div class="num mt-1 text-xl font-semibold text-highlighted">{{ wk(st.top_fifth_weeks.p50) }}</div>
                <div class="mt-1 text-[11px] text-dimmed">typical · {{ wk(st.top_fifth_weeks.mean) }} on average · 1 in 10 stays {{ wk(st.top_fifth_weeks.p90) }}+</div>
              </div>
              <div class="rounded-lg bg-elevated/60 p-3">
                <div class="text-xs text-muted">An industry stays leading</div>
                <div class="num mt-1 text-xl font-semibold text-highlighted">{{ wk(st.leadership_weeks.p50) }}</div>
                <div class="mt-1 text-[11px] text-dimmed">typical · {{ wk(st.leadership_weeks.mean) }} on average · 1 in 10 stays {{ wk(st.leadership_weeks.p90) }}+ · {{ num(st.leadership_runs) }} runs</div>
              </div>
              <div class="rounded-lg bg-elevated/60 p-3">
                <div class="text-xs text-muted">New leading industries</div>
                <div class="num mt-1 text-xl font-semibold text-highlighted">{{ isNum(st.new_leaders_per_quarter) ? Math.round(st.new_leaders_per_quarter) : '—' }}</div>
                <div class="mt-1 text-[11px] text-dimmed">per quarter, about</div>
              </div>
            </div>
          </UCard>

          <!-- leaders -->
          <div v-if="leaders.length">
            <div class="mb-3">
              <h2 class="text-lg font-semibold text-highlighted">Leading now</h2>
              <p class="text-xs text-muted">{{ STAGES_HELP.leading }}</p>
            </div>
            <div class="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <NuxtLink v-for="i in leaders" :key="i.code" :to="`/stages/${i.code}`">
                <UCard class="h-full transition hover:shadow-md hover:ring-primary/40" :ui="{ body: 'p-4 sm:p-4 space-y-3' }">
                  <div class="flex items-start justify-between gap-2">
                    <div class="min-w-0">
                      <div class="truncate font-medium text-highlighted">{{ i.name || i.code }}</div>
                      <div class="truncate text-xs text-muted">{{ i.sector_name }}</div>
                    </div>
                    <span class="num shrink-0 rounded-md bg-primary/10 px-1.5 py-0.5 text-xs font-semibold text-primary">#{{ i.rank }}</span>
                  </div>
                  <div class="flex items-end justify-between gap-2">
                    <div>
                      <div class="text-[11px] text-dimmed">vs market, 26 wk</div>
                      <Change :value="themesFrac(i.rs26_pct)" pill class="text-sm" />
                    </div>
                    <StagesRankSpark :history="i.rank_history" :ranked="rankedCount" :width="88" :height="28" />
                  </div>
                  <div>
                    <div class="mb-1 flex justify-between text-xs"><span class="text-muted">Liquid members in an uptrend</span><span class="num font-medium">{{ Math.round(i.stage2_pct) }}%</span></div>
                    <UProgress :model-value="i.stage2_pct" size="xs" :color="breadthColor(i.stage2_pct)" />
                  </div>
                  <div class="text-[11px] text-muted">Leading for {{ i.weeks_leading }} {{ i.weeks_leading === 1 ? 'week' : 'weeks' }}</div>
                </UCard>
              </NuxtLink>
            </div>
          </div>

          <!-- industries -->
          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 class="font-semibold text-highlighted">All industries</h3>
                  <p class="text-xs text-muted">Ranked by return against the market over 26 weeks · click one for its stocks</p>
                </div>
                <div class="flex flex-wrap items-center gap-3">
                  <USwitch v-model="leadersOnly" label="Leading only" size="sm" />
                  <USelect v-model="sector" :items="sectors" size="sm" class="w-48" />
                </div>
              </div>
            </template>
            <div class="overflow-x-auto">
              <table class="w-full min-w-[900px] text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="px-4 py-2.5 text-right font-medium">
                      <UTooltip :text="STAGES_HELP.rank"><span class="cursor-help underline decoration-dotted underline-offset-2">Rank</span></UTooltip>
                    </th>
                    <th class="px-3 py-2.5 text-left font-medium">Industry</th>
                    <th class="px-3 py-2.5 text-left font-medium">
                      <UTooltip :text="STAGES_HELP.rs26"><span class="cursor-help underline decoration-dotted underline-offset-2">vs market, 26 wk</span></UTooltip>
                    </th>
                    <th class="px-3 py-2.5 text-right font-medium">Since 4 wk</th>
                    <th class="px-3 py-2.5 text-right font-medium">Since 13 wk</th>
                    <th class="px-3 py-2.5 text-left font-medium">Rank, 26 wk</th>
                    <th class="px-3 py-2.5 text-left font-medium">Index stage</th>
                    <th class="px-3 py-2.5 text-left font-medium">
                      <UTooltip :text="`Share of liquid members in an uptrend (Stage 2). ${STAGES_HELP.liquid}`"><span class="cursor-help underline decoration-dotted underline-offset-2">Members in uptrend</span></UTooltip>
                    </th>
                    <th class="px-4 py-2.5 text-right font-medium">
                      <UTooltip :text="STAGES_HELP.liquid"><span class="cursor-help underline decoration-dotted underline-offset-2">Liquid / all</span></UTooltip>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="i in industries" :key="i.code" class="group border-b border-default/60 last:border-0 hover:bg-elevated/40" :class="i.leading ? 'bg-success/5' : ''">
                    <td class="num px-4 py-2 text-right font-medium" :class="i.rank ? 'text-highlighted' : 'text-dimmed'">{{ i.rank || '—' }}</td>
                    <td class="max-w-72 px-3 py-2">
                      <NuxtLink :to="`/stages/${i.code}`" class="block truncate font-medium text-highlighted group-hover:text-primary">{{ i.name || i.code }}</NuxtLink>
                      <div class="flex items-center gap-1.5 text-xs text-muted">
                        <span class="truncate">{{ i.sector_name }}</span>
                        <UBadge v-if="i.leading" :label="`Leading ${i.weeks_leading} wk`" color="success" variant="subtle" size="xs" class="shrink-0 whitespace-nowrap" />
                      </div>
                    </td>
                    <td class="px-3 py-2">
                      <div class="flex items-center gap-2">
                        <Change :value="themesFrac(i.rs26_pct)" class="w-16 justify-end" />
                        <div class="relative h-1.5 w-20 rounded-full bg-elevated">
                          <div v-if="isNum(i.rs26_pct)" class="absolute top-0 h-full rounded-full"
                            :class="i.rs26_pct >= 0 ? 'left-1/2 bg-[var(--up)]' : 'right-1/2 bg-[var(--down)]'"
                            :style="{ width: `${Math.abs(i.rs26_pct) / rsMax * 50}%` }" />
                        </div>
                      </div>
                    </td>
                    <td class="px-3 py-2 text-right"><StagesMove :now="i.rank" :before="i.rank_4w" /></td>
                    <td class="px-3 py-2 text-right"><StagesMove :now="i.rank" :before="i.rank_13w" /></td>
                    <td class="px-3 py-2"><StagesRankSpark :history="i.rank_history" :ranked="rankedCount" /></td>
                    <td class="px-3 py-2"><StageBadge :stage="i.stage" size="xs" short /></td>
                    <td class="px-3 py-2">
                      <div class="flex items-center gap-2">
                        <UProgress :model-value="i.stage2_pct" size="xs" :color="breadthColor(i.stage2_pct)" class="w-20" />
                        <span class="num w-9 text-right text-xs font-medium">{{ Math.round(i.stage2_pct) }}%</span>
                      </div>
                    </td>
                    <td class="num px-4 py-2 text-right whitespace-nowrap text-muted">{{ i.eligible_members }} / {{ i.members }}</td>
                  </tr>
                </tbody>
              </table>
              <UEmpty v-if="!industries.length" icon="i-lucide-filter-x" title="No industry matches" description="Turn off 'Leading only' or pick another sector." class="py-10" />
            </div>
          </UCard>

          <!-- candidates -->
          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 class="font-semibold text-highlighted">Candidates <span class="num text-muted">· {{ candidates.length }}</span></h3>
                  <p class="text-xs text-muted">Liquid stocks in an uptrend inside the leading industries, strongest first</p>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <USelect v-model="filterMode" :items="[{ label: 'Any fundamentals', value: 'all' }, { label: 'Fundamentals check: passes', value: 'pass' }]" size="sm" class="w-56" />
                  <UInput v-model="search" icon="i-lucide-search" placeholder="Symbol" size="sm" class="w-36" />
                </div>
              </div>
              <p class="mt-2 flex gap-1.5 text-xs text-muted">
                <UIcon name="i-lucide-info" class="mt-0.5 size-3.5 shrink-0" />
                <span>Fundamentals check: no flaw and a story score at or above the median ({{ isNum(median) ? Math.round(median) : '—' }}). Story scores exist only from 29 Sep 2026, so it cannot be tested on the past and is tested forward only.<span v-if="storiesError" class="text-error"> Story scores could not be loaded.</span></span>
              </p>
            </template>
            <div class="overflow-x-auto">
              <table class="w-full min-w-[940px] text-sm">
                <thead>
                  <tr class="border-b border-default text-xs text-muted">
                    <th class="px-4 py-2.5 text-left font-medium">Stock</th>
                    <th class="px-3 py-2.5 text-left font-medium">Industry</th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.rs26"><span class="cursor-help underline decoration-dotted underline-offset-2">vs market, 26 wk</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium">Rank in industry</th>
                    <th class="px-3 py-2.5 text-right font-medium">Weeks in uptrend</th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.aboveMa"><span class="cursor-help underline decoration-dotted underline-offset-2">Above 30-wk avg</span></UTooltip></th>
                    <th class="px-3 py-2.5 text-right font-medium"><UTooltip :text="STAGES_HELP.volume"><span class="cursor-help underline decoration-dotted underline-offset-2">Volume vs usual</span></UTooltip></th>
                    <th class="px-4 py-2.5 text-left font-medium"><UTooltip :text="STAGES_HELP.filter"><span class="cursor-help underline decoration-dotted underline-offset-2">Fundamentals</span></UTooltip></th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="s in candidates" :key="s.symbol" class="group border-b border-default/60 last:border-0 hover:bg-elevated/40">
                    <td class="px-4 py-2">
                      <NuxtLink :to="`/company/${encodeURIComponent(s.symbol)}`" class="font-mono font-medium text-highlighted group-hover:text-primary">{{ s.symbol }}</NuxtLink>
                      <div class="num text-xs text-muted">{{ inr(s.close) }}</div>
                    </td>
                    <td class="max-w-56 px-3 py-2">
                      <NuxtLink :to="`/stages/${s.industry_code}`" class="block truncate text-toned hover:text-primary">{{ nameOf.get(s.industry_code) ?? s.industry_code }}</NuxtLink>
                    </td>
                    <td class="px-3 py-2 text-right"><Change :value="themesFrac(s.rs26_pct)" /></td>
                    <td class="num px-3 py-2 text-right">{{ s.rank_in_industry }}</td>
                    <td class="num px-3 py-2 text-right">{{ s.weeks_in_stage2 }}</td>
                    <td class="px-3 py-2 text-right"><Change :value="themesFrac(s.above_ma30_pct)" /></td>
                    <td class="num px-3 py-2 text-right" :class="isNum(s.volume_ratio) && s.volume_ratio >= 1.5 ? 'font-medium text-highlighted' : 'text-muted'">{{ mult(s.volume_ratio, 2) }}</td>
                    <td class="px-4 py-2"><StagesStoryCell :row="bySymbol.get(s.symbol)" :verdict="filterOf(s.symbol)" /></td>
                  </tr>
                </tbody>
              </table>
              <UEmpty v-if="!candidates.length" icon="i-lucide-filter-x" title="No candidates match" class="py-10" />
            </div>
          </UCard>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
