<script setup lang="ts">
// A metric beside its industry median, on one shared track, with the numbers written out.
const props = defineProps<{ label: string, value?: number | null, median?: number | null, unit: 'pct' | 'x', higherIsBetter?: boolean | null }>()
const fmt = (v: any) => (props.unit === 'pct' ? pct(v) : mult(v))
const geo = computed(() => {
  const v = props.value, m = props.median
  if (!isNum(v)) return null
  const vals = [v, isNum(m) ? m : v, 0]
  const lo = Math.min(...vals), hi = Math.max(...vals)
  const span = hi - lo || 1
  const p = (x: number) => ((x - lo) / span) * 92 + 4
  return { v: p(v), m: isNum(m) ? p(m) : null }
})
const better = computed(() => {
  if (props.higherIsBetter == null || !isNum(props.value) || !isNum(props.median)) return null
  return props.higherIsBetter ? props.value >= props.median : props.value <= props.median
})
</script>

<template>
  <div>
    <div class="text-xs text-muted">{{ label }}</div>
    <div class="num mt-0.5 flex items-baseline justify-between gap-2 text-xs">
      <b class="text-sm text-highlighted">{{ fmt(value) }}</b>
      <span class="text-dimmed">industry {{ fmt(median) }}</span>
    </div>
    <div v-if="geo" class="relative mt-1.5 h-1.5 rounded-full bg-elevated">
      <div v-if="geo.m != null" class="absolute top-1/2 h-3 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded bg-[var(--chart-axis)]" :style="{ left: `${geo.m}%` }" />
      <div class="absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-[var(--chart-surface)]"
        :class="better == null ? 'bg-primary' : better ? 'bg-[var(--up)]' : 'bg-[var(--down)]'" :style="{ left: `${geo.v}%` }" />
    </div>
  </div>
</template>
