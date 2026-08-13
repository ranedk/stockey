<script setup lang="ts">
import type { Strategy } from '~/types/api'

const api = useApi()
const { data, status, error } = await useAsyncData('strategies', () => api.get<Strategy[]>('/api/strategies'))
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Strategies</h1>
    <p class="mt-1 text-sm text-slate-600">
      Every rule/LLM trigger this screener runs, and how many companies are currently
      watched under it. A company can satisfy more than one at once -- click into a
      strategy to see who, or open a company to see every strategy it satisfies.
    </p>

    <div v-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach the API ({{ error.message }}).
    </div>

    <div v-else-if="data && data.length === 0" class="mt-6 text-sm text-slate-500">
      No strategy has alerted yet.
    </div>

    <div v-else-if="data" class="mt-4 grid gap-3 sm:grid-cols-2">
      <NuxtLink
        v-for="s in data"
        :key="s.trigger_type"
        :to="`/strategies/${encodeURIComponent(s.trigger_type)}`"
        class="block rounded-lg border border-slate-200 bg-white p-4 transition hover:border-slate-300 hover:shadow-sm"
      >
        <div class="flex items-center justify-between gap-4">
          <span class="font-semibold">{{ strategyLabel(s.trigger_type) }}</span>
          <BadgePill :label="`${s.company_count} ${s.company_count === 1 ? 'company' : 'companies'}`" tone="neutral" />
        </div>
        <p class="mt-1 text-xs text-slate-400">last alert {{ formatDate(s.last_alert_date) }}</p>
      </NuxtLink>
    </div>

    <p v-else-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</p>
  </div>
</template>
