<script setup lang="ts">
// Price (candles or line) with 50- and 200-day averages, volume underneath on its own panel
// (two stacked panels, never a second y-scale on one plot). Averages are computed on the full
// history sent by the API (it adds 300 days before the visible range) so they start defined.
type Bar = { d: string, o: number, h: number, l: number, c: number, v: number }
const props = defineProps<{ bars: Bar[], visibleFrom: string, mode: 'line' | 'candle', showAvg?: boolean }>()
const { t, base, axis, tooltip } = useChartTheme()

function sma(vals: number[], n: number) {
  const out: (number | null)[] = []
  let s = 0
  vals.forEach((v, i) => { s += v; if (i >= n) s -= vals[i - n]!; out.push(i >= n - 1 ? s / n : null) })
  return out
}

const view = computed(() => {
  const all = props.bars
  const c = all.map(b => b.c)
  const m50 = sma(c, 50), m200 = sma(c, 200)
  const start = Math.max(0, all.findIndex(b => b.d >= props.visibleFrom))
  const bars = all.slice(start)
  return { bars, m50: m50.slice(start), m200: m200.slice(start), prev: all.slice(start - 1, start)[0] }
})

const option = computed(() => {
  const { bars, m50, m200 } = view.value
  const up = t.value.up, down = t.value.down
  const dates = bars.map(b => b.d)
  const first = bars[0]?.c ?? 0
  const last = bars.at(-1)?.c ?? 0
  const lineColor = t.value['series-1']
  const series: any[] = []
  if (props.mode === 'candle') {
    series.push({ name: 'Price', type: 'candlestick', data: bars.map(b => [b.o, b.c, b.l, b.h]), barMaxWidth: 8,
      itemStyle: { color: up, color0: down, borderColor: up, borderColor0: down } })
  }
  else {
    series.push({ name: 'Price', type: 'line', data: bars.map(b => b.c), showSymbol: false, smooth: false,
      lineStyle: { width: 2, color: lineColor }, itemStyle: { color: lineColor },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1, colorStops: [
        { offset: 0, color: lineColor + '33' }, { offset: 1, color: lineColor + '00' }] } },
      markLine: { symbol: 'none', silent: true, label: { show: false },
        lineStyle: { color: t.value['chart-axis'], type: 'solid', width: 1, opacity: 0.5 },
        data: [{ yAxis: first }] } })
  }
  if (props.showAvg !== false) {
    series.push({ name: '50-day avg', type: 'line', data: m50, showSymbol: false, lineStyle: { width: 1.5, color: t.value['series-2'] }, itemStyle: { color: t.value['series-2'] }, z: 3 })
    series.push({ name: '200-day avg', type: 'line', data: m200, showSymbol: false, lineStyle: { width: 1.5, color: t.value['series-7'] }, itemStyle: { color: t.value['series-7'] }, z: 3 })
  }
  series.push({ name: 'Volume', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, barMaxWidth: 6,
    data: bars.map((b, i) => ({ value: b.v, itemStyle: { color: (i === 0 ? b.c >= b.o : b.c >= bars[i - 1]!.c) ? up + '66' : down + '66', borderRadius: [2, 2, 0, 0] } })) })

  return {
    ...base.value,
    grid: [{ left: 8, right: 56, top: 12, height: '68%' }, { left: 8, right: 56, top: '80%', bottom: 24 }],
    axisPointer: { link: [{ xAxisIndex: 'all' }], label: { backgroundColor: t.value['chart-axis'] } },
    tooltip: { ...tooltip.value, trigger: 'axis', axisPointer: { type: 'cross', crossStyle: { color: t.value['chart-axis'] }, lineStyle: { color: t.value['chart-axis'], type: 'solid' } },
      formatter: (ps: any[]) => {
        const i = ps[0]?.dataIndex ?? 0
        const b = bars[i]!
        const prev = bars[i - 1]
        const ch = prev ? b.c / prev.c - 1 : null
        const row = (k: string, v: string) => `<div style="display:flex;justify-content:space-between;gap:16px"><span style="opacity:.65">${k}</span><b style="font-variant-numeric:tabular-nums">${v}</b></div>`
        return `<div style="min-width:170px"><div style="margin-bottom:4px;font-weight:600">${dateShort(b.d)}</div>`
          + row('Open', inr(b.o)) + row('High', inr(b.h)) + row('Low', inr(b.l)) + row('Close', inr(b.c))
          + (ch != null ? row('Change', `<span style="color:${ch >= 0 ? up : down}">${pct(ch, 2, true)}</span>`) : '')
          + (m50[i] ? row('50-day avg', inr(m50[i])) : '') + (m200[i] ? row('200-day avg', inr(m200[i])) : '')
          + row('Volume', num(b.v)) + '</div>'
      } },
    xAxis: [
      { type: 'category', data: dates, boundaryGap: props.mode === 'candle', ...axis.value, axisLabel: { show: false }, splitLine: { show: false } },
      { type: 'category', gridIndex: 1, data: dates, boundaryGap: true, ...axis.value, splitLine: { show: false },
        axisLabel: { ...axis.value.axisLabel, hideOverlap: true,
          // one label per month (or per year on long ranges), on the first session of it
          interval: (i: number, d: string) => {
            const p = dates[i - 1]
            if (!p) return false
            const long = dates.length > 600
            return long ? p.slice(0, 4) !== d.slice(0, 4) : p.slice(0, 7) !== d.slice(0, 7)
          },
          formatter: (d: string) => {
            const dt = new Date(d)
            return dates.length > 600 ? String(dt.getFullYear()) : dt.toLocaleDateString('en-IN', { month: 'short', year: dt.getMonth() === 0 ? 'numeric' : undefined })
          } } },
    ],
    yAxis: [
      { type: 'value', scale: true, position: 'right', ...axis.value, axisLine: { show: false },
        axisLabel: { ...axis.value.axisLabel, formatter: (v: number) => num(v) } },
      { type: 'value', gridIndex: 1, position: 'right', ...axis.value, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }],
    series,
  }
})
</script>

<template>
  <EChart :option="option" height="400px" />
</template>
