<script setup lang="ts">
import type { WatchlistItem, WatchlistStatus } from '~/types/api'

const STATUS_TABS: { value: WatchlistStatus | 'all'; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'stale', label: 'Stale' },
  { value: 'invalidated', label: 'Invalidated' },
  { value: 'price_flagged', label: 'Price flagged' },
  { value: 'all', label: 'All' },
]

const selectedStatus = ref<WatchlistStatus | 'all'>('active')

const api = useApi()
const { data, status, error } = await useAsyncData(
  () => `watchlist-${selectedStatus.value}`,
  () => api.get<WatchlistItem[]>(`/api/watchlist?status=${selectedStatus.value}`),
  { watch: [selectedStatus] },
)

function priceDeltaTone(item: WatchlistItem): 'good' | 'bad' | 'neutral' {
  const pct = priceChangePct(item.first_seen_price, item.current_price)
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
      large price move -- never deleted, just out of the way until you look again.
    </p>

    <div class="mt-4 flex gap-1 border-b border-slate-200">
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

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="data && data.length === 0" class="mt-6 text-sm text-slate-500">
      {{ emptyStateMessage }}
    </div>

    <div v-else-if="data" class="mt-4 grid gap-3">
      <NuxtLink
        v-for="item in data"
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
              <BadgePill :label="formatPct(priceChangePct(item.first_seen_price, item.current_price))" :tone="priceDeltaTone(item)" />
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
