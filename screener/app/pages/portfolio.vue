<script setup lang="ts">
import type { PortfolioThesis } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('portfolio', () => api.get<PortfolioThesis[]>('/api/portfolio'))

const open = computed(() => data.value?.filter((t) => t.status === 'open') ?? [])
const resolved = computed(() => data.value?.filter((t) => t.status !== 'open') ?? [])
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Portfolio</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every thesis you've committed to -- open positions and their resolved history.
      Nothing here was added automatically.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <template v-else-if="data">
      <h2 class="mt-6 text-sm font-semibold uppercase tracking-wide text-slate-500">Open ({{ open.length }})</h2>
      <div class="mt-2 grid gap-2">
        <NuxtLink
          v-for="t in open"
          :key="t.thesis_id"
          :to="`/watchlist/${encodeURIComponent(t.company_master_id)}`"
          class="rounded-lg border border-slate-200 bg-white p-3 hover:border-slate-300"
        >
          <div class="flex items-center justify-between">
            <span class="font-medium">{{ t.company_master_id.replace('nse:', '') }}</span>
            <span class="text-xs text-slate-400">target {{ formatDate(t.target_date) }}</span>
          </div>
          <p class="mt-1 text-sm text-slate-700">{{ t.prediction_text }}</p>
        </NuxtLink>
        <p v-if="!open.length" class="text-sm text-slate-400">No open positions.</p>
      </div>

      <h2 class="mt-6 text-sm font-semibold uppercase tracking-wide text-slate-500">Resolved ({{ resolved.length }})</h2>
      <div class="mt-2 grid gap-2">
        <NuxtLink
          v-for="t in resolved"
          :key="t.thesis_id"
          :to="`/watchlist/${encodeURIComponent(t.company_master_id)}`"
          class="rounded-lg border border-slate-200 bg-white p-3 hover:border-slate-300"
        >
          <div class="flex items-center justify-between">
            <span class="font-medium">{{ t.company_master_id.replace('nse:', '') }}</span>
            <BadgePill :label="t.resolved_true ? 'came true' : 'did not come true'" :tone="t.resolved_true ? 'good' : 'bad'" />
          </div>
          <p class="mt-1 text-sm text-slate-700">{{ t.prediction_text }}</p>
          <p v-if="t.failure_attribution" class="mt-1 text-xs text-slate-500">{{ t.failure_attribution }}</p>
        </NuxtLink>
        <p v-if="!resolved.length" class="text-sm text-slate-400">Nothing resolved yet.</p>
      </div>
    </template>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
