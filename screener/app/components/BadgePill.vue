<script setup lang="ts">
const props = defineProps<{
  label: string
  tone?: 'neutral' | 'good' | 'bad' | 'warn'
}>()

const toneClasses: Record<string, string> = {
  neutral: 'bg-slate-100 text-slate-700',
  good: 'bg-emerald-100 text-emerald-700',
  bad: 'bg-rose-100 text-rose-700',
  warn: 'bg-amber-100 text-amber-800',
}
</script>

<template>
  <span
    class="inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium"
    :class="toneClasses[props.tone || 'neutral']"
  >
    {{ props.label }}
  </span>
</template>
