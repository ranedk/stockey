<script setup lang="ts">
// A signed change with an arrow: direction is in the text, colour only reinforces it.
withDefaults(defineProps<{ value?: number | null, digits?: number, pill?: boolean }>(), { digits: 1 })
</script>

<template>
  <span class="num inline-flex items-center gap-0.5 font-medium" :class="[signClass(value), pill && isNum(value) && value !== 0 ? (value! > 0 ? 'bg-up-soft' : 'bg-down-soft') + ' rounded px-1.5 py-0.5' : '']">
    <span v-if="arrow(value)" class="text-[0.6em] leading-none">{{ arrow(value) }}</span>{{ pct(isNum(value) ? Math.abs(value) : value, digits) }}
  </span>
</template>
