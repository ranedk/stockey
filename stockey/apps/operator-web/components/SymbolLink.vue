<script setup lang="ts">
const props = defineProps<{
  symbol?: unknown
  subtle?: boolean
}>()

const normalizedSymbol = computed(() => String(props.symbol || '').trim().toUpperCase())
const href = computed(() => `/symbols/${encodeURIComponent(normalizedSymbol.value)}`)
</script>

<template>
  <NuxtLink
    v-if="normalizedSymbol"
    :to="href"
    class="inline-flex items-center rounded-full font-black transition hover:translate-y-[-1px]"
    :class="subtle ? 'bg-ink/10 px-2.5 py-1 text-xs text-ink/70 hover:bg-ink hover:text-paper' : 'bg-ink px-3 py-1.5 text-sm text-paper hover:bg-moss'"
  >
    {{ normalizedSymbol }}
  </NuxtLink>
  <span v-else>-</span>
</template>
