<script setup lang="ts">
import type { TraceSummary } from '~/types/api'

const api = useOperatorApi()
const [{ data: summary }, { data: actions }, { data: portfolio }, { data: marketContext }, { data: technicalCalibration }] = await Promise.all([
  useAsyncData('summary', () => api.getSummary()),
  useAsyncData('actions', () => api.getActions()),
  useAsyncData('portfolio', () => api.getPortfolio()),
  useAsyncData('market-context', () => api.getMarketContext(20)),
  useAsyncData('technical-calibration-home', () => api.getTechnicalCalibration(3))
])

const summaryValues = computed(() => summary.value?.summary || {})
const topActions = computed(() => actions.value?.top_action_recommendations || [])
const today = computed(() => portfolio.value?.today_recommendations || [])
const marketSummary = computed(() => marketContext.value?.summary || {})
const marketLeaders = computed(() => marketContext.value?.top_universe || [])
const calibrationSummary = computed(() => technicalCalibration.value?.summary || [])
const symbolTraces = reactive<Record<string, TraceSummary>>({})
const loadingSymbolTrace = reactive<Record<string, boolean>>({})

function pct(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return '-'
  return `${Math.round(num * 10) / 10}%`
}

function numberText(value: unknown) {
  const num = Number(value)
  if (Number.isNaN(num)) return String(value || '-')
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 1 }).format(num)
}

async function loadSymbolTrace(row: Record<string, unknown>) {
  const symbol = String(row.symbol || '').toUpperCase()
  if (!symbol || symbolTraces[symbol] || loadingSymbolTrace[symbol]) return
  loadingSymbolTrace[symbol] = true
  try {
    symbolTraces[symbol] = await api.getSymbolTraceSummary(symbol, 100)
  } finally {
    loadingSymbolTrace[symbol] = false
  }
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Read-only operator view</p>
    <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">See actions first. Expand the evidence when something changes.</h1>
    <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
      Generated {{ summary?.generated_at || '-' }} for {{ summary?.asof_date || 'latest available date' }}.
    </p>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Actions" :value="String(summaryValues.action_count || 0)" note="Resolved buy/sell/review queue" />
    <MetricTile label="Today" :value="String(summaryValues.today_count || 0)" note="Latest recommendation date" />
    <MetricTile label="Watch" :value="String(summaryValues.watch_count || 0)" note="Waiting for trigger or confirmation" />
    <MetricTile label="Alerts" :value="String(summaryValues.alert_count || 0)" note="Live watcher alerts" />
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Market Context: Top 50%</p>
        <h2 class="mt-2 text-2xl font-black">Broad tape before stock-specific conviction</h2>
        <p class="mt-2 max-w-4xl text-sm leading-6 text-ink/60">
          {{ marketSummary.summary_text || 'No market-context summary has been generated yet. Run the advisory pipeline through the market_context stage.' }}
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ marketSummary.regime_name || 'UNKNOWN' }}
      </span>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-5">
      <MetricTile label="Top Names" :value="String(marketSummary.top_context_count || 0)" note="Tracked context universe" />
      <MetricTile label="Above 50DMA" :value="pct(marketSummary.breadth_above_dma50_pct)" note="Breadth inside top context" />
      <MetricTile label="Trend Aligned" :value="pct(marketSummary.breadth_trend_alignment_pct)" note="Healthy leadership share" />
      <MetricTile label="RS Positive" :value="pct(marketSummary.breadth_rs_positive_pct)" note="Outperforming benchmark" />
      <MetricTile label="Events" :value="String((Number(marketSummary.news_event_count_7d || 0) + Number(marketSummary.announcement_event_count_7d || 0)) || 0)" note="News + announcements, 7d" />
    </div>
    <details class="mt-5">
      <summary class="cursor-pointer text-sm font-black text-moss">Show top market-context leaders</summary>
      <div class="mt-3 grid gap-3 lg:grid-cols-2">
        <article v-for="row in marketLeaders.slice(0, 10)" :key="String(row.symbol)" class="rounded-2xl bg-white/70 p-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <p class="font-black text-ink">{{ row.symbol }}</p>
              <p class="text-xs text-ink/50">{{ row.sector_code || 'sector n/a' }} · rank {{ row.context_rank || '-' }}</p>
            </div>
            <span class="rounded-full bg-moss px-3 py-1 text-xs font-bold text-white">
              {{ numberText(Number(row.context_rank_score || 0) * 100) }}
            </span>
          </div>
          <div class="mt-3 grid gap-2 text-sm md:grid-cols-3">
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Liquidity:</b> {{ numberText(row.avg_traded_value_20d) }}</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>RS:</b> {{ numberText(Number(row.rs_vs_benchmark || 0) * 100) }}%</p>
            <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Events:</b> {{ Number(row.news_event_count_7d || 0) + Number(row.announcement_event_count_7d || 0) }}</p>
          </div>
        </article>
      </div>
    </details>
  </section>

  <section class="mt-8 grid gap-6 lg:grid-cols-[1.1fr_0.9fr]">
    <div>
      <h2 class="mb-3 text-xl font-black">Action Queue</h2>
      <div class="space-y-3">
        <RecordCard
          v-for="(row, idx) in topActions"
          :key="idx"
          :title="String(row.symbol || row.action_code || 'Action')"
          :subtitle="String(row.action_reason || row.next_action_reason || row.reason || '')"
          :record="row"
        >
          <template #badge>
            <span class="rounded-full bg-moss px-3 py-1 text-xs font-bold text-white">{{ row.action_code || row.action || 'ACTION' }}</span>
          </template>
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <NuxtLink
            class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
            :to="`/decision-trace?symbol=${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open trace page
          </NuxtLink>
          <TraceTimeline
            v-if="symbolTraces[String(row.symbol || '').toUpperCase()]"
            class="mt-4"
            :trace="symbolTraces[String(row.symbol || '').toUpperCase()]"
            title="Symbol trace"
          />
        </RecordCard>
        <p v-if="!topActions.length" class="glass-panel rounded-3xl p-6 text-ink/60">No current action rows.</p>
      </div>
    </div>
    <div>
      <h2 class="mb-3 text-xl font-black">Today's Recommendations</h2>
      <div class="space-y-3">
        <RecordCard
          v-for="(row, idx) in today"
          :key="idx"
          :title="String(row.symbol || 'Recommendation')"
          :subtitle="String(row.reason || row.action_summary || row.bucket_summary || '')"
          :record="row"
        >
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <NuxtLink
            class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
            :to="`/decision-trace?symbol=${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open trace page
          </NuxtLink>
          <TraceTimeline
            v-if="symbolTraces[String(row.symbol || '').toUpperCase()]"
            class="mt-4"
            :trace="symbolTraces[String(row.symbol || '').toUpperCase()]"
            title="Symbol trace"
          />
        </RecordCard>
        <p v-if="!today.length" class="glass-panel rounded-3xl p-6 text-ink/60">No recommendations for the latest date.</p>
      </div>
    </div>
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-start justify-between gap-4">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Technical Calibration</p>
        <h2 class="mt-2 text-2xl font-black">Thresholds are research evidence, not auto-promoted</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          Latest realized-outcome checks for the swing technical engine. Review sample size, hit rate after costs, and average return before changing setup thresholds.
        </p>
      </div>
      <NuxtLink class="rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper" to="/technical-calibration">
        Open calibration
      </NuxtLink>
      <NuxtLink class="rounded-full bg-white px-4 py-2 text-sm font-bold text-ink" to="/events">
        Open event policy
      </NuxtLink>
    </div>
    <div class="mt-5 grid gap-3 md:grid-cols-3">
      <article v-for="row in calibrationSummary.slice(0, 3)" :key="String(row.horizon_days)" class="rounded-2xl bg-white/70 p-4">
        <p class="text-xs font-black uppercase tracking-[0.2em] text-ink/45">{{ row.horizon_days }} day horizon</p>
        <p class="mt-2 font-black text-ink">{{ row.best_config_id || 'No config yet' }}</p>
        <div class="mt-3 grid gap-2 text-sm">
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Eligible:</b> {{ numberText(row.best_eligible_count) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Hit:</b> {{ pct(Number(row.best_hit_rate_after_cost || 0)) }}</p>
          <p class="rounded-xl bg-paper/70 px-3 py-2"><b>Avg return:</b> {{ pct(Number(row.best_avg_forward_return_after_cost || 0)) }}</p>
        </div>
      </article>
      <p v-if="!calibrationSummary.length" class="rounded-2xl bg-white/70 p-4 text-sm text-ink/60">
        No calibration rows yet. Run the technical threshold calibration script.
      </p>
    </div>
  </section>
</template>
