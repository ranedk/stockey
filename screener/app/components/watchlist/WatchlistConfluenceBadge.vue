<script setup lang="ts">
// The balance of the five independent checks (confluence_score.py). Both numbers always show:
// "1 for · 3 against" and "1 for · none against" are opposite messages.
const props = defineProps<{ supportive: number | null, contradicting: number | null, evaluable: number | null, size?: 'xs' | 'sm' | 'md' }>()

const scored = computed(() => props.supportive !== null && props.evaluable !== null)
const color = computed(() => {
  if (!scored.value) return 'neutral' as const
  const yes = props.supportive ?? 0
  const no = props.contradicting ?? 0
  if (yes > no) return 'success' as const
  if (no > yes) return 'error' as const
  return 'warning' as const
})
const label = computed(() => {
  if (!scored.value) return 'Checks: not scored'
  const yes = props.supportive ?? 0
  const no = props.contradicting ?? 0
  return `${yes} for · ${no > 0 ? no : 'none'} against`
})
const tip = computed(() => {
  if (!scored.value) return 'The five independent checks have not been run on this company yet.'
  const unknown = 5 - (props.evaluable ?? 0)
  return `Of five independent checks, ${props.supportive} support this company and ${props.contradicting} contradict it`
    + (unknown > 0 ? `; ${unknown} could not be judged for lack of data.` : '.')
})
</script>

<template>
  <UTooltip :text="tip">
    <UBadge :color="color" variant="subtle" :size="size || 'sm'" icon="i-lucide-scale" :label="label" class="whitespace-nowrap" />
  </UTooltip>
</template>
