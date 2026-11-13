<script setup lang="ts">
// Sectors as dots: sales growth across, capacity (gross block) growth up. Only the sectors
// out of balance are labelled. Above the dashed
// line, capacity is growing faster than sales. Colour follows the phase, which is also named
// in the legend and tooltip, so colour is never the only cue.
const props = defineProps<{ items: { label: string, sales: number, capacity: number, phase: string }[] }>()
const { t, base, axis, tooltip } = useChartTheme()
const PHASES: Record<string, { name: string, tone: 'up' | 'down' | 'axis' }> = {
  capacity_discipline: { name: 'Capacity held back', tone: 'up' },
  balanced: { name: 'Balanced', tone: 'axis' },
  capacity_expansion: { name: 'Capacity running ahead', tone: 'down' },
}
const option = computed(() => {
  const all = props.items.flatMap(i => [i.sales, i.capacity])
  const lo = Math.floor(Math.min(0, ...all) / 5) * 5
  const hi = Math.ceil(Math.max(...all) / 5) * 5
  const color = (tone: string) => tone === 'axis' ? t.value['chart-axis'] : t.value[tone]
  return {
    ...base.value,
    legend: { top: 0, left: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: t.value['chart-text'], fontSize: 12 } },
    grid: { left: 28, right: 24, top: 36, bottom: 28, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'item',
      formatter: (p: any) => p.data?.name ? `<b>${p.data.name}</b><br/>Sales growth ${p.data.value[0].toFixed(1)}%<br/>Capacity growth ${p.data.value[1].toFixed(1)}%<br/>${PHASES[p.data.phase]?.name}` : '' },
    xAxis: { type: 'value', min: lo, max: hi, name: 'Sales growth, %', nameLocation: 'middle', nameGap: 28, ...axis.value,
      nameTextStyle: { color: t.value['chart-text'] } },
    yAxis: { type: 'value', min: lo, max: hi, name: 'Capacity growth, %', nameLocation: 'middle', nameGap: 36, nameRotate: 90, ...axis.value,
      nameTextStyle: { color: t.value['chart-text'] } },
    // one series holds every dot so overlapping labels can be hidden across all of them;
    // the three empty series only give the legend its entries
    series: [
      ...Object.values(PHASES).map(ph => ({ name: ph.name, type: 'scatter', data: [], itemStyle: { color: color(ph.tone) } })),
      { type: 'scatter', symbolSize: 11, labelLayout: { hideOverlap: true },
        label: { show: true, position: 'right', formatter: (p: any) => p.name, color: t.value['chart-text'], fontSize: 11 },
        data: props.items.map(i => ({
          value: [i.sales, i.capacity], name: i.label, phase: i.phase,
          itemStyle: { color: color(PHASES[i.phase]?.tone || 'axis'), borderColor: t.value['chart-surface'], borderWidth: 2 },
          // balanced sectors crowd the middle: their names are in the tooltip and the cards below
          label: { show: i.phase !== 'balanced', ...(i.sales > lo + (hi - lo) * 0.75 ? { position: 'left' } : {}) },
        })),
        markLine: { silent: true, symbol: 'none', lineStyle: { type: 'dashed', color: t.value['chart-axis'], width: 1 },
          label: { show: true, position: 'insideEndTop', formatter: 'same pace', color: t.value['chart-text'], fontSize: 11 },
          data: [[{ coord: [lo, lo] }, { coord: [hi, hi] }]] } },
    ],
  }
})
</script>

<template>
  <EChart :option="option" height="420px" />
</template>
