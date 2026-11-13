<script setup lang="ts">
// Treemap: area = combined market cap, colour = median return (diverging red-grey-green,
// neutral grey at zero). Each tile is labelled with its value, so colour is never the only cue.
const props = defineProps<{ items: { name: string, mcap: number, value: number | null, n: number }[], metric: string, cap?: number }>()
const emit = defineEmits<{ pick: [name: string] }>()
const { t, base, tooltip } = useChartTheme()
const mode = useColorMode()
const option = computed(() => {
  const dark = mode.value === 'dark'
  return {
    ...base.value,
    tooltip: { ...tooltip.value, formatter: (p: any) =>
      `<b>${p.name}</b><br/>Median ${props.metric} return: <b>${pct(p.data.ret, 1, true)}</b><br/>${p.data.n} companies · ${croreShort(p.value)}` },
    series: [{
      type: 'treemap', roam: false, nodeClick: false, breadcrumb: { show: false },
      left: 0, right: 0, top: 0, bottom: 0,
      itemStyle: { borderColor: t.value['chart-surface'], borderWidth: 2, gapWidth: 2, borderRadius: 6 },
      label: { show: true, formatter: (p: any) => `{n|${p.name}}\n{v|${pct(p.data.ret, 1, true)}}`,
        rich: { n: { fontSize: 12, fontWeight: 600, color: dark ? '#f5f5f4' : '#1c1917', lineHeight: 16 },
          v: { fontSize: 11, color: dark ? '#e7e5e4' : '#44403c' } } },
      upperLabel: { show: false },
      data: props.items.filter(i => i.mcap > 0).map(i => ({ name: i.name, value: i.mcap, ret: i.value, n: i.n,
        itemStyle: { color: diverging(i.value, props.cap ?? 0.1, dark) } })),
    }],
  }
})
</script>

<template>
  <EChart :option="option" height="360px" @click="(p: any) => emit('pick', p.name)" />
</template>
