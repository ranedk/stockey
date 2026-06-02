<script setup lang="ts">
const props = defineProps<{
  error: unknown
  title?: string
}>()

const message = computed(() => {
  const err = props.error as any
  const detail = err?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    const parts = [detail.message, detail.error_type, detail.operation, detail.route].filter(Boolean)
    if (parts.length) return parts.join(' · ')
  }
  return err?.message || String(props.error || '')
})

const statusCode = computed(() => {
  const err = props.error as any
  return err?.statusCode || err?.status || err?.response?.status || ''
})
</script>

<template>
  <div v-if="error" class="rounded-3xl border border-rust/25 bg-rust/10 p-4 text-rust">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <p class="text-sm font-black">{{ title || 'API request failed' }}</p>
        <p class="mt-1 text-sm leading-6">{{ message }}</p>
      </div>
      <span v-if="statusCode" class="rounded-full bg-rust px-3 py-1 text-xs font-black text-paper">HTTP {{ statusCode }}</span>
    </div>
    <p class="mt-2 text-xs font-semibold text-rust/75">
      This failure is also recorded in Operator API Errors when the backend can reach Postgres.
    </p>
  </div>
</template>
