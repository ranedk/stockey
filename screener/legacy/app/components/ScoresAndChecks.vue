<script setup lang="ts">
import type { ScoresAndChecks } from '~/types/api'

// The re-evaluation checks behind a watchlist name (2026-10-19). `compact` renders chips for
// the list rows; the full form is the company page's "Scores & checks" block.
const props = withDefaults(defineProps<{
  checks: ScoresAndChecks | null | undefined
  flaws?: string | null
  compact?: boolean
}>(), { flaws: null, compact: false })

const c = computed(() => props.checks ?? { in_portfolio: false })
const pct = (v: number | null | undefined, d = 0) => v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(d)}%`
const pp = (v: number | null | undefined) => v === null || v === undefined ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)} pts`
const quarter = computed(() => {
  const d = c.value.results?.latest_period_end
  return d ? new Date(d).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' }) : null
})
type Tone = 'good' | 'warn' | 'neutral'
const tone = (v: number | null | undefined, good = 0): Tone => v === null || v === undefined ? 'neutral' : v > good ? 'good' : v < -good ? 'warn' : 'neutral'
// the portfolio never buys at 2x its own valuation history and trims a third there
const valTone = computed<Tone>(() => (c.value.valuation_ratio ?? 0) >= 2 ? 'warn' : 'neutral')
</script>

<template>
  <div v-if="compact" class="flex flex-wrap gap-1">
    <BadgePill v-if="c.in_portfolio" label="in portfolio" tone="good" />
    <BadgePill
      v-if="c.score_change_7d !== null && c.score_change_7d !== undefined && Math.abs(c.score_change_7d) >= 1"
      :label="`story ${c.score_change_7d > 0 ? '+' : ''}${Math.round(c.score_change_7d)} in 7d`"
      :tone="tone(c.score_change_7d)"
    />
    <BadgePill v-if="c.results && quarter" :label="`${quarter}: sales ${pct(c.results.sales_yoy_pct)} · profit ${pct(c.results.profit_yoy_pct)}`"
               :tone="tone(c.results.sales_yoy_pct, 5)" />
    <BadgePill v-if="c.valuation_ratio !== null && c.valuation_ratio !== undefined"
               :label="`valuation ${c.valuation_ratio.toFixed(1)}× own history`" :tone="valTone" />
    <BadgePill v-if="flaws" :label="`flaw: ${flaws}`" tone="bad" />
  </div>

  <section v-else class="rounded-lg border border-slate-200 bg-white p-5">
    <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Scores &amp; checks</h2>
    <dl class="mt-3 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
      <div>
        <dt class="text-xs text-slate-500">Story score, 7 days ago</dt>
        <dd class="tabular-nums">{{ c.score_7d_ago?.toFixed(0) ?? '—' }}
          <span v-if="c.score_change_7d !== null && c.score_change_7d !== undefined" class="text-xs text-slate-500">
            ({{ c.score_change_7d > 0 ? '+' : '' }}{{ c.score_change_7d.toFixed(0) }} since)</span></dd>
      </div>
      <div>
        <dt class="text-xs text-slate-500">Valuation vs own history</dt>
        <dd class="tabular-nums" :class="(c.valuation_ratio ?? 0) >= 2 ? 'text-amber-700' : ''">
          {{ c.valuation_ratio !== null && c.valuation_ratio !== undefined ? `${c.valuation_ratio.toFixed(2)}×` : '—' }}
          <span v-if="(c.valuation_ratio ?? 0) >= 2" class="block text-[11px]">at or above 2×: the portfolio will not buy, and trims a third</span>
        </dd>
      </div>
      <div>
        <dt class="text-xs text-slate-500">Portfolio (v3)</dt>
        <dd>{{ c.in_portfolio ? 'held' : 'not held' }}</dd>
      </div>
      <div>
        <dt class="text-xs text-slate-500">Flaw</dt>
        <dd :class="flaws ? 'text-rose-700' : 'text-slate-400'">{{ flaws || 'none' }}</dd>
      </div>
    </dl>
    <div v-if="c.results" class="mt-4 border-t border-slate-100 pt-3">
      <div class="text-xs text-slate-500">Latest results{{ quarter ? ` (quarter to ${quarter})` : '' }}, vs the same quarter a year earlier</div>
      <dl class="mt-2 grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-4">
        <div><dt class="text-xs text-slate-500">Sales</dt><dd class="tabular-nums">{{ pct(c.results.sales_yoy_pct, 1) }}</dd></div>
        <div><dt class="text-xs text-slate-500">Profit (one-offs removed)</dt><dd class="tabular-nums">{{ pct(c.results.profit_yoy_pct, 1) }}</dd></div>
        <div><dt class="text-xs text-slate-500">Operating margin</dt><dd class="tabular-nums">{{ pp(c.results.margin_change_pp) }}</dd></div>
        <div><dt class="text-xs text-slate-500">One-offs, share of profit</dt><dd class="tabular-nums">{{ c.results.one_off_share !== null ? `${(c.results.one_off_share * 100).toFixed(0)}%` : '—' }}</dd></div>
      </dl>
    </div>
    <p v-else class="mt-3 text-xs text-slate-400">No results reading yet.</p>
  </section>
</template>
