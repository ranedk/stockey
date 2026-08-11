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

const showAddForm = ref(false)
const resolvingThesisId = ref<string | null>(null)

const openThesis = computed(() => data.value?.portfolio.find((t) => t.status === 'open') ?? null)
const mostRecentAlert = computed(() => {
  const alerts = data.value?.alerts ?? []
  return alerts.length ? alerts[alerts.length - 1] : null
})

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

async function onCreated() {
  showAddForm.value = false
  await refresh()
}
async function onResolved() {
  resolvingThesisId.value = null
  await refresh()
}
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
        <h1 class="text-2xl font-semibold">{{ data.watchlist.company_master_id.replace('nse:', '') }}</h1>
        <p class="text-slate-600">{{ data.watchlist.company_name }}</p>
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

    <!-- Portfolio: the one manual action -->
    <section class="rounded-lg border border-slate-200 bg-white p-5">
      <h2 class="text-sm font-semibold uppercase tracking-wide text-slate-500">Portfolio</h2>

      <div v-if="openThesis" class="mt-3">
        <BadgePill label="open position" tone="good" />
        <p class="mt-2 text-sm text-slate-800">{{ openThesis.prediction_text }}</p>
        <p class="mt-1 text-xs text-slate-500">target: {{ formatDate(openThesis.target_date) }} · invalidation: {{ openThesis.invalidation_criteria }}</p>
        <button
          v-if="resolvingThesisId !== openThesis.thesis_id"
          class="mt-3 rounded border border-slate-300 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50"
          @click="resolvingThesisId = openThesis.thesis_id"
        >
          Remove from portfolio
        </button>
        <PortfolioResolveForm
          v-else
          class="mt-3"
          :thesis-id="openThesis.thesis_id"
          @resolved="onResolved"
          @cancel="resolvingThesisId = null"
        />
      </div>

      <div v-else-if="!showAddForm" class="mt-3">
        <p class="text-sm text-slate-500">Not in your portfolio.</p>
        <button class="mt-2 rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white" @click="showAddForm = true">
          Add to portfolio
        </button>
      </div>
      <PortfolioAddForm
        v-else
        class="mt-3"
        :company-master-id="data.watchlist.company_master_id"
        :prefill-text="data.watchlist.narrative_text || ''"
        :source-alert="mostRecentAlert ? { source: mostRecentAlert.source, newsId: mostRecentAlert.news_id, triggerType: mostRecentAlert.trigger_type } : null"
        @created="onCreated"
        @cancel="showAddForm = false"
      />

      <div v-if="data.portfolio.filter((t) => t.status !== 'open').length" class="mt-4 border-t border-slate-100 pt-3">
        <p class="text-xs font-medium text-slate-500">History</p>
        <ul class="mt-2 space-y-1 text-xs text-slate-500">
          <li v-for="t in data.portfolio.filter((th) => th.status !== 'open')" :key="t.thesis_id">
            {{ formatDate(t.created_date) }} → {{ formatDate(t.resolution_date) }}:
            {{ t.resolved_true ? 'came true' : 'did not come true' }}
            <span v-if="t.failure_attribution">({{ t.failure_attribution }})</span>
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
    </section>

    <section v-if="data.sector_context" class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center gap-2">
        <h2 class="text-sm font-semibold text-slate-700">{{ data.sector_context.sector_name }}</h2>
        <BadgePill v-if="data.sector_context.phase" :label="data.sector_context.phase as string" :tone="phaseTone[data.sector_context.phase as string] || 'neutral'" />
      </div>
      <p class="mt-1 text-xs text-slate-500">
        capacity growth {{ formatPct(data.sector_context.capacity_growth_pct as any) }} · demand growth {{ formatPct(data.sector_context.demand_growth_pct as any) }}
      </p>
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
