<script setup lang="ts">
import type { PortfolioCreatePayload, PortfolioThesis } from '~/types/api'

const props = defineProps<{
  companyMasterId: string
  prefillText: string
  sourceAlert?: { source: string; newsId: string; triggerType: string } | null
}>()

const emit = defineEmits<{ created: [PortfolioThesis]; cancel: [] }>()

const api = useApi()
const submitting = ref(false)
const submitError = ref<string | null>(null)

const defaultTargetDate = new Date(Date.now() + 180 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10)

const form = reactive({
  prediction_text: props.prefillText,
  target_date: defaultTargetDate,
  invalidation_criteria: '',
  origin_tag: (props.sourceAlert ? 'systematic_screen' : 'ad_hoc') as 'systematic_screen' | 'ad_hoc',
})

async function submit() {
  submitting.value = true
  submitError.value = null
  const payload: PortfolioCreatePayload = {
    company_master_id: props.companyMasterId,
    prediction_text: form.prediction_text,
    target_date: form.target_date,
    invalidation_criteria: form.invalidation_criteria,
    origin_tag: form.origin_tag,
    source_alert_source: props.sourceAlert?.source ?? null,
    source_alert_news_id: props.sourceAlert?.newsId ?? null,
    source_alert_trigger_type: props.sourceAlert?.triggerType ?? null,
  }
  try {
    const created = await api.post<PortfolioThesis>('/api/portfolio', payload)
    emit('created', created)
  } catch (err: any) {
    submitError.value = err?.data?.detail || err?.message || 'Failed to create thesis'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <form class="space-y-3 rounded-lg border border-slate-200 bg-slate-50 p-4" @submit.prevent="submit">
    <div>
      <label class="text-xs font-medium text-slate-600">Prediction (falsifiable claim)</label>
      <textarea v-model="form.prediction_text" rows="3" required class="mt-1 w-full rounded border border-slate-300 p-2 text-sm" />
      <p class="mt-1 text-xs text-slate-400">Pre-filled from the narrative -- edit into an actual falsifiable claim before saving.</p>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="text-xs font-medium text-slate-600">Target date</label>
        <input v-model="form.target_date" type="date" required class="mt-1 w-full rounded border border-slate-300 p-2 text-sm" />
      </div>
      <div>
        <label class="text-xs font-medium text-slate-600">Origin</label>
        <select v-model="form.origin_tag" class="mt-1 w-full rounded border border-slate-300 p-2 text-sm">
          <option value="systematic_screen">systematic_screen</option>
          <option value="ad_hoc">ad_hoc</option>
        </select>
      </div>
    </div>
    <div>
      <label class="text-xs font-medium text-slate-600">Invalidation criteria</label>
      <textarea v-model="form.invalidation_criteria" rows="2" required placeholder="What would prove this thesis wrong?" class="mt-1 w-full rounded border border-slate-300 p-2 text-sm" />
    </div>
    <p v-if="submitError" class="text-sm text-rose-600">{{ submitError }}</p>
    <div class="flex gap-2">
      <button type="submit" :disabled="submitting" class="rounded bg-slate-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50">
        {{ submitting ? 'Saving…' : 'Add to portfolio' }}
      </button>
      <button type="button" class="rounded px-3 py-1.5 text-sm text-slate-600 hover:text-slate-900" @click="emit('cancel')">Cancel</button>
    </div>
  </form>
</template>
