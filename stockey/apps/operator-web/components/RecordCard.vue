<script setup lang="ts">
import type { Dict } from '~/types/api'

defineProps<{
  title: string
  subtitle?: string
  record: Dict
}>()
</script>

<template>
  <article class="glass-panel rounded-3xl p-5">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-base font-black text-ink">{{ title }}</h3>
        <p v-if="subtitle" class="mt-1 text-sm text-ink/60">{{ subtitle }}</p>
      </div>
      <slot name="badge" />
    </div>
    <slot />
    <details class="mt-4">
      <summary class="cursor-pointer text-sm font-semibold text-moss">Show raw details</summary>
      <pre class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(record, null, 2) }}</pre>
    </details>
  </article>
</template>
