<script setup lang="ts">
// One measure per chart (never two y-scales). Columns grow from zero; losses drawn in the
// down colour; the latest value is labelled on its cap, the rest live in the tooltip.
const props = defineProps<{ rows: { label: string, value: number | null }[], format: (v: number) => string,
  color?: string, height?: string, kind?: 'bar' | 'line' }>()
const { t, base, axis, tooltip, resolve } = useChartTheme()
const option = computed(() => {
  const c = resolve(props.color) || t.value['series-1']
  const vals = props.rows.map(r => r.value)
  const lastI = vals.length - 1
  const isLine = props.kind === 'line'
  return {
    ...base.value,
    grid: { left: 4, right: 8, top: 22, bottom: 4, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: isLine ? 'line' : 'shadow', lineStyle: { color: t.value['chart-axis'] } },
      valueFormatter: (v: number) => (isNum(v) ? props.format(v) : '—') },
    xAxis: { type: 'category', data: props.rows.map(r => r.label), ...axis.value, splitLine: { show: false },
      axisLabel: { ...axis.value.axisLabel, hideOverlap: true, interval: 'auto' } },
    yAxis: { type: 'value', ...axis.value, axisLine: { show: false }, splitNumber: 3, scale: isLine,
      axisLabel: { ...axis.value.axisLabel, formatter: (v: number) => props.format(v) } },
    series: [isLine
      ? { type: 'line', data: vals, showSymbol: true, symbolSize: (v: any, p: any) => (p.dataIndex === lastI ? 8 : 0),
          lineStyle: { width: 2, color: c }, itemStyle: { color: c, borderColor: t.value['chart-surface'], borderWidth: 2 },
          areaStyle: { color: c + '1a' }, connectNulls: true,
          label: { show: true, position: 'top', formatter: (p: any) => (p.dataIndex === lastI && isNum(p.value) ? props.format(p.value) : ''), color: t.value['chart-text'], fontWeight: 600 } }
      : { type: 'bar', barMaxWidth: 22, data: vals.map((v, i) => ({ value: v,
          itemStyle: { color: isNum(v) && v < 0 ? t.value.down : (i === lastI ? c : c + 'b3'), borderRadius: isNum(v) && v < 0 ? [0, 0, 4, 4] : [4, 4, 0, 0] } })),
          label: { show: true, position: 'top', formatter: (p: any) => (p.dataIndex === lastI && isNum(p.value) ? props.format(p.value) : ''), color: t.value['chart-text'], fontWeight: 600, fontSize: 11 } }],
  }
})
</script>

<template>
  <EChart :option="option" :height="height || '200px'" />
</template>
