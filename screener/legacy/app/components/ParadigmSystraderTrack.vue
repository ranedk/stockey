<script setup lang="ts">
import type { PaperStrategy, PaperSummary } from '~/types/systrader'

// One momentum replica: its 2016+ replay (the reference track) and its forward paper
// record, from systrader's GET /api/paper/{name}.
const props = defineProps<{ name: string; label: string }>()

const api = useSystraderApi()
const { data, error } = await useAsyncData(`paradigm-${props.name}`, () => api.get<PaperStrategy>(`/api/paper/${props.name}`))

const BOOKS = PAPER_BOOKS

function book(s: PaperStrategy | null | undefined, key: string): PaperSummary | undefined {
  return s?.summaries?.find(x => x.book === key)
}

// Annualised from the NAV and the calendar span, so books of any length compare.
function perYear(s: PaperStrategy | null | undefined, key: string): number | null {
  const b = book(s, key)
  if (!s || !b || !s.start || !s.as_of) return null
  const years = (new Date(s.as_of).getTime() - new Date(s.start).getTime()) / (365.25 * 864e5)
  if (years < 0.5) return null
  return Math.pow(b.nav / 100, 1 / years) - 1
}

const ref_ = computed(() => data.value?.reference ?? null)
const fwd = computed(() => data.value ?? null)
const topHoldings = computed(() => {
  const h = fwd.value?.holdings?.length ? fwd.value.holdings : ref_.value?.holdings ?? []
  return [...h].sort((a, b) => b.weight - a.weight).slice(0, 12)
})
const holdingsFrom = computed(() => (fwd.value?.holdings?.length ? 'forward record' : 'replay, today'))
const invested = computed(() => topHoldings.value.length
  ? (fwd.value?.holdings?.length ? fwd.value.holdings : ref_.value?.holdings ?? []).reduce((s, h) => s + h.weight, 0)
  : null)
</script>

<template>
  <div class="rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <NuxtLink :to="`/paper/${name}`" class="font-medium text-slate-900 hover:underline">{{ label }}</NuxtLink>
      <span class="text-xs text-slate-500">{{ data?.spec?.rebalance }}</span>
    </div>
    <div v-if="error" class="mt-2 text-sm text-rose-700">Could not reach systrader ({{ error.message }}).</div>
    <template v-else-if="data">
      <div class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <div class="rounded-lg border border-slate-200 px-3 py-2">
          <div class="text-xs text-slate-500">Replay since {{ ref_?.start ?? '—' }}, per year</div>
          <ReturnValue :value="perYear(ref_, 'strategy')" :digits="1" />
          <span class="ml-1 text-xs text-slate-500">vs <ReturnValue :value="perYear(ref_, 'equal-weight')" :digits="1" /> same-universe equal weight</span>
        </div>
        <div class="rounded-lg border border-slate-200 px-3 py-2">
          <div class="text-xs text-slate-500">Replay worst drop</div>
          <ReturnValue :value="book(ref_, 'strategy')?.max_drawdown" :digits="1" />
          <span class="ml-1 text-xs text-slate-500">vs <ReturnValue :value="book(ref_, 'equal-weight')?.max_drawdown" :digits="1" /></span>
        </div>
        <div class="rounded-lg border border-slate-200 px-3 py-2">
          <div class="text-xs text-slate-500">Forward since {{ fwd?.start }}</div>
          <template v-if="fwd?.days">
            <ReturnValue :value="book(fwd, 'strategy')?.total_return" :digits="1" />
            <span class="ml-1 text-xs text-slate-500">vs <ReturnValue :value="book(fwd, 'equal-weight')?.total_return" :digits="1" /> ({{ fwd.days }} days)</span>
          </template>
          <div v-else class="text-sm text-slate-500">starts {{ fwd?.start }}</div>
        </div>
        <StatChip label="Replay costs per year" :value="book(ref_, 'strategy')?.ann_cost != null ? `${(100 * (book(ref_, 'strategy')?.ann_cost ?? 0)).toFixed(1)}%` : null" />
      </div>
      <div v-if="ref_?.nav?.length" class="mt-3">
        <NavChart :points="ref_.nav" :books="BOOKS" :height="160" />
      </div>
      <div class="mt-3 text-xs text-slate-500">
        Holdings ({{ holdingsFrom }}){{ invested != null && invested < 0.995 ? `, ${(100 * (1 - invested)).toFixed(0)}% in cash` : '' }}:
      </div>
      <div class="mt-1 flex flex-wrap gap-1.5">
        <span v-for="h in topHoldings" :key="h.symbol" class="rounded bg-slate-100 px-2 py-0.5 text-xs tabular-nums text-slate-700">
          {{ h.symbol }} {{ (100 * h.weight).toFixed(1) }}%
          <ReturnValue v-if="h.return != null" :value="h.return" :digits="0" />
        </span>
        <span v-if="!topHoldings.length" class="text-xs text-slate-400">none yet</span>
      </div>
    </template>
  </div>
</template>
