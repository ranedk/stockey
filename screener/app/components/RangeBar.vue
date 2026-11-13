<script setup lang="ts">
// 52-week range: where today's price sits between the low and the high.
const props = defineProps<{ low?: number | null, high?: number | null, value?: number | null }>()
const pos = computed(() => {
  if (!isNum(props.low) || !isNum(props.high) || !isNum(props.value) || props.high <= props.low) return null
  return Math.max(0, Math.min(1, (props.value - props.low) / (props.high - props.low)))
})
</script>

<template>
  <div v-if="pos != null" class="w-full">
    <div class="relative h-1.5 rounded-full" style="background: linear-gradient(90deg, var(--down-soft), var(--chart-grid), var(--up-soft))">
      <div class="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-[var(--chart-surface)] bg-highlighted shadow" :style="{ left: `${pos * 100}%` }" />
    </div>
    <div class="num mt-1.5 flex justify-between text-[11px] text-muted">
      <span>{{ inr(low) }}</span><span class="text-dimmed">52-week range</span><span>{{ inr(high) }}</span>
    </div>
  </div>
</template>
