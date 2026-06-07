<script setup lang="ts">
withDefaults(defineProps<{
  to: string
  variant?: 'primary' | 'secondary' | 'ghost'
}>(), {
  variant: 'primary'
})
</script>

<template>
  <NuxtLink
    :to="to"
    class="inline-flex items-center justify-center gap-2 rounded-2xl border px-4 py-2 text-sm font-black shadow-sm transition hover:-translate-y-0.5 hover:shadow-soft focus:outline-none focus:ring-2 focus:ring-moss/40"
    :class="{
      'border-ink bg-ink text-paper': variant === 'primary',
      'border-moss/25 bg-moss/10 text-moss': variant === 'secondary',
      'border-black/10 bg-white/80 text-ink/75': variant === 'ghost'
    }"
  >
    <slot />
  </NuxtLink>
</template>
