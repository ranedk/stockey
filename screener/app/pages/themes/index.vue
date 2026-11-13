<script setup lang="ts">
// Thematic watchlist (stockey docs/THEMATIC_WATCHLIST_PRD.md): the 5 strongest themes that are
// not late, 10 stocks each, ranked on four equal parts; refreshed every morning. Reporting only.
useHead({ title: 'Themes' })
const { data, status, error } = useStockey<ThemesResponse>('/api/themes')

const themes = computed(() => data.value?.themes ?? [])
const byStatus = (s: ThemesRow['status']) => themes.value.filter(t => t.status === s)
const shown = computed(() => byStatus('shown').sort((a, b) => (a.position ?? 99) - (b.position ?? 99)))
const late = computed(() => byStatus('late'))
const unranked = computed(() => byStatus('unranked'))
const stocksByTheme = computed(() => {
  const m = new Map<string, ThemesStock[]>()
  for (const s of data.value?.stocks ?? []) m.set(s.theme_id, [...(m.get(s.theme_id) ?? []), s])
  return m
})
const area = (a?: string) => (a ? a.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase()) : '')
</script>

<template>
  <UDashboardPanel id="themes">
    <template #header>
      <PageNavbar title="Themes" icon="i-lucide-sparkles">
        <template #right>
          <span v-if="data?.as_of" class="hidden text-xs text-muted sm:inline">Updated {{ dateShort(data.as_of) }}</span>
        </template>
      </PageNavbar>
    </template>

    <template #body>
      <div class="mx-auto w-full max-w-7xl space-y-6">
        <div class="space-y-2">
          <p class="max-w-3xl text-sm text-muted">
            Real end markets with money flowing in. Each theme is scored on four equal parts: government support,
            capacity being built, members' latest results and how many members are rising in price. The five strongest
            that are not <span class="font-medium text-highlighted">late</span> (more capacity coming than demand) are
            listed below, ten stocks each. Refreshed every morning; for reading only, nothing trades on it.
          </p>
          <UCollapsible class="max-w-3xl">
            <UButton label="How stocks are picked and what is left out" variant="link" color="neutral" size="xs" trailing-icon="i-lucide-chevron-down" class="px-0 group" :ui="{ trailingIcon: 'group-data-[state=open]:rotate-180 transition-transform' }" />
            <template #content>
              <ul class="mt-2 list-disc space-y-1 pl-5 text-sm text-muted">
                <li>Stocks are ranked on four equal parts: the share of revenue that comes from the theme, the price trend (an early breakout ranks above an extended run), business results and capacity being built.</li>
                <li>Left out: companies named in a SEBI order, with an auditor resignation, a loan default, promoter pledge over 10% or a flaw in their story score.</li>
                <li>Also left out: weak businesses (return on capital under 8% or negative operating cash flow over three years), unless their quarterly losses are shrinking.</li>
                <li>A theme stays on the list while it is still in the top 8 ("kept"), so the list does not churn every day.</li>
              </ul>
            </template>
          </UCollapsible>
        </div>

        <UAlert v-if="error" color="error" variant="subtle" icon="i-lucide-cloud-off" title="Could not load themes" :description="error.message" />
        <template v-else-if="status === 'pending' && !data">
          <USkeleton class="h-72 rounded-xl" />
          <USkeleton v-for="i in 2" :key="i" class="h-96 rounded-xl" />
        </template>
        <UEmpty v-else-if="data && !themes.length" icon="i-lucide-sparkles" title="No theme list yet" description="The list is built every morning." />

        <template v-else-if="data">
          <div class="grid items-start gap-4 lg:grid-cols-3">
            <UCard class="lg:col-span-2">
              <template #header>
                <h3 class="font-semibold text-highlighted">Theme strength</h3>
                <p class="text-xs text-muted">All ranked themes, 0-100, strongest first · click a theme for its members and policies</p>
              </template>
              <ThemesStrengthList :themes="themes" />
            </UCard>

            <div class="space-y-4">
              <UCard v-if="late.length">
                <template #header>
                  <div class="flex items-center gap-2">
                    <UIcon name="i-lucide-alarm-clock" class="size-4 text-error" />
                    <h3 class="font-semibold text-highlighted">Late</h3>
                  </div>
                  <p class="text-xs text-muted">More capacity coming than demand, so left off the list</p>
                </template>
                <ul class="space-y-3">
                  <li v-for="t in late" :key="t.theme_id">
                    <NuxtLink :to="`/themes/${t.theme_id}`" class="text-sm font-medium text-highlighted hover:text-primary">{{ t.name }}</NuxtLink>
                    <p class="mt-0.5 text-xs text-muted">{{ t.late_reasons }}</p>
                  </li>
                </ul>
              </UCard>
              <UCard v-if="unranked.length">
                <template #header>
                  <h3 class="font-semibold text-highlighted">Not ranked yet</h3>
                  <p class="text-xs text-muted">Fewer than 5 listed members with results</p>
                </template>
                <ul class="divide-y divide-default">
                  <li v-for="t in unranked" :key="t.theme_id">
                    <NuxtLink :to="`/themes/${t.theme_id}`" class="flex items-center justify-between gap-2 py-1.5 text-sm hover:text-primary">
                      <span class="truncate">{{ t.name }}</span>
                      <span class="num shrink-0 text-xs text-muted">{{ t.n_members ?? 0 }} {{ t.n_members === 1 ? 'member' : 'members' }}</span>
                    </NuxtLink>
                  </li>
                </ul>
              </UCard>
            </div>
          </div>

          <div>
            <h2 class="text-lg font-semibold text-highlighted">On the list</h2>
            <p class="text-xs text-muted">The {{ shown.length }} strongest themes that are not late, with their best stocks · click a stock row for its write-up</p>
          </div>

          <UCard v-for="t in shown" :key="t.theme_id" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex flex-wrap items-start justify-between gap-3">
                <div class="flex min-w-0 items-start gap-3">
                  <span class="num flex size-9 shrink-0 items-center justify-center rounded-full bg-primary/10 font-semibold text-primary">{{ t.position }}</span>
                  <div class="min-w-0">
                    <NuxtLink :to="`/themes/${t.theme_id}`" class="text-lg font-semibold text-highlighted hover:text-primary">{{ t.name }}</NuxtLink>
                    <div class="mt-0.5 flex flex-wrap items-center gap-2 text-xs text-muted">
                      <span>{{ area(t.area) }}</span>
                      <span>· {{ t.n_members ?? 0 }} listed members</span>
                      <UTooltip v-if="t.kept_by_buffer" text="Dropped out of the top five but still in the top eight, so kept to avoid daily churn">
                        <UBadge label="Kept (still in top 8)" color="neutral" variant="outline" size="xs" />
                      </UTooltip>
                    </div>
                  </div>
                </div>
                <div class="text-right">
                  <div class="text-xs text-muted">Strength</div>
                  <div class="num text-2xl font-semibold leading-tight text-highlighted">{{ themesScore(t.strength) ?? '—' }}</div>
                  <div class="text-[11px] text-dimmed">#{{ t.strength_order }} of all themes</div>
                </div>
              </div>
              <div class="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 md:grid-cols-4">
                <ThemesPartMeter v-for="p in THEMES_PARTS" :key="p.key" :label="p.label" :desc="p.desc" :value="t[p.key]" />
              </div>
              <ThemesFacts :theme="t" class="mt-4" />
              <div v-if="t.reasoning || t.conclusion" class="mt-4 space-y-2">
                <p v-if="t.conclusion" class="rounded-r-lg border-l-2 border-primary bg-primary/5 px-3 py-2 text-sm font-medium text-highlighted">{{ t.conclusion }}</p>
                <p v-if="t.reasoning" class="text-sm text-muted">{{ t.reasoning }}</p>
              </div>
            </template>
            <ThemesStockTable v-if="(stocksByTheme.get(t.theme_id) ?? []).length" :stocks="stocksByTheme.get(t.theme_id)!" variant="theme" />
            <UEmpty v-else icon="i-lucide-filter-x" title="No stock qualifies yet" description="No company passes all the checks with a known revenue share." class="py-8" />
          </UCard>

          <UCard v-if="data.also_strong?.length" :ui="{ body: 'p-0 sm:p-0' }">
            <template #header>
              <div class="flex items-center gap-2">
                <UIcon name="i-lucide-trending-up" class="size-4 text-up" />
                <h3 class="font-semibold text-highlighted">Also strong</h3>
              </div>
              <p class="text-xs text-muted">The best rising stocks (uptrend, top 3 of their theme) in themes outside the top five that are not late, so a good name is not hidden because its theme ranks sixth.</p>
            </template>
            <ThemesStockTable :stocks="data.also_strong" variant="also" />
          </UCard>
        </template>
      </div>
    </template>
  </UDashboardPanel>
</template>
