<script setup lang="ts">
const props = defineProps<{
  symbol?: unknown
  subtle?: boolean
}>()

const normalizedSymbol = computed(() => String(props.symbol || '').trim().toUpperCase())
const href = computed(() => `/symbols/${encodeURIComponent(normalizedSymbol.value)}`)
const label = computed(() => `Open ${normalizedSymbol.value} symbol detail`)
</script>

<template>
  <NuxtLink
    v-if="normalizedSymbol"
    :to="href"
    :aria-label="label"
    :title="label"
    class="inline-flex items-center gap-1.5 rounded-lg border border-moss/25 bg-moss/10 font-black text-moss underline decoration-moss/45 underline-offset-4 transition hover:border-moss hover:bg-moss hover:text-white focus:outline-none focus:ring-2 focus:ring-moss/35"
    :class="subtle ? 'px-2 py-0.5 text-xs' : 'px-2.5 py-1 text-sm'"
  >
    <span class="sr-only">Open symbol page for </span>{{ normalizedSymbol }} <span aria-hidden="true">-&gt;</span>
  </NuxtLink>
  <span v-else>-</span>
</template>
