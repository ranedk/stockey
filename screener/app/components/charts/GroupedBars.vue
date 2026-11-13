<script setup lang="ts">
// Two or three related rupee measures side by side (e.g. operating cash flow vs free cash
// flow). One scale; a legend always shown; fixed slot colours in order.
const props = defineProps<{ labels: string[], series: { name: string, values: (number | null)[] }[], format: (v: number) => string, height?: string }>()
const { t, base, axis, tooltip } = useChartTheme()
const SLOTS = ['series-1', 'series-2', 'series-3'] as const
const option = computed(() => ({
  ...base.value,
  grid: { left: 4, right: 8, top: 12, bottom: 4, containLabel: true },
  tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'shadow' }, valueFormatter: (v: number) => (isNum(v) ? props.format(v) : '—') },
  xAxis: { type: 'category', data: props.labels, ...axis.value, splitLine: { show: false } },
  yAxis: { type: 'value', ...axis.value, axisLine: { show: false }, splitNumber: 3, axisLabel: { ...axis.value.axisLabel, formatter: (v: number) => props.format(v) } },
  series: props.series.map((s, k) => ({ name: s.name, type: 'bar', barMaxWidth: 16, barGap: '15%',
    data: s.values.map(v => ({ value: v, itemStyle: { color: t.value[SLOTS[k]!], borderRadius: isNum(v) && v < 0 ? [0, 0, 4, 4] : [4, 4, 0, 0] } })) })),
}))
</script>

<template>
  <div>
    <div class="mb-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      <span v-for="(s, k) in series" :key="s.name" class="inline-flex items-center gap-1.5">
        <span class="size-2 rounded-sm" :style="{ background: `var(--${SLOTS[k]})` }" />{{ s.name }}
      </span>
    </div>
    <EChart :option="option" :height="height || '200px'" />
  </div>
</template>
