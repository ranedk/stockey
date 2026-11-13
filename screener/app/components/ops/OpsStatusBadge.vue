<script setup lang="ts">
// Status = colour AND icon AND word, never colour alone.
const props = withDefaults(defineProps<{ level?: string | null, label?: string, tip?: string, size?: 'xs' | 'sm' | 'md' }>(), { size: 'sm' })
const meta = computed(() => opsLevel(props.level))
</script>

<template>
  <UTooltip :text="tip" :disabled="!tip">
    <UBadge :color="meta.color" variant="subtle" :size="size" :icon="meta.icon" :label="label ?? meta.label" class="whitespace-nowrap" />
  </UTooltip>
</template>
