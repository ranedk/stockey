<script setup lang="ts">
import type { NavigationMenuItem } from '@nuxt/ui'
import { watchDebounced } from '@vueuse/core'

const open = ref(false)
const nav: NavigationMenuItem[][] = [[
  { label: 'Markets', icon: 'i-lucide-layout-dashboard', to: '/' },
  { label: 'Screener', icon: 'i-lucide-filter', to: '/screener' },
  { label: 'Sectors', icon: 'i-lucide-blocks', to: '/sectors' },
], [
  { label: 'Themes', icon: 'i-lucide-sparkles', badge: 'Soon', disabled: true },
  { label: 'Portfolios', icon: 'i-lucide-briefcase', badge: 'Soon', disabled: true },
  { label: 'Alerts', icon: 'i-lucide-bell', badge: 'Soon', disabled: true },
  { label: 'Research', icon: 'i-lucide-flask-conical', badge: 'Soon', disabled: true },
]]

// company search: server-side, so the palette shows whatever the API returns
const term = ref('')
const results = ref<any[]>([])
const loading = ref(false)
watchDebounced(term, async (q) => {
  if (!q?.trim()) { results.value = []; return }
  loading.value = true
  try { results.value = await api<any[]>('search', { q, limit: 10 }) } finally { loading.value = false }
}, { debounce: 150 })

const groups = computed(() => [
  {
    id: 'companies', label: term.value ? 'Companies' : undefined, ignoreFilter: true,
    items: results.value.map(r => ({
      label: r.name, suffix: r.symbol, description: r.industry, icon: 'i-lucide-building-2',
      to: `/company/${encodeURIComponent(r.symbol)}`, onSelect: () => { open.value = false },
    })),
  },
  {
    id: 'pages', label: 'Go to',
    items: [
      { label: 'Markets', icon: 'i-lucide-layout-dashboard', to: '/' },
      { label: 'Screener', icon: 'i-lucide-filter', to: '/screener' },
      { label: 'Sectors', icon: 'i-lucide-blocks', to: '/sectors' },
    ],
  },
])

const { data: fresh } = useApi<any>('meta/freshness', { lazy: true })
</script>

<template>
  <UDashboardGroup unit="rem">
    <UDashboardSidebar v-model:open="open" collapsible resizable :default-size="15" :min-size="13" :max-size="20"
      class="bg-elevated/40" :ui="{ footer: 'lg:border-t lg:border-default' }">
      <template #header="{ collapsed }">
        <NuxtLink to="/" class="flex items-center gap-2 px-1">
          <span class="grid size-7 place-items-center rounded-lg bg-primary text-white shadow-sm">
            <UIcon name="i-lucide-chart-candlestick" class="size-4" />
          </span>
          <span v-if="!collapsed" class="font-semibold tracking-tight text-highlighted">Stockey</span>
          <UBadge v-if="!collapsed" label="Beta" size="xs" variant="subtle" class="ml-auto" />
        </NuxtLink>
      </template>

      <template #default="{ collapsed }">
        <UDashboardSearchButton :collapsed="collapsed" class="bg-transparent ring-default" label="Search companies" />
        <UNavigationMenu :collapsed="collapsed" :items="nav[0]" orientation="vertical" tooltip />
        <div v-if="!collapsed" class="mt-4 px-2.5 text-[11px] font-medium uppercase tracking-wider text-dimmed">Coming next</div>
        <UNavigationMenu :collapsed="collapsed" :items="nav[1]" orientation="vertical" tooltip />
      </template>

      <template #footer="{ collapsed }">
        <div v-if="!collapsed" class="w-full space-y-1 px-1 text-[11px] text-muted">
          <div class="flex items-center gap-1.5">
            <span class="size-1.5 rounded-full bg-[var(--up)]" />
            Prices as of {{ dateShort(fresh?.prices?.as_of) }}
          </div>
          <div class="text-dimmed">End-of-day data · NSE</div>
        </div>
        <UColorModeButton v-else />
      </template>
    </UDashboardSidebar>

    <UDashboardSearch v-model:search-term="term" :groups="groups" :loading="loading" placeholder="Search a company or symbol…"
      :fuse="{ resultLimit: 20 }" />

    <slot />
  </UDashboardGroup>
</template>
