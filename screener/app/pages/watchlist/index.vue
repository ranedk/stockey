<script setup lang="ts">
import type { WatchlistItem, WatchlistReturnSummary, WatchlistStatus } from '~/types/api'

const STATUS_TABS: { value: WatchlistStatus | 'all'; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'stale', label: 'Stale' },
  { value: 'invalidated', label: 'Invalidated' },
  { value: 'price_flagged', label: 'Price flagged' },
  { value: 'all', label: 'All' },
]

const selectedStatus = ref<WatchlistStatus | 'all'>('active')
const minConfluence = ref(0)

const api = useApi()
const { data, status, error } = await useAsyncData(
  () => `watchlist-${selectedStatus.value}`,
  () => api.get<WatchlistItem[]>(`/api/watchlist?status=${selectedStatus.value}`),
  { watch: [selectedStatus] },
)

const filteredData = computed(() => {
  if (!data.value || minConfluence.value === 0) return data.value
  return data.value.filter(item => (item.confluence_count ?? -1) >= minConfluence.value)
})


// Equal-weight average return across the current tab's bucket -- a directional
// gut-check ("is this list net up or down"), not a real portfolio return. Fetched
// separately (not derived client-side) so the exclusion rules -- e.g. the
// DBCORP-class "no fresh price yet" coincidence -- live in exactly one place
// (fundamentals/api/queries.py's get_watchlist_return_summary).
const { data: summary } = await useAsyncData(
  () => `watchlist-summary-${selectedStatus.value}`,
  () => api.get<WatchlistReturnSummary>(`/api/watchlist/summary?status=${selectedStatus.value}`),
  { watch: [selectedStatus] },
)

function priceDeltaTone(item: WatchlistItem): 'good' | 'bad' | 'neutral' {
  const pct = priceChangePct(item.first_seen_price, item.current_price)
  if (pct === null) return 'neutral'
  return pct >= 0 ? 'good' : 'bad'
}

function summaryTone(pct: number | null): 'good' | 'bad' | 'neutral' {
  if (pct === null) return 'neutral'
  return pct >= 0 ? 'good' : 'bad'
}

const emptyStateMessage = computed(() => {
  if (selectedStatus.value === 'all') return 'Nothing on the watchlist yet.'
  return `Nothing ${WATCHLIST_STATUS_LABELS[selectedStatus.value].toLowerCase()}.`
})
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Watchlist</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every company with an active fundamental alert. Added automatically -- nothing
      here has been reviewed by you yet, that's what the detail page is for.
      Companies leave the default view on a timeout, a contradicting signal, or a
    <PipelineNote layer="L2 × L3" title="Why a company appears here">
      <p>
        <strong>L2</strong> keeps a state vector for every L1 company — debt trajectory, capital
        work-in-progress, promoter and institutional stake, pledge trend, valuation against its own
        sector — answering one question in advance: <em>if an event hits this name, does it
        matter?</em>
      </p>
      <p class="mt-2">
        <strong>L3</strong> is the alert rule, and its whole design is one sentence: an alert fires
        on a trigger <em>against a primed L2 state</em>, never on a trigger alone. A rating action or
        an insider trade is not automatically interesting; it becomes interesting when it lines up
        with — or flatly contradicts — what the company's own state already suggested. Names arrive
        here automatically and leave on a timeout, a contradicting signal, or your own decision.
      </p>
    </PipelineNote>
      large price move -- never deleted, just out of the way until you look again.
    </p>

    <DraftThesesPanel />

    <div v-if="summary && summary.included_count > 0" class="mt-4 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-slate-50 px-4 py-3">
      <span class="text-sm text-slate-500">Equal-weight return since watching</span>
      <BadgePill :label="formatPct(summary.net_return_pct)" :tone="summaryTone(summary.net_return_pct)" />
      <span class="text-xs text-slate-400">
        {{ summary.included_count }} of {{ summary.total_count }} priced
        <template v-if="summary.excluded_count">· {{ summary.excluded_count }} excluded (no fresh price yet)</template>
      </span>
      <span class="basis-full text-xs italic text-slate-400">
        Directional only -- equal-weighted, not time-weighted, and drops off the list once a company exits this view.
      </span>
    </div>

    <div class="mt-4 flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 pb-1">
      <div class="flex gap-1">
        <button
          v-for="tab in STATUS_TABS"
          :key="tab.value"
          class="border-b-2 px-3 py-2 text-sm font-medium transition"
          :class="selectedStatus === tab.value ? 'border-slate-900 text-slate-900' : 'border-transparent text-slate-500 hover:text-slate-700'"
          @click="selectedStatus = tab.value"
        >
          {{ tab.label }}
        </button>
      </div>
      <label class="flex items-center gap-2 pb-2 text-sm text-slate-500">
        Min confluence
        <select v-model.number="minConfluence" class="rounded-md border border-slate-200 px-2 py-1 text-sm">
          <option :value="0">Any</option>
          <option v-for="n in [1, 2, 3, 4, 5]" :key="n" :value="n">{{ n }}+</option>
        </select>
      </label>
    </div>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="filteredData && filteredData.length === 0" class="mt-6 text-sm text-slate-500">
      {{ minConfluence > 0 ? `Nothing at ${minConfluence}+ confluence.` : emptyStateMessage }}
    </div>

    <div v-else-if="filteredData" class="mt-4 grid gap-3">
      <NuxtLink
        v-for="item in filteredData"
        :key="item.company_master_id"
        :to="`/watchlist/${encodeURIComponent(item.company_master_id)}`"
        class="block rounded-lg border border-slate-200 bg-white p-4 transition hover:border-slate-300 hover:shadow-sm"
      >
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-semibold">{{ item.company_master_id.replace('nse:', '') }}</span>
              <span class="text-sm text-slate-500">{{ item.company_name }}</span>
              <BadgePill :label="`${item.alert_count} event${item.alert_count === 1 ? '' : 's'}`" tone="neutral" />
              <BadgePill v-if="item.status !== 'active'" :label="WATCHLIST_STATUS_LABELS[item.status]" :tone="WATCHLIST_STATUS_TONE[item.status]" />
              <ConfluenceBadge
                v-if="item.confluence_count !== null"
                :supportive="item.confluence_count"
                :contradicting="item.contradicting_count"
                :evaluable="item.evaluable_count"
              />
            </div>
            <div v-if="item.strategies.length" class="mt-1.5 flex flex-wrap gap-1">
              <BadgePill v-for="s in item.strategies" :key="s" :label="strategyLabel(s)" tone="neutral" />
            </div>
            <p v-if="item.status_reason" class="mt-1.5 text-xs text-slate-500">{{ item.status_reason }}</p>
            <p v-if="item.narrative_text" class="mt-2 line-clamp-2 max-w-2xl text-sm text-slate-600">
              {{ item.narrative_text }}
            </p>
            <p v-else class="mt-2 text-sm italic text-slate-400">Narrative not generated yet.</p>
          </div>
          <div class="shrink-0 text-right">
            <div class="text-sm text-slate-500">watching since {{ formatDate(item.first_seen_at) }}</div>
            <div class="mt-1 flex items-center justify-end gap-2 text-sm">
              <span>{{ formatPrice(item.first_seen_price) }} → {{ formatPrice(item.current_price) }}</span>
              <BadgePill
                v-if="item.no_fresh_price_yet"
                label="no fresh price yet"
                tone="neutral"
              />
              <BadgePill v-else :label="formatPct(priceChangePct(item.first_seen_price, item.current_price))" :tone="priceDeltaTone(item)" />
              <BadgePill v-if="item.price_data_stale" label="stale" tone="warn" />
            </div>
            <div v-if="item.suggested_watch_until" class="mt-2 text-xs text-slate-400">
              suggested watch until {{ formatDate(item.suggested_watch_until) }}
            </div>
          </div>
        </div>
      </NuxtLink>
    </div>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
