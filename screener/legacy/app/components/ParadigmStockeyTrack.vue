<script setup lang="ts">
// One fundamental replica: a stockey forward track (no replay -- the ratios it needs
// exist only since 2026-09-27). Every card shares one fetch of the track list.
const props = defineProps<{ name: string; label: string }>()

const api = useApi()
const { data, error } = await useAsyncData('paradigm-stockey-tracks', () => api.get<StockeyParadigmTrack[]>('/api/paradigms/tracks'))

const t = computed(() => data.value?.find(x => x.track === props.name) ?? null)
const rules = computed(() => STOCKEY_RULE_KEYS
  .map(k => [k, t.value?.spec?.[k]] as const)
  .filter(([, v]) => typeof v === 'string' && v))
const pct = (v: number | null | undefined) => (v == null ? null : v / 100)
const topHoldings = computed(() => [...(t.value?.holdings ?? [])].sort((a, b) => b.weight - a.weight).slice(0, 20))
</script>

<template>
  <div class="rounded-lg border border-slate-200 bg-white p-4">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <span class="font-medium text-slate-900">{{ label }}</span>
      <span class="text-xs text-slate-500">{{ t?.rebalance }} rebalance, v{{ t?.version }}</span>
    </div>
    <div v-if="error" class="mt-2 text-sm text-rose-700">Could not reach the stockey API ({{ error.message }}).</div>
    <div v-else-if="!t" class="mt-2 text-sm text-slate-500">Track not found.</div>
    <template v-else>
      <div class="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
        <div class="rounded-lg border border-slate-200 px-3 py-2">
          <div class="text-xs text-slate-500">Forward since {{ t.start }}</div>
          <template v-if="t.status === 'live'">
            <ReturnValue :value="pct(t.return_since_start_pct)" :digits="1" />
            <span class="ml-1 text-xs text-slate-500">vs <ReturnValue :value="pct(t.benchmark?.return_since_start_pct)" :digits="1" /> {{ t.benchmark?.name }}</span>
          </template>
          <div v-else class="text-sm text-slate-500">starts {{ t.start }}</div>
        </div>
        <StatChip label="Holding" :value="t.holdings.length ? `${t.holdings.length} names` : null" />
        <StatChip label="Cash" :value="t.cash_weight != null ? `${(100 * t.cash_weight).toFixed(0)}%` : null" />
      </div>
      <div class="mt-2 text-xs text-slate-500">No replay: the ratios these rules need are stored only since Sep 2026, so this is a forward record only.</div>
      <dl class="mt-3 space-y-1 text-sm">
        <div v-for="[k, v] in rules" :key="k" class="grid grid-cols-[6.5rem_1fr] gap-2">
          <dt class="text-xs uppercase text-slate-500">{{ k }}</dt>
          <dd class="text-slate-700">{{ v }}</dd>
        </div>
      </dl>
      <div class="mt-3 rounded bg-amber-50 px-3 py-2 text-xs text-amber-900">
        <span class="font-medium">Our guesses:</span> {{ t.approximations }}
      </div>
      <div class="mt-3 text-xs text-slate-500">Holdings{{ t.holdings_as_of ? ` (${t.holdings_as_of})` : '' }}:</div>
      <div class="mt-1 flex flex-wrap gap-1.5">
        <span v-for="h in topHoldings" :key="h.symbol" class="rounded bg-slate-100 px-2 py-0.5 text-xs tabular-nums text-slate-700">
          {{ h.symbol }} {{ (100 * h.weight).toFixed(1) }}%
          <ReturnValue v-if="h.return_since_entry_pct != null" :value="pct(h.return_since_entry_pct)" :digits="0" />
        </span>
        <span v-if="!topHoldings.length" class="text-xs text-slate-400">none yet -- first buy on or after {{ t.start }}</span>
      </div>
    </template>
  </div>
</template>
