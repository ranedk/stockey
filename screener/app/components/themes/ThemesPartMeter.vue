<script setup lang="ts">
// One 0..100 part of a theme's strength: label (with its rule in a tooltip), value, thin bar.
const props = defineProps<{ label: string, desc?: string, value?: number | null }>()
const v = computed(() => themesScore(props.value))
</script>

<template>
  <div class="min-w-0">
    <div class="mb-1 flex items-baseline justify-between gap-2 text-xs">
      <UTooltip :text="desc" :disabled="!desc">
        <span class="truncate text-muted" :class="desc ? 'cursor-help underline decoration-dotted decoration-1 underline-offset-2' : ''">{{ label }}</span>
      </UTooltip>
      <span class="num font-semibold text-highlighted">{{ v ?? '—' }}</span>
    </div>
    <div class="h-1.5 overflow-hidden rounded-full bg-elevated">
      <div class="h-full rounded-full bg-primary transition-all" :style="{ width: `${v ?? 0}%` }" />
    </div>
  </div>
</template>
