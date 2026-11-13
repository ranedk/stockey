<script setup lang="ts">
// One industry's rank over the last weeks; rank 1 (strongest) at the top. One measure, one line.
const props = defineProps<{ history: number[], ranked: number, height?: string }>()
const { t, base, axis, tooltip } = useChartTheme()
const option = computed(() => {
  const n = props.history.length
  const labels = props.history.map((_, k) => (k === n - 1 ? 'Now' : `${n - 1 - k}w ago`))
  return {
    ...base.value,
    grid: { left: 4, right: 28, top: 12, bottom: 4, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', valueFormatter: (v: number) => (isNum(v) ? `Rank ${v} of ${props.ranked}` : 'Not ranked') },
    xAxis: { type: 'category', data: labels, boundaryGap: false, ...axis.value, splitLine: { show: false },
      axisLabel: { ...axis.value.axisLabel, interval: (i: number) => (n - 1 - i) % 8 === 0, hideOverlap: true } },
    yAxis: { type: 'value', inverse: true, min: 1, max: Math.max(props.ranked, 2), splitNumber: 4, minInterval: 1, ...axis.value, axisLine: { show: false } },
    series: [{
      type: 'line', name: 'Rank', smooth: false, symbol: 'circle', symbolSize: 5, showSymbol: false,
      data: props.history.map(r => (r > 0 ? r : null)), connectNulls: false,
      lineStyle: { width: 2, color: t.value['series-1'] }, itemStyle: { color: t.value['series-1'] },
      areaStyle: { color: t.value['series-1'], opacity: 0.08, origin: 'start' },
      endLabel: { show: true, formatter: (p: any) => `#${p.value}`, color: t.value['chart-text'], fontWeight: 600 },
    }],
  }
})
</script>

<template>
  <EChart :option="option" :height="height || '220px'" />
</template>
