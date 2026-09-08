<script setup lang="ts">
// A signed percentage that colours itself. Used everywhere a gain or loss
// appears so red and green mean the same thing on every page.
const props = withDefaults(defineProps<{
  value: number | null | undefined
  digits?: number
  size?: 'sm' | 'lg'
  /** value is already a percentage (12.3) rather than a fraction (0.123) */
  isPercent?: boolean
}>(), { digits: 2, size: 'sm', isPercent: false })

const pct = computed(() => {
  if (props.value === null || props.value === undefined) return null
  return props.isPercent ? props.value : props.value * 100
})

const text = computed(() => {
  if (pct.value === null) return '—'
  const sign = pct.value > 0 ? '+' : ''
  return `${sign}${pct.value.toFixed(props.digits)}%`
})

const tone = computed(() => {
  if (pct.value === null) return 'text-slate-400'
  if (pct.value > 0) return 'text-emerald-600'
  if (pct.value < 0) return 'text-rose-600'
  return 'text-slate-500'
})
</script>

<template>
  <span
    class="tabular-nums"
    :class="[tone, size === 'lg' ? 'text-2xl font-semibold' : 'font-medium']"
  >{{ text }}</span>
</template>
