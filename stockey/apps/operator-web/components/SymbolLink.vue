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
    class="inline-flex items-center gap-1 border-b-2 border-moss/35 font-black text-moss transition hover:border-moss hover:text-ink"
    :class="subtle ? 'text-xs' : 'text-sm'"
  >
    {{ normalizedSymbol }} <span aria-hidden="true">-&gt;</span>
  </NuxtLink>
  <span v-else>-</span>
</template>
