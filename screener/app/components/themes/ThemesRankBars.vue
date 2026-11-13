<script setup lang="ts">
// A stock's overall score in its theme plus its four equal parts as small bars (0..100 each).
const props = defineProps<{ stock: ThemesStock }>()
const parts = computed(() => THEMES_STOCK_PARTS.map(p => ({ ...p, v: themesScore(props.stock[p.key]) })))
const tip = computed(() => parts.value.map(p => `${p.label}: ${p.v ?? '—'}`).join('\n'))
</script>

<template>
  <UTooltip :text="tip" :ui="{ content: 'h-auto whitespace-pre-line text-left' }">
    <div class="inline-flex cursor-help items-center gap-2">
      <span class="num w-7 text-right font-semibold text-highlighted">{{ themesScore(stock.combined) ?? '—' }}</span>
      <div class="flex h-5 items-end gap-0.5" aria-hidden="true">
        <div v-for="p in parts" :key="p.key" class="flex h-full w-1.5 items-end overflow-hidden rounded-sm bg-elevated">
          <div class="w-full rounded-sm bg-primary" :style="{ height: `${Math.max(p.v ?? 0, 4)}%`, opacity: p.v == null ? 0.2 : 1 }" />
        </div>
      </div>
    </div>
  </UTooltip>
</template>
