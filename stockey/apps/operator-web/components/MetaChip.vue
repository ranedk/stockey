<script setup lang="ts">
withDefaults(defineProps<{
  label?: string
  tone?: 'plain' | 'blue' | 'green' | 'yellow' | 'red'
  title?: string
}>(), {
  tone: 'plain',
  title: ''
})
</script>

<template>
  <span
    :title="title || undefined"
    class="inline-flex items-center gap-1 rounded-xl border px-2.5 py-1 text-xs font-bold"
    :class="{
      'border-black/10 bg-white/75 text-ink/60': tone === 'plain',
      'border-sky/20 bg-sky/10 text-sky': tone === 'blue',
      'border-moss/20 bg-moss/10 text-moss': tone === 'green',
      'border-sun/30 bg-sun/20 text-ink': tone === 'yellow',
      'border-rust/20 bg-rust/10 text-rust': tone === 'red'
    }"
  >
    <span v-if="label" class="font-black uppercase tracking-[0.14em] opacity-60">{{ label }}</span>
    <slot />
  </span>
</template>
