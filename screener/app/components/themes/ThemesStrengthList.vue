<script setup lang="ts">
// Every ranked theme as a bar of its strength (0-100), strongest first, coloured by where it
// landed (on the list, next up, late, not shown). The status is also written as a badge.
const props = defineProps<{ themes: ThemesRow[] }>()
const rows = computed(() => props.themes.filter(t => isNum(t.strength))
  .sort((a, b) => (a.strength_order ?? 1e9) - (b.strength_order ?? 1e9)))
const BAR: Record<string, string> = { shown: 'bg-success', next: 'bg-info', late: 'bg-error/70', out: 'bg-accented' }
const legend = computed(() => (['shown', 'next', 'late', 'out'] as const).map(s => ({
  s, ...THEMES_STATUS[s]!, n: rows.value.filter(t => t.status === s).length,
})).filter(l => l.n))
</script>

<template>
  <div>
    <div class="mb-3 flex flex-wrap gap-x-4 gap-y-1 text-xs">
      <UTooltip v-for="l in legend" :key="l.s" :text="l.desc">
        <span class="inline-flex cursor-help items-center gap-1.5">
          <span class="size-2 rounded-sm" :class="BAR[l.s]" />
          <span class="text-muted">{{ l.label }}</span>
          <span class="num font-medium text-highlighted">{{ l.n }}</span>
        </span>
      </UTooltip>
    </div>
    <ul class="space-y-1">
      <li v-for="t in rows" :key="t.theme_id">
        <NuxtLink :to="`/themes/${t.theme_id}`" class="group grid grid-cols-[1.5rem_minmax(0,13rem)_1fr_2rem] items-center gap-2 rounded-md px-1 py-1 hover:bg-elevated/50 sm:grid-cols-[1.5rem_minmax(0,19rem)_1fr_2rem_5.5rem]">
          <span class="num text-right text-xs text-dimmed">{{ t.strength_order }}</span>
          <span class="truncate text-sm group-hover:text-primary" :class="t.status === 'shown' ? 'font-medium text-highlighted' : 'text-default'">{{ t.name }}</span>
          <span class="h-2.5 overflow-hidden rounded-full bg-elevated">
            <span class="block h-full rounded-full" :class="BAR[t.status] ?? 'bg-accented'" :style="{ width: `${themesScore(t.strength) ?? 0}%` }" />
          </span>
          <span class="num text-right text-sm font-medium text-highlighted">{{ themesScore(t.strength) }}</span>
          <span class="hidden sm:block">
            <UBadge :label="THEMES_STATUS[t.status]?.label" :color="THEMES_STATUS[t.status]?.color" variant="subtle" size="xs" class="whitespace-nowrap" />
          </span>
        </NuxtLink>
      </li>
    </ul>
  </div>
</template>
