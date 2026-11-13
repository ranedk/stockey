<script setup lang="ts">
const props = defineProps<{ tag: string, rule?: string, size?: 'xs' | 'sm' | 'md' }>()
const tone = computed(() => tagTone(props.tag))
</script>

<template>
  <UTooltip :text="rule" :disabled="!rule">
    <UBadge :color="tone.color" variant="subtle" :size="size || 'sm'" :icon="tone.icon" :label="tag" class="whitespace-nowrap" />
  </UTooltip>
</template>
