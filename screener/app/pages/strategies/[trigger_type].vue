<script setup lang="ts">
import type { StrategyDetail } from '~/types/api'

const route = useRoute()
const triggerType = computed(() => decodeURIComponent(route.params.trigger_type as string))

const api = useApi()
const { data, status, error } = await useAsyncData(
  () => `strategy-detail-${triggerType.value}`,
  () => api.get<StrategyDetail>(`/api/strategies/${encodeURIComponent(triggerType.value)}`),
  { watch: [triggerType] },
)

const label = computed(() => strategyLabel(triggerType.value))
</script>

<template>
  <div v-if="error" class="rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
    Could not load {{ triggerType }} ({{ error.statusCode === 404 ? 'no company has ever satisfied this' : error.message }}).
  </div>

  <p v-else-if="status === 'pending'" class="text-sm text-slate-500">Loading…</p>

  <div v-else-if="data" class="space-y-4">
    <NuxtLink to="/strategies" class="text-sm text-slate-500 hover:text-slate-900">← Strategies</NuxtLink>

    <div>
      <h1 class="text-xl font-semibold">{{ label }}</h1>
      <p class="mt-1 text-sm text-slate-600">
        {{ data.companies.length }} {{ data.companies.length === 1 ? 'company' : 'companies' }} currently satisfy this strategy.
      </p>
    </div>

    <div v-if="data.companies.length === 0" class="text-sm text-slate-500">No company currently satisfies this.</div>

    <div v-else class="grid gap-3">
      <NuxtLink
        v-for="c in data.companies"
        :key="c.company_master_id"
        :to="`/watchlist/${encodeURIComponent(c.company_master_id)}`"
        class="block rounded-lg border border-slate-200 bg-white p-4 transition hover:border-slate-300 hover:shadow-sm"
      >
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <span class="font-semibold">{{ c.company_master_id.replace('nse:', '') }}</span>
              <span class="text-sm text-slate-500">{{ c.company_name }}</span>
              <BadgePill v-if="c.origin" :label="c.origin" tone="neutral" />
            </div>
            <p v-if="c.reasoning" class="mt-2 max-w-2xl text-sm text-slate-600">{{ c.reasoning }}</p>
          </div>
          <div class="shrink-0 text-right text-sm text-slate-500">
            <div>{{ formatPrice(c.current_price) }}</div>
            <div class="mt-1 text-xs text-slate-400">{{ formatDate(c.alert_date) }}</div>
          </div>
        </div>
      </NuxtLink>
    </div>
  </div>
</template>
