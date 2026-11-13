<script setup lang="ts">
import type { ScoresAndChecks } from '~/types/watchlist'

// Re-evaluation checks behind a watchlist name. `compact` = badges for list rows; otherwise a card.
const props = withDefaults(defineProps<{ checks: ScoresAndChecks | null | undefined, flaws?: string | null, compact?: boolean }>(),
  { flaws: null, compact: false })

const c = computed<ScoresAndChecks>(() => props.checks ?? { in_portfolio: false })
const sgn = (v: number | null | undefined, d = 0) => (isNum(v) ? `${v > 0 ? '+' : ''}${v.toFixed(d)}%` : '—')
const quarter = computed(() => {
  const d = c.value.results?.latest_period_end
  return d ? new Date(d).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' }) : null
})
const chg = computed(() => c.value.score_change_7d)
const val = computed(() => c.value.valuation_ratio)
// The portfolio never buys at 2x its own valuation history, and trims a third there.
const expensive = computed(() => isNum(val.value) && val.value >= 2)
const tone = (v: number | null | undefined, band = 0) => (!isNum(v) ? 'neutral' as const : v > band ? 'success' as const : v < -band ? 'warning' as const : 'neutral' as const)
</script>

<template>
  <div v-if="compact" class="flex flex-wrap gap-1">
    <UBadge v-if="c.in_portfolio" color="success" variant="soft" size="sm" icon="i-lucide-briefcase" label="In portfolio" />
    <UTooltip v-if="isNum(chg) && Math.abs(chg) >= 1" text="Change in the story score over the last 7 days">
      <UBadge :color="tone(chg)" variant="soft" size="sm" :label="`Story ${chg > 0 ? '+' : ''}${Math.round(chg)} in 7 days`" />
    </UTooltip>
    <UTooltip v-if="c.results && quarter" text="Latest quarter vs the same quarter a year earlier; profit excludes one-off items">
      <UBadge :color="tone(c.results.sales_yoy_pct, 5)" variant="soft" size="sm" :label="`${quarter}: sales ${sgn(c.results.sales_yoy_pct)} · profit ${sgn(c.results.profit_yoy_pct)}`" />
    </UTooltip>
    <UTooltip v-if="isNum(val)" text="Today's valuation divided by the company's own usual valuation. 1x = normal for it; 2x or more and the portfolio will not buy.">
      <UBadge :color="expensive ? 'warning' : 'neutral'" variant="soft" size="sm" :label="`Valued ${val.toFixed(1)}x its usual`" />
    </UTooltip>
    <UBadge v-if="flaws" color="error" variant="soft" size="sm" icon="i-lucide-triangle-alert" :label="`Flaw: ${flaws}`" class="max-w-full" :ui="{ label: 'truncate' }" />
  </div>

  <UCard v-else>
    <template #header>
      <h3 class="font-semibold text-highlighted">Scores and checks</h3>
      <p class="text-xs text-muted">Re-checked every night: has the story weakened, did results confirm it, is it now expensive?</p>
    </template>
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Story score 7 days ago</div>
        <div class="num mt-1 text-lg font-semibold text-highlighted">{{ isNum(c.score_7d_ago) ? c.score_7d_ago.toFixed(0) : '—' }}</div>
        <div v-if="isNum(chg)" class="text-xs" :class="signClass(chg)">{{ arrow(chg) }} {{ chg > 0 ? '+' : '' }}{{ chg.toFixed(0) }} since</div>
      </div>
      <div class="rounded-lg bg-elevated/60 p-3">
        <UTooltip text="Today's valuation divided by the company's own usual valuation. 1x = normal for it.">
          <div class="text-xs text-muted underline decoration-dotted underline-offset-2">Valuation vs its own past</div>
        </UTooltip>
        <div class="num mt-1 text-lg font-semibold" :class="expensive ? 'text-warning' : 'text-highlighted'">{{ isNum(val) ? `${val.toFixed(2)}x` : '—' }}</div>
        <div v-if="expensive" class="text-[11px] text-warning">2x or more: the portfolio will not buy, and trims a third</div>
        <div v-else-if="isNum(val)" class="text-[11px] text-dimmed">{{ val < 1 ? 'cheaper than usual' : 'dearer than usual' }}</div>
      </div>
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Portfolio (v3)</div>
        <div class="mt-1"><UBadge :color="c.in_portfolio ? 'success' : 'neutral'" variant="subtle" :label="c.in_portfolio ? 'Held' : 'Not held'" /></div>
      </div>
      <div class="rounded-lg bg-elevated/60 p-3">
        <div class="text-xs text-muted">Deal-breaker flaw</div>
        <div class="mt-1 text-sm font-medium" :class="flaws ? 'text-error' : 'text-dimmed'">{{ flaws || 'None' }}</div>
      </div>
    </div>

    <div v-if="c.results" class="mt-4 border-t border-default pt-4">
      <div class="mb-2 text-xs text-muted">Latest results{{ quarter ? ` (quarter to ${quarter})` : '' }}, against the same quarter a year earlier</div>
      <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <div><div class="text-xs text-dimmed">Sales</div><Change :value="wlFrac(c.results.sales_yoy_pct)" class="text-base" /></div>
        <div><div class="text-xs text-dimmed">Profit (one-offs removed)</div><Change :value="wlFrac(c.results.profit_yoy_pct)" class="text-base" /></div>
        <div>
          <div class="text-xs text-dimmed">Operating margin</div>
          <span class="num text-base font-medium" :class="signClass(c.results.margin_change_pp)">{{ arrow(c.results.margin_change_pp) }} {{ isNum(c.results.margin_change_pp) ? `${c.results.margin_change_pp > 0 ? '+' : ''}${c.results.margin_change_pp.toFixed(1)} pts` : '—' }}</span>
        </div>
        <div><div class="text-xs text-dimmed">One-offs, share of profit</div><span class="num text-base font-medium">{{ pct(c.results.one_off_share, 0) }}</span></div>
      </div>
    </div>
    <p v-else class="mt-3 text-xs text-dimmed">No results reading yet.</p>
  </UCard>
</template>
