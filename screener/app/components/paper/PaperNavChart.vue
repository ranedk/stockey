<script setup lang="ts">
import { use } from 'echarts/core'
import { LegendComponent } from 'echarts/components'
// Lines for several books on one axis (one measure only). `mode`:
//   return: values are an index starting at 100, shown as % gained since the start
//   growth: same index on a log scale, shown as what ₹100 grew to (long replays)
//   lakh:   values already in ₹ lakh (profit and loss)
const props = withDefaults(defineProps<{
  points: { date: string, values: Record<string, number> }[]
  books: { key: string, label: string, color: string }[]
  mode?: 'return' | 'growth' | 'lakh'
  height?: string
}>(), { mode: 'return', height: '280px' })

// EChart.vue does not register the legend component; this chart needs it.
use([LegendComponent])

const { t, base, axis, tooltip, resolve } = useChartTheme()

const fmt = (v: number) => {
  if (props.mode === 'return') return pct(v / 100 - 1, 1, true)
  if (props.mode === 'growth') return `₹${num(Math.round(v))}`
  return `${v >= 0 ? '' : '−'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })} L`
}

const option = computed(() => {
  const dates = props.points.map(p => p.date.slice(0, 10))
  const long = dates.length > 400
  const shown = props.books.filter(b => props.points.some(p => p.values[b.key] != null))
  const series = shown.map(b => {
    const c = resolve(b.color)
    return {
      name: b.label, type: 'line', showSymbol: false, smooth: false, connectNulls: true,
      data: props.points.map(p => p.values[b.key] ?? null),
      lineStyle: { width: 2, color: c }, itemStyle: { color: c },
      emphasis: { focus: 'series' },
      z: b.key === 'strategy' ? 3 : 2,
    }
  })
  return {
    ...base.value,
    legend: shown.length > 1
      ? { type: 'scroll', top: 0, left: 0, icon: 'roundRect', itemWidth: 14, itemHeight: 3, itemGap: 16,
          textStyle: { color: t.value['chart-text'], fontSize: 12 } }
      : undefined,
    grid: { left: 8, right: 16, top: shown.length > 1 ? 40 : 12, bottom: 8, containLabel: true },
    tooltip: { ...tooltip.value, trigger: 'axis', order: 'valueDesc',
      axisPointer: { type: 'line', lineStyle: { color: t.value['chart-axis'] } },
      formatter: (ps: any[]) => {
        const head = `<div style="font-weight:600;margin-bottom:4px">${dateShort(dates[ps[0]?.dataIndex ?? 0])}</div>`
        return head + ps.filter(p => p.value != null).map(p =>
          `<div style="display:flex;justify-content:space-between;gap:16px">${p.marker}<span style="flex:1;opacity:.75">${p.seriesName}</span><b style="font-variant-numeric:tabular-nums">${fmt(p.value)}</b></div>`).join('')
      } },
    xAxis: { type: 'category', data: dates, boundaryGap: false, ...axis.value, splitLine: { show: false },
      axisLabel: { ...axis.value.axisLabel, hideOverlap: true,
        interval: (i: number, d: string) => {
          const p = dates[i - 1]
          if (!p) return false
          return long ? p.slice(0, 4) !== d.slice(0, 4) : p.slice(0, 7) !== d.slice(0, 7)
        },
        formatter: (d: string) => {
          const dt = new Date(d)
          return long ? String(dt.getFullYear()) : dt.toLocaleDateString('en-IN', { month: 'short', year: dt.getMonth() === 0 ? '2-digit' : undefined })
        } } },
    yAxis: { type: props.mode === 'growth' ? 'log' : 'value', scale: props.mode !== 'growth', ...axis.value,
      ...(props.mode === 'growth' ? { logBase: 2, min: (v: any) => v.min * 0.9, max: (v: any) => v.max * 1.1 } : {}),
      axisLine: { show: false }, axisLabel: { ...axis.value.axisLabel, formatter: fmt, showMinLabel: props.mode !== 'growth', showMaxLabel: props.mode !== 'growth' } },
    series,
  }
})
</script>

<template>
  <EChart v-if="points.length" :option="option" :height="height" />
  <UEmpty v-else icon="i-lucide-chart-line" title="No data yet" description="The line starts with the first recorded session." class="py-8" />
</template>
