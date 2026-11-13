<script setup lang="ts">
// Theme member table. variant: 'theme' = a listed theme's top ten, 'also' = strong names from
// other themes, 'ranked' = every ranked member on the theme page. Click a row for the write-up.
const props = defineProps<{ stocks: ThemesStock[], variant: 'theme' | 'also' | 'ranked' }>()
const open = ref<Record<string, boolean>>({})
const key = (s: ThemesStock) => s.theme_id + s.company_master_id
const toggle = (s: ThemesStock) => { open.value[key(s)] = !open.value[key(s)] }
const show = computed(() => ({
  theme: props.variant === 'also',
  breakout: props.variant !== 'also',
  rs: props.variant !== 'theme',
  roce: props.variant === 'ranked',
  ranks: props.variant !== 'also',
}))
const cols = computed(() => 6 + Object.values(show.value).filter(Boolean).length)
const fmtPct0 = (v: number | null) => (isNum(v) ? `${Math.round(v)}%` : '—')
</script>

<template>
  <div class="overflow-x-auto">
    <table class="w-full min-w-[860px] text-sm">
      <thead>
        <tr class="border-b border-default text-xs text-muted">
          <th class="w-16 px-4 py-2.5 text-left font-medium">
            <UTooltip :text="THEMES_RATING_DESC"><span class="cursor-help underline decoration-dotted underline-offset-2">Place</span></UTooltip>
          </th>
          <th v-if="show.theme" class="px-3 py-2.5 text-left font-medium">Theme</th>
          <th class="px-3 py-2.5 text-left font-medium">Company</th>
          <th class="px-3 py-2.5 text-right font-medium">
            <UTooltip text="Share of the company's revenue that comes from this theme"><span class="cursor-help underline decoration-dotted underline-offset-2">Revenue in theme</span></UTooltip>
          </th>
          <th class="px-3 py-2.5 text-left font-medium">Trend stage</th>
          <th v-if="show.breakout" class="px-3 py-2.5 text-right font-medium whitespace-nowrap">
            <UTooltip text="Price now against the level it broke out from (the prior 26-week high)"><span class="cursor-help underline decoration-dotted underline-offset-2">Above breakout</span></UTooltip>
          </th>
          <th v-if="show.rs" class="px-3 py-2.5 text-right font-medium whitespace-nowrap">
            <UTooltip text="Return over 26 weeks minus the market's return"><span class="cursor-help underline decoration-dotted underline-offset-2">vs market 26w</span></UTooltip>
          </th>
          <th class="px-3 py-2.5 text-right font-medium whitespace-nowrap">Sales growth</th>
          <th v-if="show.roce" class="px-3 py-2.5 text-right font-medium">
            <UTooltip text="Return on capital employed"><span class="cursor-help underline decoration-dotted underline-offset-2">ROCE</span></UTooltip>
          </th>
          <th v-if="show.ranks" class="px-3 py-2.5 text-left font-medium whitespace-nowrap">
            <UTooltip text="Overall score in the theme (0-100) and its four equal parts: revenue share, price trend, results, capacity"><span class="cursor-help underline decoration-dotted underline-offset-2">Score</span></UTooltip>
          </th>
          <th class="w-8" />
        </tr>
      </thead>
      <tbody>
        <template v-for="s in stocks" :key="key(s)">
          <tr class="group cursor-pointer border-b border-default/60 align-top hover:bg-elevated/40"
              :class="variant === 'ranked' && !s.on_watchlist ? 'opacity-60' : ''" @click="toggle(s)">
            <td class="px-4 py-2.5">
              <div class="flex items-center gap-1.5">
                <span class="num w-4 text-right text-xs text-dimmed">{{ s.position ?? '' }}</span>
                <UBadge v-if="s.rating" :label="s.rating" :color="THEMES_RATING[s.rating] ?? 'neutral'" variant="soft" size="sm" class="w-6 justify-center font-semibold" />
              </div>
            </td>
            <td v-if="show.theme" class="px-3 py-2.5">
              <NuxtLink :to="`/themes/${s.theme_id}`" class="font-medium text-highlighted hover:text-primary" @click.stop>{{ s.theme_name ?? s.theme_id }}</NuxtLink>
            </td>
            <td class="max-w-[22rem] px-3 py-2.5">
              <div class="flex flex-wrap items-center gap-x-2 gap-y-1">
                <NuxtLink v-if="s.ticker" :to="`/company/${encodeURIComponent(s.ticker)}`" class="font-medium text-highlighted hover:text-primary" @click.stop>{{ s.company_name ?? s.ticker }}</NuxtLink>
                <span v-else class="font-medium text-highlighted">{{ s.company_name }}</span>
                <span class="font-mono text-xs text-muted">{{ s.ticker }}</span>
                <UTooltip v-if="s.web_verdict && THEMES_WEB[s.web_verdict]" :text="THEMES_WEB[s.web_verdict]!.desc">
                  <UBadge :label="THEMES_WEB[s.web_verdict]!.label" :icon="THEMES_WEB[s.web_verdict]!.icon" :color="THEMES_WEB[s.web_verdict]!.color" variant="subtle" size="xs" />
                </UTooltip>
                <UBadge v-if="variant === 'ranked' && s.on_watchlist" label="On the list" color="primary" variant="subtle" size="xs" />
              </div>
              <p v-if="s.conclusion" class="mt-1 text-xs text-toned">{{ s.conclusion }}</p>
            </td>
            <td class="px-3 py-2.5 text-right">
              <div class="num font-medium">{{ fmtPct0(s.share_pct) }}</div>
              <UTooltip v-if="s.share_source" :text="s.share_source">
                <div class="ml-auto max-w-32 truncate text-[11px] text-dimmed">{{ s.share_source }}</div>
              </UTooltip>
            </td>
            <td class="px-3 py-2.5">
              <div class="flex flex-col items-start gap-1">
                <StageBadge :stage="s.stage" size="xs" short />
                <div class="flex items-center gap-1 text-[11px] text-muted">
                  <span v-if="s.weeks_in_stage">{{ s.weeks_in_stage }} wk</span>
                  <UTooltip v-if="s.tier === 4" :text="THEMES_EARLY_DESC">
                    <UBadge label="Early" icon="i-lucide-sparkle" color="success" variant="outline" size="xs" />
                  </UTooltip>
                </div>
              </div>
            </td>
            <td v-if="show.breakout" class="px-3 py-2.5 text-right"><Change :value="themesFrac(s.from_breakout_pct)" :digits="0" /></td>
            <td v-if="show.rs" class="px-3 py-2.5 text-right"><Change :value="themesFrac(s.rs26_pct)" :digits="0" /></td>
            <td class="px-3 py-2.5 text-right"><Change :value="themesFrac(s.sales_yoy_pct)" :digits="0" /></td>
            <td v-if="show.roce" class="num px-3 py-2.5 text-right">{{ fmtPct0(s.roce_pct) }}</td>
            <td v-if="show.ranks" class="px-3 py-2.5"><ThemesRankBars :stock="s" /></td>
            <td class="px-2 py-2.5 text-muted">
              <UIcon name="i-lucide-chevron-down" class="size-4 transition-transform" :class="open[key(s)] ? 'rotate-180' : ''" />
            </td>
          </tr>
          <tr v-if="open[key(s)]" class="border-b border-default/60 bg-elevated/30">
            <td :colspan="cols" class="px-4 py-3">
              <ThemesStockNotes :stock="s" />
            </td>
          </tr>
        </template>
      </tbody>
    </table>
  </div>
</template>
