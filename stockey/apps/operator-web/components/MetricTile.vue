<script setup lang="ts">
defineProps<{
  label: string
  value: string | number
  note?: string
}>()
</script>

<template>
  <div class="glass-panel rounded-3xl p-5">
    <p class="text-xs font-semibold uppercase tracking-[0.24em] text-ink/50">{{ label }}</p>
    <p class="mt-3 text-3xl font-black text-ink">{{ value }}</p>
    <p v-if="note" class="mt-2 text-sm leading-6 text-ink/60">{{ note }}</p>
  </div>
</template>

