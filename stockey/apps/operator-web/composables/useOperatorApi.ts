import type { EventPolicyEvaluationPayload, EventPolicyPayload, EventTrace, HypothesesPayload, HypothesisCreateResult, HypothesisPreviewResult, HypothesisPromotionAuditResult, HypothesisRunResult, OperatorActions, OperatorEvents, OperatorHealthDetails, OperatorMarketContext, OperatorPortfolio, OperatorSummary, SymbolTrace, TechnicalCalibrationPayload, TechnicalPromotionDecisionResult, TechnicalPromotionReviewResult, TechnicalPromotionReviewsPayload, TraceSummary } from '~/types/api'

export function useOperatorApi() {
  const config = useRuntimeConfig()
  const apiBase = String(config.public.apiBase || '').replace(/\/$/, '')

  const get = async <T>(path: string): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`)
  }
  const post = async <T>(path: string, body: Record<string, unknown>): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`, { method: 'POST', body })
  }

  return {
    getSummary: () => get<OperatorSummary>('/api/summary'),
    getHealthDetails: () => get<OperatorHealthDetails>('/api/health/details'),
    getActions: () => get<OperatorActions>('/api/actions'),
    getPortfolio: () => get<OperatorPortfolio>('/api/portfolio'),
    getMarketContext: (limit = 50) => get<OperatorMarketContext>(`/api/market-context?limit=${limit}`),
    getTechnicalCalibration: (limit = 25) => get<TechnicalCalibrationPayload>(`/api/technical-calibration?limit=${limit}`),
    reviewTechnicalCalibration: (body: Record<string, unknown>) => post<TechnicalPromotionReviewResult>('/api/technical-calibration/promotion-review', body),
    getTechnicalPromotionReviews: (limit = 25) => get<TechnicalPromotionReviewsPayload>(`/api/technical-calibration/promotion-reviews?limit=${limit}`),
    decideTechnicalPromotionReview: (body: Record<string, unknown>) => post<TechnicalPromotionDecisionResult>('/api/technical-calibration/promotion-review/decision', body),
    getEvents: (limit = 50) => get<OperatorEvents>(`/api/events?limit=${limit}`),
    getEventPolicy: (limit = 100, actionType = 'ALL') => get<EventPolicyPayload>(`/api/event-policy?limit=${limit}&action_type=${encodeURIComponent(actionType)}`),
    getEventPolicyEvaluation: (limit = 100) => get<EventPolicyEvaluationPayload>(`/api/event-policy/evaluation?limit=${limit}`),
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
