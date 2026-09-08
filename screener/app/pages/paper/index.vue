<script setup lang="ts">
import type { PaperListResponse } from '~/types/systrader'

const api = useSystraderApi()
const { data, status, error } = await useAsyncData('paper-list', () => api.get<PaperListResponse>('/api/paper'))

const strategies = computed(() => data.value?.strategies ?? [])
</script>

<template>
  <div>
    <h1 class="text-xl font-semibold">Paper Trading</h1>
    <p class="mt-1 max-w-3xl text-sm text-slate-600">
      Forward records for frozen strategies. Everything else in this system is scored against
      history, and history can be mined; these accumulate evidence from data nobody chose. Nothing
      here is a recommendation, and no capital moves off it.
    </p>

    <div v-if="status === 'pending'" class="mt-6 text-sm text-slate-500">Loading…</div>
    <div v-else-if="error" class="mt-6 rounded-lg border border-rose-200 bg-rose-50 p-4 text-sm text-rose-700">
      Could not reach systrader's API. Is <code>cmd/api</code> running on :8090?
    </div>
    <div v-else-if="!strategies.length" class="mt-6 rounded-lg border border-slate-200 bg-white p-6 text-sm text-slate-500">
      No strategies are being tracked yet.
    </div>

    <div v-else class="mt-6 space-y-3">
      <NuxtLink
        v-for="(s, i) in strategies"
        :key="s.name"
        :to="`/paper/${s.name}`"
        class="block rounded-lg border border-slate-200 bg-white p-4 hover:border-slate-300 hover:bg-slate-50"
      >
        <div class="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div class="text-sm font-semibold text-slate-900">{{ paperStrategyLabel(s.name, i) }}</div>
            <div class="mt-0.5 text-xs text-slate-500">{{ s.spec.signal }}</div>
            <div class="mt-1 text-xs text-slate-500">{{ s.spec.universe }}</div>
          </div>
          <div class="text-right">
            <template v-if="s.days > 0">
              <ReturnValue :value="strategyBook(s)?.total_return" size="lg" />
              <div class="mt-0.5 text-xs text-slate-500">
                since {{ formatDate(s.start) }} · {{ s.days }} trading days
              </div>
              <div class="mt-1 text-xs text-slate-500">
                today <ReturnValue :value="strategyBook(s)?.day_return" />
                · vs equal-weight <ReturnValue :value="excessReturn(s)" />
              </div>
            </template>
            <template v-else>
              <div class="text-sm font-medium text-slate-500">Not started</div>
              <div class="mt-0.5 text-xs text-slate-500">
                frozen {{ formatDate(s.start) }} · first fill still to come
              </div>
              <div v-if="s.pending?.orders?.length" class="mt-1 text-xs text-slate-500">
                {{ s.pending.orders.length }} orders on the sheet
              </div>
            </template>
          </div>
        </div>
      </NuxtLink>
    </div>
  </div>
</template>
