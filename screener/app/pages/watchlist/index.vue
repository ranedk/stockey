<script setup lang="ts">
import type { WatchlistItem } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('watchlist', () => api.get<WatchlistItem[]>('/api/watchlist'))

function priceDeltaTone(item: WatchlistItem): 'good' | 'bad' | 'neutral' {
  const pct = priceChangePct(item.first_seen_price, item.current_price)
  if (pct === null) return 'neutral'
  return pct >= 0 ? 'good' : 'bad'
}
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Watchlist</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every company with an active fundamental alert. Added automatically -- nothing
      here has been reviewed by you yet, that's what the detail page is for.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="data && data.length === 0" class="mt-6 text-sm text-slate-500">
      Nothing on the watchlist yet.
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
            </div>
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
