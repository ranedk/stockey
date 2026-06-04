<script setup lang="ts">
import type { Dict, TraceSummary } from '~/types/api'

const api = useOperatorApi()
const actionLimit = ref(25)
const actionOffset = ref(0)
const actionSymbol = ref('')
const actionType = ref('ALL')
const actionStatus = ref('all')
const actionSearch = ref('')
const portfolioLimit = ref(25)
const portfolioOffset = ref(0)
const portfolioSymbol = ref('')
const portfolioStatus = ref('all')
const portfolioSearch = ref('')
const portfolioBucket = ref('today_recommendations')

const [{ data: marketContext }, { data: technicalCalibration }, { data: signalRefresh, error: signalRefreshError }] = await Promise.all([
  useAsyncData('market-context', () => api.getMarketContext(20)),
  useAsyncData('technical-calibration-home', () => api.getTechnicalCalibration(3)),
  useAsyncData('signal-refresh-home', () => api.getSignalRefresh({ limit: 12, compact: true }))
])

const { data: home } = useAsyncData('home', () => api.getHome(), {
  lazy: true,
  server: false
})
const { data: actionsData, refresh: refreshActions, error: actionsError } = useAsyncData('home-actions-paged', () => api.getActions({
  limit: actionLimit.value,
  offset: actionOffset.value,
  symbol: actionSymbol.value.trim().toUpperCase(),
  action: actionType.value,
  status: actionStatus.value,
  search: actionSearch.value.trim(),
  compact: true
}), {
  lazy: true,
  server: false,
  watch: [actionLimit, actionOffset, actionType, actionStatus]
})
const { data: portfolioData, refresh: refreshPortfolio, error: portfolioError } = useAsyncData('home-portfolio-paged', () => api.getPortfolio({
  limit: portfolioLimit.value,
  offset: portfolioOffset.value,
  symbol: portfolioSymbol.value.trim().toUpperCase(),
  status: portfolioStatus.value,
  search: portfolioSearch.value.trim(),
  compact: true
}), {
  lazy: true,
  server: false,
  watch: [portfolioLimit, portfolioOffset, portfolioStatus]
})

const summaryValues = computed(() => home.value?.summary || {})
const liveSignals = computed(() => signalRefresh.value?.signals || [])
const signalMeta = computed(() => asDict(signalRefresh.value?.meta?.signals))
const topActions = computed(() => [
  ...(actionsData.value?.top_action_recommendations || []),
  ...(actionsData.value?.action_recommendations || []),
  ...(actionsData.value?.alerts || [])
])
const actionMeta = computed(() => asDict(actionsData.value?.meta?.action_recommendations))
const actionQueueMeta = computed(() => {
  const topMeta = asDict(actionsData.value?.meta?.top_action_recommendations)
  const rowMeta = asDict(actionsData.value?.meta?.action_recommendations)
  const alertMeta = asDict(actionsData.value?.meta?.alerts)
  const returned = Number(topMeta.returned || 0) + Number(rowMeta.returned || 0) + Number(alertMeta.returned || 0)
  const total = Number(topMeta.total || 0) + Number(rowMeta.total || 0) + Number(alertMeta.total || 0)
  return { returned, total }
})
const portfolioMeta = computed(() => asDict(portfolioData.value?.meta?.portfolio))
const today = computed(() => portfolioRowsForBucket(portfolioBucket.value))
const snapshotMeta = computed(() => asDict(home.value?.snapshot || actionsData.value?.snapshot || portfolioData.value?.snapshot))
const snapshotIsStale = computed(() => String(snapshotMeta.value.freshness || '').toLowerCase() === 'stale')
const snapshotGeneratedAt = computed(() => String(snapshotMeta.value.generated_at || home.value?.generated_at || '-'))
const snapshotAgeText = computed(() => ageText(snapshotMeta.value.age_seconds))
const marketSummary = computed(() => marketContext.value?.summary || {})
const marketLeaders = computed(() => marketContext.value?.top_universe || [])
const calibrationSummary = computed(() => technicalCalibration.value?.summary || [])
const symbolTraces = reactive<Record<string, TraceSummary>>({})
const loadingSymbolTrace = reactive<Record<string, boolean>>({})
const portfolioBuckets = [
  { key: 'today_recommendations', label: 'Today' },
  { key: 'current_recommendations', label: 'Current' },
  { key: 'portfolio', label: 'Portfolio' },
  { key: 'lifecycle', label: 'Lifecycle' },
  { key: 'exited_recommendations', label: 'Exited' }
]

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}

function portfolioRowsForBucket(bucket: string): Dict[] {
  const payload = portfolioData.value as Record<string, unknown> | null | undefined
  const rows = payload?.[bucket]
  return Array.isArray(rows) ? rows.filter((row) => typeof row === 'object' && row !== null) as Dict[] : []
}

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

function priceText(value: unknown) {
  const num = Number(value)
  if (!Number.isFinite(num)) return '-'
  return Intl.NumberFormat('en-IN', { maximumFractionDigits: 2 }).format(num)
}

function ageText(value: unknown) {
  const seconds = Number(value)
  if (!Number.isFinite(seconds)) return '-'
  if (seconds < 60) return `${Math.round(seconds)}s old`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m old`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h old`
  return `${Math.round((seconds / 86400) * 10) / 10}d old`
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

async function applyActionFilters() {
  actionOffset.value = 0
  await refreshActions()
}

async function applyPortfolioFilters() {
  portfolioOffset.value = 0
  await refreshPortfolio()
}

function loadNextActions() {
  const next = Number(actionMeta.value.next_offset)
  if (Number.isFinite(next)) actionOffset.value = next
}

function loadNextPortfolio() {
  const next = Number(portfolioMeta.value.next_offset)
  if (Number.isFinite(next)) portfolioOffset.value = next
}

function bucketCount(bucket: string) {
  const meta = asDict(portfolioData.value?.meta?.[bucket])
  return Number(meta.total ?? portfolioRowsForBucket(bucket).length)
}

function actionDetailPath(row: Record<string, unknown>) {
  const params = new URLSearchParams()
  if (row.symbol) params.set('symbol', String(row.symbol))
  if (row.unique_id) params.set('unique_id', String(row.unique_id))
  if (row.setup_id) params.set('setup_id', String(row.setup_id))
  const query = params.toString()
  return `/api/actions/detail${query ? `?${query}` : ''}`
}

function nestedValue(source: unknown, path: string[]): unknown {
  let current = source
  for (const key of path) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return null
    current = (current as Dict)[key]
  }
  return current
}

function firstActionValue(row: Record<string, unknown>, keys: string[], nestedPaths: string[][] = []) {
  for (const key of keys) {
    const value = row[key]
    if (value !== null && value !== undefined && value !== '') return value
  }
  for (const path of nestedPaths) {
    const value = nestedValue(row, path)
    if (value !== null && value !== undefined && value !== '') return value
  }
  return null
}

function actionPriceFacts(row: Record<string, unknown>) {
  const latest = firstActionValue(row, ['current_price', 'last_price', 'reference_price'], [
    ['recommendation_reason', 'evidence', 'risk', 'reference_price']
  ])
  const trigger = firstActionValue(row, ['trigger_price', 'pivot_price'], [
    ['recommendation_reason', 'evidence', 'technical', 'trigger_price'],
    ['recommendation_reason', 'evidence', 'technical', 'pivot_price']
  ])
  const zoneLow = firstActionValue(row, ['attractive_price_low'])
  const zoneHigh = firstActionValue(row, ['attractive_price_high'])
  const entry = firstActionValue(row, ['entry_price'])
  const stop = firstActionValue(row, ['stop_price', 'recommended_stop_price', 'invalidation_price'], [
    ['recommendation_reason', 'evidence', 'risk', 'stop_price'],
    ['recommendation_reason', 'evidence', 'risk', 'recommended_stop_price'],
    ['recommendation_reason', 'evidence', 'risk', 'invalidation_price']
  ])
  const target = firstActionValue(row, ['target_price', 'recommended_target_price'])
  const facts = [
    { label: 'Latest', value: latest, tone: 'bg-ink text-paper' },
    { label: 'Trigger', value: trigger, tone: 'bg-white text-ink/75' },
    { label: 'Entry', value: entry, tone: 'bg-white text-ink/75' },
    { label: 'Stop', value: stop, tone: 'bg-rust/10 text-rust' },
    { label: 'Target', value: target, tone: 'bg-moss/10 text-moss' }
  ].filter((item) => item.value !== null && item.value !== undefined && item.value !== '')
  if (zoneLow !== null && zoneLow !== undefined && zoneLow !== '' && zoneHigh !== null && zoneHigh !== undefined && zoneHigh !== '') {
    facts.splice(1, 0, { label: 'Zone', value: `${priceText(zoneLow)} - ${priceText(zoneHigh)}`, tone: 'bg-sun/20 text-ink' })
  }
  return facts
}

function portfolioDetailPath(row: Record<string, unknown>) {
  const symbol = String(row.symbol || row.ticker || '').toUpperCase()
  return symbol ? `/api/portfolio/${encodeURIComponent(symbol)}/detail` : ''
}

function signalTone(row: Record<string, unknown>) {
  const status = String(row.signal_status || row.signal_action || '').toLowerCase()
  if (status.includes('exit') || status.includes('reduce') || status.includes('sell')) return 'bg-rust text-white'
  if (status.includes('entry') || status.includes('buy')) return 'bg-moss text-white'
  if (status.includes('review') || status.includes('watch')) return 'bg-sun text-ink'
  return 'bg-white text-ink/70'
}
</script>

<template>
  <section class="rounded-[2rem] bg-ink p-8 text-paper shadow-soft">
    <p class="text-sm font-semibold uppercase tracking-[0.35em] text-paper/55">Read-only operator view</p>
    <h1 class="mt-4 max-w-4xl text-5xl font-black tracking-tight">See actions first. Expand the evidence when something changes.</h1>
    <p class="mt-5 max-w-3xl text-lg leading-8 text-paper/70">
      Generated {{ home?.generated_at || '-' }} for {{ home?.asof_date || 'latest available date' }}.
    </p>
  </section>

  <section
    v-if="snapshotMeta.generated_at || snapshotMeta.source"
    class="mt-4 rounded-3xl border p-4 shadow-soft"
    :class="snapshotIsStale ? 'border-sun/60 bg-sun/20 text-ink' : 'border-moss/20 bg-moss/10 text-ink/70'"
  >
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <p class="text-xs font-black uppercase tracking-[0.28em]" :class="snapshotIsStale ? 'text-rust' : 'text-moss'">
          {{ snapshotIsStale ? 'Stale operator snapshot' : 'Fresh operator snapshot' }}
        </p>
        <p class="mt-1 text-sm font-semibold">
          Created {{ snapshotGeneratedAt }} · {{ snapshotAgeText }} · source {{ snapshotMeta.source || 'unknown' }}
        </p>
      </div>
      <p v-if="snapshotIsStale" class="max-w-2xl text-sm leading-6 text-ink/65">
        The API is serving the latest cached operator snapshot because a fresh snapshot is missing. Run `python -m advisory.operator_snapshot` or wait for the next advisory/watchers cycle.
      </p>
    </div>
  </section>

  <section class="mt-6 grid gap-4 md:grid-cols-4">
    <MetricTile label="Actions" :value="String(summaryValues.action_count || 0)" note="Resolved buy/sell/review queue" />
    <MetricTile label="Today" :value="String(summaryValues.today_count || 0)" note="Latest recommendation date" />
    <MetricTile label="Watch" :value="String(summaryValues.watch_count || 0)" note="Waiting for trigger or confirmation" />
    <MetricTile label="Alerts" :value="String(summaryValues.alert_count || 0)" note="Live watcher alerts" />
  </section>

  <section class="mt-8 glass-panel rounded-3xl p-6">
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p class="text-xs font-bold uppercase tracking-[0.3em] text-ink/45">Live Signal Refresh</p>
        <h2 class="mt-2 text-2xl font-black">Watcher-triggered symbol decisions</h2>
        <p class="mt-2 max-w-3xl text-sm leading-6 text-ink/60">
          These are fast symbol-scoped signals from OHLCV/news/announcement watcher routes. The daily advisory remains the authoritative reconciliation.
        </p>
      </div>
      <span class="rounded-full bg-white px-3 py-1 text-xs font-black text-ink/60">
        {{ signalMeta.total || 0 }} recent
      </span>
    </div>
    <ApiErrorBanner v-if="signalRefreshError" class="mt-4" title="Live signal refresh failed" :error="signalRefreshError" />
    <div v-if="liveSignals.length" class="mt-5 grid gap-3 xl:grid-cols-3">
      <article v-for="row in liveSignals" :key="String(row.refresh_id || `${row.symbol}-${row.refreshed_at}`)" class="rounded-3xl bg-white/75 p-4 shadow-soft">
        <div class="flex items-start justify-between gap-3">
          <div>
            <SymbolLink :symbol="row.symbol" />
            <p class="mt-1 text-xs font-semibold text-ink/45">{{ row.refreshed_at || row.load_ts || '-' }}</p>
          </div>
          <span class="rounded-full px-3 py-1 text-xs font-black" :class="signalTone(row)">
            {{ row.signal_action || 'NO_CHANGE' }}
          </span>
        </div>
        <p class="mt-3 text-sm leading-6 text-ink/70">{{ row.action_reason || 'No reason captured.' }}</p>
        <div class="mt-3 flex flex-wrap gap-2 text-xs font-bold text-ink/50">
          <span class="rounded-full bg-paper px-3 py-1">status: {{ row.signal_status || '-' }}</span>
          <span class="rounded-full bg-paper px-3 py-1">source: {{ row.signal_source || '-' }}</span>
          <span class="rounded-full bg-paper px-3 py-1">reason: {{ row.reason || '-' }}</span>
        </div>
      </article>
    </div>
    <p v-else class="mt-5 rounded-2xl bg-white/70 p-4 text-sm font-semibold text-ink/55">
      No live signal-refresh rows yet. Run `./all_watchers.sh` or `python -m advisory.signal_refresh --symbol RELIANCE --reason manual`.
    </p>
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
              <SymbolLink :symbol="row.symbol" />
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
      <div class="mb-3 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 class="text-xl font-black">Action Queue</h2>
          <p class="mt-1 text-sm font-semibold text-ink/50">
            Showing {{ actionQueueMeta.returned || topActions.length }} of {{ actionQueueMeta.total || topActions.length }} action rows.
          </p>
        </div>
      </div>
      <ApiErrorBanner v-if="actionsError" class="mb-3" title="Action queue failed" :error="actionsError" />
      <div class="mb-4 glass-panel rounded-3xl p-4">
        <div class="grid gap-3 md:grid-cols-[0.75fr_0.75fr_0.75fr_1fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Symbol
            <input v-model="actionSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyActionFilters" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Action
            <select v-model="actionType" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="ALL">All</option>
              <option value="BUY">Buy</option>
              <option value="SELL">Sell</option>
              <option value="EXIT">Exit</option>
              <option value="HOLD">Hold</option>
              <option value="WATCH">Watch</option>
              <option value="MANUAL">Manual</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Status
            <select v-model="actionStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="all">All</option>
              <option value="error">Error</option>
              <option value="manual">Manual</option>
              <option value="approved">Approved</option>
              <option value="blocked">Blocked</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Search
            <input v-model="actionSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="reason, setup, event..." @keyup.enter="applyActionFilters" />
          </label>
          <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="applyActionFilters">
            Apply
          </button>
        </div>
      </div>
      <div class="space-y-3">
        <RecordCard
          v-for="(row, idx) in topActions"
          :key="idx"
          :title="String(row.action_code || row.action || row.alert_type || row.source_type || 'Action')"
          :subtitle="String(row.action_reason || row.next_action_reason || row.reason || '')"
          :record="row"
          :detail-path="actionDetailPath(row)"
        >
          <template #badge>
            <div class="flex flex-wrap gap-2">
              <SymbolLink :symbol="row.symbol" subtle />
              <span class="rounded-full bg-moss px-3 py-1 text-xs font-bold text-white">{{ row.action_code || row.action || row.alert_type || 'ALERT' }}</span>
            </div>
          </template>
          <div class="mt-4 grid gap-2 text-xs font-black sm:grid-cols-2 xl:grid-cols-3">
            <p
              v-for="fact in actionPriceFacts(row)"
              :key="`${fact.label}-${String(fact.value)}`"
              class="rounded-2xl px-3 py-2"
              :class="fact.tone"
            >
              <span class="block uppercase tracking-[0.18em] opacity-60">{{ fact.label }}</span>
              <span class="mt-1 block text-sm">{{ fact.label === 'Zone' ? fact.value : priceText(fact.value) }}</span>
            </p>
            <p v-if="row.pnl_pct !== null && row.pnl_pct !== undefined && row.pnl_pct !== ''" class="rounded-2xl bg-white px-3 py-2 text-ink/75">
              <span class="block uppercase tracking-[0.18em] opacity-60">P&L</span>
              <span class="mt-1 block text-sm">{{ pct(row.pnl_pct) }}</span>
            </p>
          </div>
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <TechnicalDecisionPanel class="mt-4" :record="row" compact />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <NuxtLink
            class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
            :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open symbol page
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
      <button v-if="actionMeta.has_more" class="mt-4 rounded-full bg-moss px-5 py-3 text-sm font-black text-paper" type="button" @click="loadNextActions">
        Show next {{ actionLimit }} actions
      </button>
    </div>
    <div>
      <div class="mb-3">
        <h2 class="text-xl font-black">Portfolio & Recommendations</h2>
        <p class="mt-1 text-sm font-semibold text-ink/50">
          {{ portfolioBuckets.find((item) => item.key === portfolioBucket)?.label || 'Selected' }}: {{ today.length }} shown.
        </p>
      </div>
      <ApiErrorBanner v-if="portfolioError" class="mb-3" title="Portfolio payload failed" :error="portfolioError" />
      <div class="mb-4 glass-panel rounded-3xl p-4">
        <div class="flex flex-wrap gap-2">
          <button
            v-for="bucket in portfolioBuckets"
            :key="bucket.key"
            class="rounded-full px-3 py-2 text-xs font-black"
            :class="portfolioBucket === bucket.key ? 'bg-ink text-paper' : 'bg-white/80 text-ink/60'"
            type="button"
            @click="portfolioBucket = bucket.key"
          >
            {{ bucket.label }} · {{ bucketCount(bucket.key) }}
          </button>
        </div>
        <div class="mt-3 grid gap-3 md:grid-cols-[0.75fr_0.75fr_1fr_auto]">
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Symbol
            <input v-model="portfolioSymbol" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="Optional" @keyup.enter="applyPortfolioFilters" />
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Status
            <select v-model="portfolioStatus" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss">
              <option value="all">All</option>
              <option value="current">Current</option>
              <option value="exit">Exit</option>
              <option value="manual">Manual</option>
              <option value="watch">Watch</option>
            </select>
          </label>
          <label class="grid gap-1 text-xs font-black uppercase tracking-[0.18em] text-ink/45">
            Search
            <input v-model="portfolioSearch" class="rounded-xl border border-black/10 bg-white/80 px-3 py-2 text-sm normal-case tracking-normal text-ink outline-none focus:border-moss" placeholder="reason, bucket, exit..." @keyup.enter="applyPortfolioFilters" />
          </label>
          <button class="self-end rounded-full bg-ink px-4 py-2 text-sm font-black text-paper" type="button" @click="applyPortfolioFilters">
            Apply
          </button>
        </div>
      </div>
      <div class="space-y-3">
        <RecordCard
          v-for="(row, idx) in today"
          :key="idx"
          :title="String(row.action_summary || row.kind || 'Recommendation')"
          :subtitle="String(row.reason || row.action_summary || row.bucket_summary || '')"
          :record="row"
          :detail-path="portfolioDetailPath(row)"
        >
          <template #badge>
            <SymbolLink :symbol="row.symbol" subtle />
          </template>
          <ReasonContractPanel
            v-if="row.recommendation_reason || row.reason_contract_status"
            class="mt-4"
            :contract="row.recommendation_reason"
            :status="row.reason_contract_status"
            compact
          />
          <TechnicalDecisionPanel class="mt-4" :record="row" compact />
          <button
            class="mt-4 rounded-full bg-ink px-4 py-2 text-sm font-bold text-paper"
            type="button"
            @click="loadSymbolTrace(row)"
          >
            {{ loadingSymbolTrace[String(row.symbol || '').toUpperCase()] ? 'Loading trace...' : 'Load symbol trace' }}
          </button>
          <NuxtLink
            class="ml-2 inline-flex rounded-full bg-white px-4 py-2 text-sm font-bold text-ink"
            :to="`/symbols/${encodeURIComponent(String(row.symbol || '').toUpperCase())}`"
          >
            Open symbol page
          </NuxtLink>
          <TraceTimeline
            v-if="symbolTraces[String(row.symbol || '').toUpperCase()]"
            class="mt-4"
            :trace="symbolTraces[String(row.symbol || '').toUpperCase()]"
            title="Symbol trace"
          />
        </RecordCard>
        <p v-if="!today.length" class="glass-panel rounded-3xl p-6 text-ink/60">No rows for this portfolio bucket and filter.</p>
      </div>
      <button v-if="portfolioBucket === 'portfolio' && portfolioMeta.has_more" class="mt-4 rounded-full bg-moss px-5 py-3 text-sm font-black text-paper" type="button" @click="loadNextPortfolio">
        Show next {{ portfolioLimit }} portfolio rows
      </button>
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
