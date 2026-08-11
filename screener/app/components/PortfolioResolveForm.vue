<script setup lang="ts">
import type { PortfolioResolvePayload } from '~/types/api'

const props = defineProps<{ thesisId: string }>()
const emit = defineEmits<{ resolved: []; cancel: [] }>()

const api = useApi()
const submitting = ref(false)
const submitError = ref<string | null>(null)

const resolvedTrue = ref<'true' | 'false'>('true')
const resolutionNotes = ref('')
const failureAttribution = ref<'thesis_wrong' | 'thesis_right_market_hasnt_paid'>('thesis_wrong')

async function submit() {
  submitting.value = true
  submitError.value = null
  const payload: PortfolioResolvePayload = {
    resolved_true: resolvedTrue.value === 'true',
    resolution_notes: resolutionNotes.value || null,
    failure_attribution: resolvedTrue.value === 'false' ? failureAttribution.value : null,
  }
  try {
    await api.post(`/api/portfolio/${encodeURIComponent(props.thesisId)}/resolve`, payload)
    emit('resolved')
  } catch (err: any) {
    submitError.value = err?.data?.detail || err?.message || 'Failed to resolve thesis'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4" @submit.prevent="submit">
    <div>
      <label class="text-xs font-medium text-slate-600">Outcome</label>
      <select v-model="resolvedTrue" class="mt-1 w-full rounded border border-slate-300 p-2 text-sm">
        <option value="true">Prediction came true</option>
        <option value="false">Prediction did not come true</option>
      </select>
    </div>
    <div v-if="resolvedTrue === 'false'">
      <label class="text-xs font-medium text-slate-600">Why -- these look identical in P&amp;L, be honest</label>
      <select v-model="failureAttribution" class="mt-1 w-full rounded border border-slate-300 p-2 text-sm">
        <option value="thesis_wrong">Thesis was wrong</option>
        <option value="thesis_right_market_hasnt_paid">Thesis was right, market hasn't paid yet</option>
      </select>
    </div>
    <div>
      <label class="text-xs font-medium text-slate-600">Notes</label>
      <textarea v-model="resolutionNotes" rows="2" class="mt-1 w-full rounded border border-slate-300 p-2 text-sm" />
    </div>
    <p v-if="submitError" class="text-sm text-rose-600">{{ submitError }}</p>
    <div class="flex gap-2">
      <button type="submit" :disabled="submitting" class="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
        {{ submitting ? 'Saving…' : 'Resolve' }}
      </button>
      <button type="button" class="rounded px-3 py-1.5 text-sm text-slate-600 hover:text-slate-900" @click="emit('cancel')">Cancel</button>
    </div>
  </form>
</template>
