<script setup lang="ts">
withDefaults(defineProps<{
  tone?: 'action' | 'success' | 'warning' | 'danger' | 'neutral' | 'info' | 'dark'
  title?: string
}>(), {
  tone: 'neutral',
  title: ''
})
</script>

<template>
  <span
    :title="title || undefined"
    class="inline-flex items-center gap-1 rounded-full border px-3 py-1 text-xs font-black uppercase tracking-[0.12em]"
    :class="{
      'border-sky/30 bg-sky/15 text-sky': tone === 'info',
      'border-moss/30 bg-moss/15 text-moss': tone === 'success',
      'border-sun/40 bg-sun/25 text-ink': tone === 'warning',
      'border-rust/30 bg-rust/10 text-rust': tone === 'danger',
      'border-ink/20 bg-ink text-paper': tone === 'dark',
      'border-ink/10 bg-white/85 text-ink/65': tone === 'neutral',
      'border-moss bg-moss text-paper shadow-sm shadow-moss/20': tone === 'action'
    }"
  >
    <slot />
  </span>
</template>
