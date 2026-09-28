<script setup lang="ts">
import type { WatchlistDetail } from '~/types/api'

const route = useRoute()
const companyMasterId = computed(() => decodeURIComponent(route.params.id as string))

const api = useApi()
const { data, status, error, refresh } = await useAsyncData(
  () => `watchlist-detail-${companyMasterId.value}`,
  () => api.get<WatchlistDetail>(`/api/watchlist/${encodeURIComponent(companyMasterId.value)}`),
  { watch: [companyMasterId] },
)

// The human add/resolve forms were removed 2026-09-04 with the human forecast register.
// Positions are opened by the nightly ruleset and their forecasts resolved unattended, so
// this section is now a read-only view of what the machine decided and why.
const openPosition = computed(
  () => data.value?.portfolio.find((t) => t.status === 'open' && t.entry_decision !== 'reject') ?? null,
)
const vetoed = computed(
  () => data.value?.portfolio.find((t) => t.entry_decision === 'reject') ?? null,
)
const resolvedForecasts = computed(
  () => (data.value?.portfolio ?? []).filter((t) => t.resolution_date !== null),
)

const priceDeltaTone = computed(() => {
  if (!data.value) return 'neutral'
  const pct = priceChangePct(data.value.watchlist.first_seen_price, data.value.watchlist.current_price)
  if (pct === null) return 'neutral'
  return pct >= 0 ? 'good' : 'bad'
})

const phaseTone: Record<string, 'good' | 'bad' | 'neutral'> = {
  capacity_discipline: 'good',
  capacity_expansion: 'bad',
  balanced: 'neutral',
}

const satisfiedStrategies = computed(() =>
  (data.value?.signal_pointers ?? []).filter((p) => p.signal_type === 'strategy_satisfied'),
)
const otherSignalPointers = computed(() =>
  (data.value?.signal_pointers ?? []).filter((p) => p.signal_type !== 'strategy_satisfied'),
)

const DIRECTION_TONE: Record<string, 'good' | 'bad' | 'neutral'> = {
  increasing: 'good',
  decreasing: 'bad',
  new: 'good',
  up: 'good',
  down: 'bad',
}
function directionTone(direction: string | null): 'good' | 'bad' | 'neutral' {
  return direction ? DIRECTION_TONE[direction] || 'neutral' : 'neutral'
}

// Delivery % and price-band hits (TODO C5): descriptive, not signals. Band hits are blank for
// stocks with futures -- NSE's band-hit file is inverted for them (checked 2026-09-28).
const deliveryLabel = computed(() => {
  const t = data.value?.technicals as Record<string, number | null> | undefined
  if (t?.delivery_pct_20d == null) return '—'
  const change = t.delivery_change_pp
  return `${t.delivery_pct_20d}%` + (change == null ? '' : ` (${change > 0 ? '+' : ''}${change} pp)`)
})
const bandLabel = computed(() => {
  const t = data.value?.technicals as Record<string, number | null> | undefined
  if (!t || t.delivery_pct_20d == null) return '—'
  if (t.upper_band_hits_60d == null) return 'n/a (F&O)'
  return `${t.upper_band_hits_60d} / ${t.lower_band_hits_60d}`
})
</script>

<template>
  <div v-if="error" class="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
    Could not load {{ companyMasterId }} ({{ error.statusCode === 404 ? 'not on watchlist' : error.message }}).
  </div>

  <p v-else-if="status === 'pending'" class="text-sm text-slate-500">Loading…</p>

  <div v-else-if="data" class="space-y-6">
    <NuxtLink to="/watchlist" class="text-sm text-slate-500 hover:text-slate-900">← Watchlist</NuxtLink>

    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <div class="flex items-center gap-2">
          <h1 class="text-2xl font-semibold">{{ data.watchlist.company_master_id.replace('nse:', '') }}</h1>
          <BadgePill
            v-if="data.watchlist.status !== 'active'"
            :label="WATCHLIST_STATUS_LABELS[data.watchlist.status]"
            :tone="WATCHLIST_STATUS_TONE[data.watchlist.status]"
          />
        </div>
        <p class="text-slate-600">{{ data.watchlist.company_name }}</p>
        <p v-if="data.watchlist.status_reason" class="mt-1 text-sm text-slate-500">{{ data.watchlist.status_reason }}</p>
        <div v-if="satisfiedStrategies.length" class="mt-2 flex flex-wrap gap-1.5">
          <NuxtLink
            v-for="p in satisfiedStrategies"
            :key="p.value as string"
            :to="`/strategies/${encodeURIComponent(p.value as string)}`"
          >
            <BadgePill :label="strategyLabel(p.value as string)" tone="neutral" />
          </NuxtLink>
        </div>
      </div>
      <div class="text-right">
        <div class="flex items-center justify-end gap-2">
          <span class="text-sm">{{ formatPrice(data.watchlist.first_seen_price) }} → {{ formatPrice(data.watchlist.current_price) }}</span>
          <BadgePill :label="formatPct(priceChangePct(data.watchlist.first_seen_price, data.watchlist.current_price))" :tone="priceDeltaTone" />
        </div>
        <p class="mt-1 text-xs text-slate-400">watching since {{ formatDate(data.watchlist.first_seen_at) }}</p>
        <p v-if="data.watchlist.suggested_watch_until" class="text-xs text-slate-400">
          suggested watch until {{ formatDate(data.watchlist.suggested_watch_until) }}
        </p>
      </div>
    </div>

    <!-- Narrative-first: the WHY, front and center -->
    <section class="rounded-lg border border-slate-200 bg-white p-5">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Why this is on the watchlist</h2>
      <p v-if="data.watchlist.narrative_text" class="mt-2 whitespace-pre-line text-slate-800">
        {{ data.watchlist.narrative_text }}
      </p>
      <p v-else class="mt-2 text-sm italic text-slate-400">Narrative not generated yet -- run the watch_summary pipeline.</p>
    </section>

    <ConfluencePanel :confluence="data.confluence ?? null" />

    <!-- Portfolio: machine-decided, read-only -->
    <section class="rounded-lg border border-slate-200 bg-white p-5">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Portfolio</h2>

      <div v-if="openPosition" class="mt-3">
        <div class="flex flex-wrap items-center gap-2">
          <BadgePill label="open position" tone="good" />
          <BadgePill v-if="openPosition.kind === 'shadow'" label="no money committed" tone="neutral" />
        </div>
        <p class="mt-2 text-sm text-slate-800">{{ openPosition.prediction_text }}</p>
        <p class="mt-1 text-xs text-slate-500">
          target: {{ formatDate(openPosition.target_date) }}
          <template v-if="openPosition.stop_pct"> · stop: {{ openPosition.stop_pct }}%</template>
          <template v-if="openPosition.metric_name">
            · resolves on <code>{{ openPosition.metric_name }} {{ openPosition.metric_operator }} {{ openPosition.metric_threshold }}</code>
          </template>
          <template v-else> · no machine-checkable metric — will be judged</template>
        </p>
        <p v-if="openPosition.invalidation_criteria" class="mt-1 text-xs text-slate-500">
          invalidation: {{ openPosition.invalidation_criteria }}
        </p>
        <p v-if="openPosition.adjudicator_reason" class="mt-2 text-xs text-slate-400">
          adjudicator: {{ openPosition.adjudicator_reason }}
        </p>

        <!-- L5 sizing sits inside the open-position branch on purpose: the endpoint 404s
             without one, and an accepted position is the gate sizing sits behind. -->
        <PositionSizingPanel :company-master-id="data.watchlist.company_master_id" />
      </div>

      <div v-else-if="vetoed" class="mt-3">
        <BadgePill label="vetoed by the adjudicator" tone="warn" />
        <p class="mt-2 text-xs text-slate-500">
          The mechanical ruleset passed this name and the adjudicator rejected it. It is
          tracked anyway, so the veto itself can be scored.
        </p>
        <p v-if="vetoed.adjudicator_reason" class="mt-2 text-sm text-slate-700">{{ vetoed.adjudicator_reason }}</p>
      </div>

      <p v-else class="mt-3 text-sm text-slate-500">
        Not in the portfolio. The nightly ruleset decides this — there is nothing to add by hand.
      </p>

      <div v-if="resolvedForecasts.length" class="mt-4 border-t border-slate-100 pt-3">
        <p class="text-xs font-medium text-slate-500">Resolved forecasts</p>
        <ul class="mt-2 space-y-1 text-xs text-slate-500">
          <li v-for="t in resolvedForecasts" :key="t.position_id">
            {{ formatDate(t.opened_at) }} → {{ formatDate(t.resolution_date) }}:
            {{ t.resolved_true ? 'came true' : 'did not come true' }}
            <span v-if="t.failure_attribution">({{ t.failure_attribution }})</span>
            <span class="text-slate-400"> · {{ t.resolution_method }}</span>
          </li>
        </ul>
      </div>
    </section>

    <!-- Context: descriptive facts, not signals -->
    <section class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatChip label="Net debt YoY delta" :value="data.l2_state?.net_debt_yoy_delta_rscr as any" />
      <StatChip label="Promoter stake" :value="data.l2_state?.promoter_stake_direction as any" />
      <StatChip label="Pledge %" :value="data.l2_state?.pledge_pct as any" />
      <StatChip label="Interest coverage" :value="data.l2_state?.interest_coverage as any" />
      <StatChip label="3m return" :value="formatPct(data.technicals?.return_3m_pct as any)" />
      <StatChip label="12m return" :value="formatPct(data.technicals?.return_12m_pct as any)" />
      <StatChip label="vs 200-DMA" :value="formatPct(data.technicals?.pct_vs_dma_200 as any)" />
      <StatChip label="Mean-reversion z" :value="data.technicals?.z_score_vs_200d_mean as any" />
      <StatChip label="Delivery % (20d vs 1y)" :value="deliveryLabel" />
      <StatChip label="Band hits 60d (up / down)" :value="bandLabel" />
    </section>

    <section v-if="data.sector_context" class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center gap-2">
        <h2 class="text-sm font-semibold text-slate-700">{{ data.sector_context.sector_name }}</h2>
        <BadgePill v-if="data.sector_context.phase" :label="data.sector_context.phase as string" :tone="phaseTone[data.sector_context.phase as string] || 'neutral'" />
      </div>
      <p class="mt-1 text-xs text-slate-500">
        capacity growth {{ formatPct(data.sector_context.capacity_growth_pct as any) }} · demand growth {{ formatPct(data.sector_context.demand_growth_pct as any) }}
      </p>
      <p v-if="data.sector_context.phase" class="mt-1 text-[11px] text-slate-400">
        Capacity is gross-block-derived -- weak proxy for asset-light sectors (e.g. Financial Services, IT/Services).
      </p>
    </section>

    <!-- Structured pointers (2026-08-13): typed facts, not free-text tags -- promoter/
         institutional direction, rating agency+action, investor tier, sector growth. -->
    <section v-if="otherSignalPointers.length" class="rounded-lg border border-slate-200 bg-white p-4">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Signals</h2>
      <ul class="mt-3 space-y-2">
        <li v-for="p in otherSignalPointers" :key="`${p.signal_type}-${p.value}`" class="flex items-center justify-between gap-3 text-sm">
          <span class="text-slate-700">{{ p.label }}</span>
          <div class="flex items-center gap-2">
            <BadgePill v-if="p.direction" :label="p.direction" :tone="directionTone(p.direction)" />
            <span class="text-xs text-slate-400">{{ formatDate(p.as_of_date) }}</span>
          </div>
        </li>
      </ul>
    </section>

    <!-- The path: every piece of evidence that led here -->
    <section>
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Evidence trail</h2>
      <div class="mt-3">
        <AlertTimeline :alerts="data.alerts" />
      </div>
    </section>
  </div>
</template>
