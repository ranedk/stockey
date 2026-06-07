<script setup lang="ts">
import type { Dict } from '~/types/api'

const props = defineProps<{
  title: string
  subtitle?: string
  record: Dict
  detailPath?: string
  showSymbol?: boolean
}>()

const config = useRuntimeConfig()
const apiBase = String(config.public.apiBase || '').replace(/\/$/, '')
const symbol = computed(() => props.record.symbol || props.record.ticker)
const detailsOpen = ref(false)
const detailLoading = ref(false)
const detailError = ref('')
const detailPayload = ref<unknown>(null)

async function loadDetail(event: Event) {
  const target = event.target as HTMLDetailsElement
  detailsOpen.value = Boolean(target.open)
  if (!detailsOpen.value || !props.detailPath || detailPayload.value || detailLoading.value) return
  detailLoading.value = true
  detailError.value = ''
  try {
    detailPayload.value = await $fetch(`${apiBase}${props.detailPath}`)
  } catch (err) {
    detailError.value = err instanceof Error ? err.message : String(err)
  } finally {
    detailLoading.value = false
  }
}

const rawPayload = computed(() => detailPayload.value || props.record)
</script>

<template>
  <article class="rounded-3xl border border-black/10 bg-gradient-to-br from-white/85 via-white/70 to-sky/10 p-5 shadow-soft backdrop-blur">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-base font-black text-ink">{{ title }}</h3>
        <p v-if="subtitle" class="mt-1 text-sm text-ink/60">{{ subtitle }}</p>
      </div>
      <div class="flex flex-wrap justify-end gap-2">
        <SymbolLink v-if="symbol && props.showSymbol !== false" :symbol="symbol" subtle />
        <slot name="badge" />
      </div>
    </div>
    <slot />
    <details class="mt-4 rounded-2xl border border-black/10 bg-white/55 px-4 py-3" @toggle="loadDetail">
      <summary class="cursor-pointer text-sm font-black text-moss">
        {{ detailPath ? 'Load raw details' : 'Show raw details' }}
      </summary>
      <p v-if="detailLoading" class="mt-3 rounded-2xl bg-white/70 p-3 text-sm font-bold text-ink/60">Loading detail payload...</p>
      <p v-else-if="detailError" class="mt-3 rounded-2xl bg-rust/10 p-3 text-sm font-bold text-rust">{{ detailError }}</p>
      <pre v-else class="mt-3 max-h-80 overflow-auto rounded-2xl bg-ink p-4 text-xs leading-5 text-paper">{{ JSON.stringify(rawPayload, null, 2) }}</pre>
    </details>
  </article>
</template>
