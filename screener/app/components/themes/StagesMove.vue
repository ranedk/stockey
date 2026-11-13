<script setup lang="ts">
// Places climbed (▲, green) or lost (▼, red) since an earlier rank; "=" when unchanged.
const props = defineProps<{ now: number, before: number }>()
const m = computed(() => stagesRankMove(props.now, props.before))
</script>

<template>
  <UTooltip :text="before ? `Rank then: ${before}` : 'Not ranked then'">
    <span class="num inline-flex items-center gap-0.5 text-sm font-medium" :class="m == null || m === 0 ? 'text-dimmed' : m > 0 ? 'text-up' : 'text-down'">
      <template v-if="m == null">—</template>
      <template v-else-if="m === 0">=</template>
      <template v-else><span class="text-[0.6em]">{{ m > 0 ? '▲' : '▼' }}</span>{{ Math.abs(m) }}</template>
    </span>
  </UTooltip>
</template>
