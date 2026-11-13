<script setup lang="ts">
// One whole split into parts (breadth, stage mix). 2px surface gap between segments; the
// legend below names every part with its count, so colour never carries identity alone.
const props = defineProps<{ parts: { label: string, value: number, color: string, hint?: string }[], height?: number }>()
const total = computed(() => props.parts.reduce((s, p) => s + (p.value || 0), 0) || 1)
</script>

<template>
  <div>
    <div class="flex w-full gap-0.5 overflow-hidden rounded-md" :style="{ height: `${height || 10}px` }">
      <UTooltip v-for="p in parts" :key="p.label" :text="`${p.label}: ${p.value.toLocaleString('en-IN')} (${Math.round(p.value / total * 100)}%)`">
        <div class="h-full first:rounded-l-md last:rounded-r-md transition-all" :style="{ width: `${p.value / total * 100}%`, background: p.color }" />
      </UTooltip>
    </div>
    <div class="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 text-xs">
      <div v-for="p in parts" :key="p.label" class="flex items-center gap-1.5">
        <span class="size-2 rounded-sm" :style="{ background: p.color }" />
        <span class="text-muted">{{ p.label }}</span>
        <span class="num font-medium text-highlighted">{{ p.value.toLocaleString('en-IN') }}</span>
      </div>
    </div>
  </div>
</template>
