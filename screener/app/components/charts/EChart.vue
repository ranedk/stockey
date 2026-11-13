<script setup lang="ts">
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, CandlestickChart, LineChart, TreemapChart, HeatmapChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent, MarkPointComponent,
  AxisPointerComponent, VisualMapComponent, TitleComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'

use([CanvasRenderer, BarChart, CandlestickChart, LineChart, TreemapChart, HeatmapChart, ScatterChart,
  GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent, MarkPointComponent,
  AxisPointerComponent, VisualMapComponent, TitleComponent, LegendComponent])

defineProps<{ option: Record<string, any>, height?: string }>()
const emit = defineEmits<{ click: [params: any] }>()
</script>

<template>
  <ClientOnly>
    <VChart :option="option" autoresize :style="{ height: height || '280px', width: '100%' }" @click="(p: any) => emit('click', p)" />
    <template #fallback>
      <USkeleton :style="{ height: height || '280px' }" class="w-full" />
    </template>
  </ClientOnly>
</template>
