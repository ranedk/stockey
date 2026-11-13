<script setup lang="ts">
// One theme: strength parts, its members (ranked, revenue share not known, left out with the
// reason) and the government policies behind it.
const route = useRoute()
const id = computed(() => String(route.params.id))
const { data, status, error } = useStockey<ThemesDetail>(() => `/api/themes/${id.value}`)
useHead({ title: () => data.value?.theme?.name ?? 'Theme' })

const t = computed(() => data.value?.theme)
const group = (s: ThemesStock['status']) => (data.value?.stocks ?? []).filter(x => x.status === s)
const ranked = computed(() => group('ranked').sort((a, b) => (a.position ?? 999) - (b.position ?? 999)))
const unquantified = computed(() => group('unquantified'))
const excluded = computed(() => group('excluded'))
const st = computed(() => (t.value ? THEMES_STATUS[t.value.status] : null))
const area = (a?: string) => (a ? a.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()) : '')

const memberParts = computed(() => [
  { label: 'Ranked', value: ranked.value.length, color: 'var(--series-1)' },
  { label: 'Revenue share not known', value: unquantified.value.length, color: 'var(--chart-axis)' },
  { label: 'Left out', value: excluded.value.length, color: 'var(--down)' },
])
const stageParts = computed(() => {
  const n: Record<number, number> = { 0: 0, 1: 0, 2: 0, 3: 0, 4: 0 }
  for (const s of data.value?.stocks ?? []) n[s.stage && STAGES[s.stage] ? s.stage : 0]!++
  return [2, 1, 3, 4, 0].map(k => ({ label: k ? STAGES[k]!.short : 'No stage yet', value: n[k]!, color: STAGES_COLOR_VAR[k]! })).filter(p => p.value)
})
const policyFor = computed(() => (data.value?.policy ?? []).filter(p => p.direction === 'support').length)
const policyAgainst = computed(() => (data.value?.policy ?? []).filter(p => p.direction !== 'support').length)
const Link = resolveComponent('NuxtLink')
const notes = (r: string | null) => (r ?? '').replaceAll('note: ', '')
</script>

<template>
  <UDashboardPanel id="theme">
    <template #header>
      <PageNavbar :title="t?.name ?? 'Theme'" icon="i-lucide-sparkles">
        <template #right>
          <UButton to="/themes" icon="i-lucide-arrow-left" label="All themes" color="neutral" variant="ghost" size="sm" class="hidden sm:flex" />
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-cloud-off" title="Could not load this theme" :description="error.message">
          <template #actions><UButton to="/themes" label="Back to themes" color="neutral" variant="outline" size="xs" /></template>
        </UAlert>
        <template v-else-if="!t && status === 'pending'">
          <USkeleton class="h-40 rounded-xl" />
          <USkeleton class="h-96 rounded-xl" />
        </template>

        <template v-else-if="t">
          <!-- header -->
          <div class="grid gap-6 lg:grid-cols-[1fr_auto]">
            <div class="min-w-0 space-y-2">
              <NuxtLink to="/themes" class="inline-flex items-center gap-1 text-xs text-muted hover:text-primary sm:hidden"><UIcon name="i-lucide-arrow-left" />All themes</NuxtLink>
              <div class="flex flex-wrap items-center gap-x-3 gap-y-1">
                <h1 class="text-2xl font-semibold tracking-tight text-highlighted sm:text-3xl">{{ t.name }}</h1>
                <UTooltip v-if="st" :text="st.desc">
                  <UBadge :label="st.label" :icon="st.icon" :color="st.color" variant="subtle" />
                </UTooltip>
                <UBadge v-if="t.kept_by_buffer" label="Kept (still in top 8)" color="neutral" variant="outline" size="sm" />
              </div>
              <div class="text-xs text-muted">{{ area(t.area) }} · {{ t.n_members ?? 0 }} listed members · updated {{ dateShort(t.as_of_date) }}</div>
              <p v-if="t.definition" class="max-w-3xl text-sm text-toned">{{ t.definition }}</p>
            </div>
            <div class="flex gap-6 lg:flex-col lg:gap-2 lg:text-right">
              <div>
                <div class="text-xs text-muted">Strength</div>
                <div class="num text-3xl font-semibold tracking-tight text-highlighted">{{ themesScore(t.strength) ?? '—' }}</div>
              </div>
              <div>
                <div class="text-xs text-muted">Place among themes</div>
                <div class="num text-lg font-semibold text-highlighted">{{ t.strength_order ? `#${t.strength_order}` : 'Not ranked' }}</div>
              </div>
            </div>
          </div>

          <UAlert v-if="t.late_reasons" color="error" variant="subtle" icon="i-lucide-alarm-clock" title="Late: more capacity coming than demand" :description="t.late_reasons" />

          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2">
              <template #header>
                <h3 class="font-semibold text-highlighted">What drives the score</h3>
                <p class="text-xs text-muted">Four equal parts, each 0-100 against the other themes</p>
              </template>
              <div class="grid grid-cols-2 gap-x-6 gap-y-4 md:grid-cols-4">
                <ThemesPartMeter v-for="p in THEMES_PARTS" :key="p.key" :label="p.label" :desc="p.desc" :value="t[p.key]" />
              </div>
              <ThemesFacts :theme="t" class="mt-5" />
              <div v-if="t.reasoning || t.conclusion" class="mt-5 space-y-2">
                <p v-if="t.conclusion" class="rounded-r-lg border-l-2 border-primary bg-primary/5 px-3 py-2 text-sm font-medium text-highlighted">{{ t.conclusion }}</p>
                <p v-if="t.reasoning" class="text-sm text-muted">{{ t.reasoning }}</p>
              </div>
            </UCard>
            <UCard>
              <template #header>
                <h3 class="font-semibold text-highlighted">Members</h3>
                <p class="text-xs text-muted">{{ (data?.stocks ?? []).length }} listed companies in this market</p>
              </template>
              <div class="space-y-6">
                <div>
                  <div class="mb-2 text-sm font-medium">Who is ranked</div>
                  <SegmentedBar :parts="memberParts" :height="12" />
                </div>
                <div v-if="stageParts.length">
                  <div class="mb-2 text-sm font-medium">Trend stage of each member</div>
                  <SegmentedBar :parts="stageParts" :height="12" />
                </div>
                <div class="flex gap-3">
                  <div class="flex-1 rounded-lg bg-elevated/60 p-3">
                    <div class="text-xs text-muted">Policies for</div>
                    <div class="num mt-1 text-xl font-semibold text-up">{{ policyFor }}</div>
                  </div>
                  <div class="flex-1 rounded-lg bg-elevated/60 p-3">
                    <div class="text-xs text-muted">Policies against</div>
                    <div class="num mt-1 text-xl font-semibold" :class="policyAgainst ? 'text-down' : 'text-highlighted'">{{ policyAgainst }}</div>
                  </div>
                </div>
              </div>
            </UCard>
          </div>

          <!-- ranked -->
          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-baseline justify-between gap-2">
                <h3 class="font-semibold text-highlighted">Ranked members <span class="num text-muted">· {{ ranked.length }}</span></h3>
                <span class="text-xs text-muted">Faded rows are ranked but not on the shown list · click a row for its write-up</span>
              </div>
            </template>
            <ThemesStockTable v-if="ranked.length" :stocks="ranked" variant="ranked" />
            <UEmpty v-else icon="i-lucide-list-x" title="No ranked member" description="No company passes every check with a known revenue share." class="py-8" />
          </UCard>

          <!-- unquantified -->
          <UCard v-if="unquantified.length">
            <template #header>
              <h3 class="font-semibold text-highlighted">Revenue share not known <span class="num text-muted">· {{ unquantified.length }}</span></h3>
              <p class="text-xs text-muted">In the theme, but watched only: never ranked until a report gives the share of revenue it earns here</p>
            </template>
            <div class="flex flex-wrap gap-2">
              <component :is="s.ticker ? Link : 'div'" v-for="s in unquantified" :key="s.company_master_id"
                :to="s.ticker ? `/company/${encodeURIComponent(s.ticker)}` : undefined"
                class="flex items-center gap-2 rounded-lg border border-default px-2.5 py-1.5 text-sm transition hover:border-primary/50 hover:bg-elevated/40">
                <span class="font-medium text-highlighted">{{ s.company_name }}</span>
                <StageBadge :stage="s.stage" size="xs" short />
                <span v-if="s.weeks_in_stage" class="text-[11px] text-muted">{{ s.weeks_in_stage }} wk</span>
                <UBadge v-if="s.tier === 4" label="Early" color="success" variant="outline" size="xs" />
              </component>
            </div>
          </UCard>

          <!-- excluded -->
          <UCard v-if="excluded.length" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <h3 class="font-semibold text-highlighted">Left out <span class="num text-muted">· {{ excluded.length }}</span></h3>
              <p class="text-xs text-muted">Failed a governance or business check, with the reason</p>
            </template>
            <ul class="divide-y divide-default">
              <li v-for="s in excluded" :key="s.company_master_id" class="grid gap-1 px-4 py-3 sm:grid-cols-[16rem_1fr] sm:gap-4">
                <div class="min-w-0">
                  <NuxtLink v-if="s.ticker" :to="`/company/${encodeURIComponent(s.ticker)}`" class="font-medium text-highlighted hover:text-primary">{{ s.company_name }}</NuxtLink>
                  <span v-else class="font-medium text-highlighted">{{ s.company_name }}</span>
                  <div class="font-mono text-xs text-muted">{{ s.ticker }}</div>
                </div>
                <p class="text-xs text-toned">{{ notes(s.reasons) }}</p>
              </li>
            </ul>
          </UCard>

          <!-- policy -->
          <UCard :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <h3 class="font-semibold text-highlighted">Government policy</h3>
              <p class="text-xs text-muted">Schemes and rules that help or hurt this market</p>
            </template>
            <ul v-if="data?.policy?.length" class="divide-y divide-default">
              <li v-for="p in data.policy" :key="p.name" class="flex flex-col gap-2 px-4 py-3 sm:flex-row sm:items-start sm:gap-4">
                <UBadge :label="p.direction === 'support' ? 'Support' : 'Headwind'" :icon="p.direction === 'support' ? 'i-lucide-thumbs-up' : 'i-lucide-thumbs-down'"
                  :color="p.direction === 'support' ? 'success' : 'error'" variant="subtle" size="sm" class="w-fit shrink-0" />
                <div class="min-w-0 flex-1">
                  <a :href="p.source_url" target="_blank" rel="noopener" class="inline-flex items-center gap-1 font-medium text-highlighted hover:text-primary">
                    {{ p.name }}<UIcon name="i-lucide-external-link" class="size-3 text-muted" />
                  </a>
                  <p v-if="p.latest_figure" class="mt-0.5 text-xs text-toned">{{ p.latest_figure }}</p>
                  <div class="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted">
                    <span v-if="p.authority">{{ p.authority }}</span>
                    <span class="capitalize">{{ p.status }}</span>
                    <span v-if="p.announced">announced {{ dateShort(p.announced) }}</span>
                  </div>
                </div>
                <div v-if="p.outlay_cr" class="shrink-0 sm:text-right">
                  <div class="text-[11px] text-muted">Outlay</div>
                  <div class="num font-semibold text-highlighted">{{ crore(p.outlay_cr * 1e7) }}</div>
                </div>
              </li>
            </ul>
            <UEmpty v-else icon="i-lucide-landmark" title="No policy recorded for this theme" class="py-8" />
          </UCard>
        </template>
        <UEmpty v-else icon="i-lucide-search-x" :title="`No theme called ${id}`" description="Pick one from the themes list." />
      </div>
    </template>
  </UDashboardPanel>
</template>
