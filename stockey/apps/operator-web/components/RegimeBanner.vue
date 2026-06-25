<script setup lang="ts">
type Dict = Record<string, unknown>

const api = useOperatorApi()
const { data } = await useAsyncData('regime-banner', () => api.getMarketContext(1), { server: false, lazy: true })

function asDict(value: unknown): Dict {
  return typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Dict : {}
}
const summary = computed(() => asDict(asDict(data.value).summary))
const state = computed(() => String(summary.value.macro_risk_state || '').toUpperCase())
const regime = computed(() => String(summary.value.regime_name || ''))
const text = computed(() => String(summary.value.summary_text || ''))
const breadth = computed(() => {
  const v = summary.value.breadth_above_dma50_pct
  return typeof v === 'number' ? `${Math.round(v)}% > 50DMA` : ''
})

// NORMAL (calm) -> moss; WATCH -> sun; ELEVATED/STRESS -> rust. Unknown -> neutral.
const chipClass = computed(() => {
  if (state.value === 'STRESS' || state.value === 'ELEVATED') return 'border-rust/30 bg-rust/10 text-rust'
  if (state.value === 'WATCH') return 'border-sun/40 bg-sun/15 text-ink'
  if (state.value === 'NORMAL') return 'border-moss/25 bg-moss/10 text-moss'
  return 'border-ink/15 bg-white/70 text-ink/55'
})
</script>

<template>
  <div v-if="state || text" class="flex flex-wrap items-center gap-3 rounded-2xl border border-ink/10 bg-white/60 px-5 py-3 text-sm">
    <span class="rounded-full border px-3 py-1 text-xs font-black uppercase tracking-wide" :class="chipClass">
      {{ state || 'regime' }}<span v-if="regime" class="font-semibold"> · {{ regime }}</span>
    </span>
    <span v-if="breadth" class="rounded-full border border-ink/10 bg-white/70 px-3 py-1 text-xs font-semibold text-ink/55">{{ breadth }}</span>
    <span class="text-ink/65">{{ text }}</span>
  </div>
</template>
