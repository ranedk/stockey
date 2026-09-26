<script setup lang="ts">
defineProps<{
  label: string
  value: string | number | null | undefined
}>()
</script>

<template>
  <div class="rounded-lg border border-slate-200 bg-white px-3 py-2">
    <div class="text-xs text-slate-500">{{ label }}</div>
    <div class="text-sm font-medium text-slate-900">
      {{ value === null || value === undefined ? '—' : value }}
    </div>
  </div>
</template>
