// Chart colours come from the CSS tokens in main.css, re-read when the colour mode flips, so
// light and dark each use their own validated steps rather than an automatic inversion.
const TOKENS = ['chart-surface', 'chart-grid', 'chart-axis', 'chart-text', 'up', 'down',
  'series-1', 'series-2', 'series-3', 'series-4', 'series-5', 'series-6', 'series-7', 'series-8'] as const
type Tok = typeof TOKENS[number]

export function useChartTheme() {
  const mode = useColorMode()
  const t = ref<Record<Tok, string>>({} as Record<Tok, string>)
  const read = () => {
    if (!import.meta.client) return
    const cs = getComputedStyle(document.documentElement)
    const out = {} as Record<Tok, string>
    for (const k of TOKENS) out[k] = cs.getPropertyValue(`--${k}`).trim()
    t.value = out
  }
  onMounted(() => nextTick(read))
  watch(() => mode.value, () => nextTick(read))
  const base = computed(() => ({
    backgroundColor: 'transparent',
    textStyle: { fontFamily: 'Inter, ui-sans-serif, system-ui', color: t.value['chart-text'], fontSize: 11 },
    animationDuration: 350,
  }))
  const axis = computed(() => ({
    axisLine: { lineStyle: { color: t.value['chart-grid'] } },
    axisTick: { show: false },
    axisLabel: { color: t.value['chart-axis'], fontSize: 11 },
    splitLine: { lineStyle: { color: t.value['chart-grid'], width: 1 } },
  }))
  const tooltip = computed(() => ({
    backgroundColor: mode.value === 'dark' ? '#232326' : '#ffffff',
    borderColor: t.value['chart-grid'],
    borderWidth: 1,
    padding: [8, 10],
    textStyle: { color: mode.value === 'dark' ? '#f0efec' : '#0b0b0b', fontSize: 12 },
    extraCssText: 'box-shadow: 0 4px 16px rgb(0 0 0 / 0.12); border-radius: 8px;',
  }))
  // ECharts paints on canvas and cannot read CSS variables: turn 'var(--x)' into its value
  const resolve = (c?: string) => {
    const m = c?.match(/^var\(--([\w-]+)\)$/)
    return m ? (t.value as Record<string, string>)[m[1]!] || c : c
  }
  return { t, base, axis, tooltip, resolve, ready: computed(() => !!t.value['chart-grid']) }
}
