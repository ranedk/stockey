import type { EventTrace, HypothesesPayload, HypothesisCreateResult, HypothesisPreviewResult, HypothesisPromotionAuditResult, HypothesisRunResult, OperatorActions, OperatorEvents, OperatorMarketContext, OperatorPortfolio, OperatorSummary, SymbolTrace, TraceSummary } from '~/types/api'

export function useOperatorApi() {
  const config = useRuntimeConfig()
  const apiBase = String(config.public.apiBase || '').replace(/\/$/, '')

  const get = async <T>(path: string): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`)
  }
  const post = async <T>(path: string, body: unknown): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`, { method: 'POST', body })
  }

  return {
    getSummary: () => get<OperatorSummary>('/api/summary'),
    getActions: () => get<OperatorActions>('/api/actions'),
    getPortfolio: () => get<OperatorPortfolio>('/api/portfolio'),
    getMarketContext: (limit = 50) => get<OperatorMarketContext>(`/api/market-context?limit=${limit}`),
    getEvents: (limit = 50) => get<OperatorEvents>(`/api/events?limit=${limit}`),
    getEventTrace: (uniqueId: string) => get<EventTrace>(`/api/events/${encodeURIComponent(uniqueId)}/trace`),
    getEventTraceSummary: (uniqueId: string) => get<TraceSummary>(`/api/events/${encodeURIComponent(uniqueId)}/trace/summary`),
    getSymbolTrace: (symbol: string, limit = 200) => get<SymbolTrace>(`/api/symbols/${encodeURIComponent(symbol)}/trace?limit=${limit}`),
    getSymbolTraceSummary: (symbol: string, limit = 200) => get<TraceSummary>(`/api/symbols/${encodeURIComponent(symbol)}/trace/summary?limit=${limit}`),
    getHypotheses: (limit = 100) => get<HypothesesPayload>(`/api/hypotheses?limit=${limit}`),
    createHypothesis: (body: Record<string, unknown>) => post<HypothesisCreateResult>('/api/hypotheses', body),
    updateHypothesis: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisCreateResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}`, body),
    previewHypothesis: (body: Record<string, unknown>) => post<HypothesisPreviewResult>('/api/hypotheses/preview', body),
    runPromotionAudit: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisPromotionAuditResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}/promotion-audit`, body),
    runHypotheses: (body: Record<string, unknown>) => post<HypothesisRunResult>('/api/hypotheses/run', body)
  }
}
