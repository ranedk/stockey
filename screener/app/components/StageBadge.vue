<script setup lang="ts">
const props = defineProps<{ stage?: number | null, size?: 'xs' | 'sm' | 'md', short?: boolean }>()
const s = computed(() => (props.stage ? STAGES[props.stage] : null))
</script>

<template>
  <UTooltip v-if="s" :text="s.desc">
    <UBadge :color="s.color" variant="soft" :size="size || 'sm'" class="whitespace-nowrap">
      <span class="inline-flex gap-0.5 mr-0.5" aria-hidden="true">
        <span v-for="i in 4" :key="i" class="h-2.5 w-1 rounded-sm" :class="i === stage ? 'bg-current' : 'bg-current/20'" />
      </span>
      {{ short ? s.short : s.label }}
    </UBadge>
  </UTooltip>
  <span v-else class="text-dimmed text-xs">—</span>
</template>
