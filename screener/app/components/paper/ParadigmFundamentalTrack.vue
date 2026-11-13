<script setup lang="ts">
import type { ParadigmStockeyTrackData } from '~/types/paperTracks'
// One business-quality replica: a stockey live track (no backtest: the ratios it needs have been
// stored only since 2026-09-27). Every card shares one fetch of the track list.
const props = defineProps<{ name: string, label: string }>()
const { data, error, status } = useStockey<ParadigmStockeyTrackData[]>('/api/paradigms/tracks', { lazy: true })

const tr = computed(() => data.value?.find(x => x.track === props.name) ?? null)
const rules = computed(() => PARADIGM_RULE_KEYS
  .map(k => [k, tr.value?.spec?.[k]] as const)
  .filter((x): x is readonly [typeof x[0], string] => typeof x[1] === 'string' && !!x[1]))
const frac = (v: number | null | undefined) => (v == null ? null : v / 100)
const top = computed(() => [...(tr.value?.holdings ?? [])].sort((a, b) => b.weight - a.weight).slice(0, 20))
const excess = computed(() => {
  const a = tr.value?.return_since_start_pct, b = tr.value?.benchmark?.return_since_start_pct
  return a != null && b != null ? (a - b) / 100 : null
})
</script>

<template>
  <div class="rounded-xl p-4 ring ring-default">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <span class="font-semibold text-highlighted">{{ label }}</span>
      <div v-if="tr" class="flex gap-1.5">
        <UBadge color="neutral" variant="subtle" size="sm" icon="i-lucide-calendar-sync" :label="`${tr.rebalance} rebalance`" />
        <UBadge color="neutral" variant="outline" size="sm" :label="`v${tr.version}`" />
      </div>
    </div>

    <UAlert v-if="error" class="mt-3" color="error" variant="subtle" icon="i-lucide-plug-zap" title="Could not reach the stockey API" :description="error.message" />
    <USkeleton v-else-if="status === 'pending' && !data" class="mt-3 h-40" />
    <UEmpty v-else-if="!tr" icon="i-lucide-search-x" title="Track not found" class="mt-3 py-4" />
    <template v-else>
      <div class="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4">
        <div class="col-span-2 rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Live since {{ dateShort(tr.start) }}</div>
          <template v-if="tr.status === 'live'">
            <div class="mt-1 flex flex-wrap items-baseline gap-x-3">
              <Change :value="frac(tr.return_since_start_pct)" class="text-lg" />
              <span class="text-xs text-muted">vs <Change :value="frac(tr.benchmark?.return_since_start_pct)" /> {{ tr.benchmark?.name }}</span>
            </div>
            <div class="mt-0.5 text-[11px] text-dimmed">difference: <Change :value="excess" /></div>
          </template>
          <div v-else class="mt-1 text-sm text-muted">starts {{ dateShort(tr.start) }}</div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Holding</div>
          <div class="num mt-1 text-lg font-semibold text-highlighted">{{ tr.holdings.length || '—' }} <span v-if="tr.holdings.length" class="text-xs font-normal text-muted">stocks</span></div>
        </div>
        <div class="rounded-lg bg-elevated/60 p-3">
          <div class="text-[11px] text-muted">Cash</div>
          <div class="num mt-1 text-lg font-semibold text-highlighted">{{ tr.cash_weight != null ? pct(tr.cash_weight, 0) : '—' }}</div>
        </div>
      </div>
      <p class="mt-2 flex items-center gap-1.5 text-[11px] text-dimmed">
        <UIcon name="i-lucide-info" class="size-3.5 shrink-0" />
        No backtest: the ratios these rules need have been stored only since Sep 2026, so this is a live record only.
      </p>

      <dl class="mt-3 space-y-2 text-sm">
        <div v-for="[k, v] in rules" :key="k" class="grid gap-x-3 sm:grid-cols-[7rem_1fr]">
          <dt class="text-xs font-medium text-muted">{{ PARADIGM_RULE_LABEL[k] ?? k }}</dt>
          <dd class="text-default">{{ v }}</dd>
        </div>
      </dl>

      <div class="mt-3 rounded-lg bg-warning/10 px-3 py-2 text-xs text-default ring ring-warning/25">
        <span class="font-semibold text-warning">Our guesses: </span>{{ tr.approximations }}
      </div>

      <div class="mt-3 text-[11px] text-muted">Holdings{{ tr.holdings_as_of ? ` on ${dateShort(tr.holdings_as_of)}` : '' }}, biggest first</div>
      <div class="mt-1.5 flex flex-wrap gap-1.5">
        <span v-for="h in top" :key="h.symbol" class="inline-flex items-center gap-1.5 rounded-md bg-elevated px-2 py-0.5 text-xs">
          <NuxtLink :to="`/company/${encodeURIComponent(h.symbol)}`" class="font-medium text-highlighted hover:text-primary">{{ h.symbol }}</NuxtLink>
          <span class="num text-muted">{{ pct(h.weight, 1) }}</span>
          <Change v-if="h.return_since_entry_pct != null" :value="frac(h.return_since_entry_pct)" :digits="0" />
        </span>
        <span v-if="!top.length" class="text-xs text-dimmed">none yet: first buy on or after {{ dateShort(tr.start) }}</span>
      </div>
    </template>
  </div>
</template>
