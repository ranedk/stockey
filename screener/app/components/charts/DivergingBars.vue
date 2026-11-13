<script setup lang="ts">
// Horizontal bars around zero, sorted; values labelled at the bar tip (text in ink, not data colour).
const props = defineProps<{ items: { label: string, value: number | null }[], height?: string }>()
const { t, base, axis, tooltip } = useChartTheme()
const option = computed(() => {
  const rows = props.items.filter(i => isNum(i.value)).sort((a, b) => a.value! - b.value!)
  return {
    ...base.value,
    grid: { left: 8, right: 56, top: 4, bottom: 4, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'shadow' },
      valueFormatter: (v: number) => pct(v, 2, true) },
    xAxis: { type: 'value', ...axis.value, axisLabel: { show: false }, splitLine: { show: false } },
    yAxis: { type: 'category', data: rows.map(r => r.label), ...axis.value,
      axisLine: { show: false }, axisLabel: { color: t.value['chart-text'], fontSize: 12 } },
    series: [{
      type: 'bar', barMaxWidth: 14, barCategoryGap: '35%',
      data: rows.map(r => ({
        value: r.value,
        itemStyle: { color: r.value! >= 0 ? t.value.up : t.value.down,
          borderRadius: r.value! >= 0 ? [0, 4, 4, 0] : [4, 0, 0, 4] },
        label: { show: true, position: 'right', formatter: pct(r.value, 1, true),
          color: t.value['chart-text'], fontSize: 11, fontWeight: 500 },
      })),
    }],
  }
})
</script>

<template>
  <EChart :option="option" :height="height || `${Math.max(160, items.length * 26)}px`" />
</template>
