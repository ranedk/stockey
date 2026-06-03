import type { CronLogsPayload, EventModelArtifactsPayload, EventModelPromotionCheckPayload, EventPolicyEvaluationPayload, EventPolicyPayload, EventTrace, HypothesesPayload, HypothesisCreateResult, HypothesisPreviewResult, HypothesisPromotionAuditResult, HypothesisRunResult, ManualReviewDecisionResult, ManualReviewPayload, OperatorActions, OperatorApiErrorsPayload, OperatorCommandRunResult, OperatorCommandsPayload, OperatorEvents, OperatorHealthDetails, OperatorHome, OperatorMarketContext, OperatorPortfolio, OperatorSmokePayload, OperatorSummary, SignalRefreshPayload, SymbolTrace, TechnicalCalibrationPayload, TechnicalPromotionDecisionResult, TechnicalPromotionReviewResult, TechnicalPromotionReviewsPayload, TraceSummary, WaitSignalsPayload } from '~/types/api'

export function useOperatorApi() {
  const config = useRuntimeConfig()
  const apiBase = String(config.public.apiBase || '').replace(/\/$/, '')

  const get = async <T>(path: string): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`)
  }
  const post = async <T>(path: string, body: Record<string, unknown>): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`, { method: 'POST', body })
  }
  const query = (params: Record<string, unknown>) => {
    const search = new URLSearchParams()
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      search.set(key, String(value))
    }
    const text = search.toString()
    return text ? `?${text}` : ''
  }

  return {
    getHome: () => get<OperatorHome>('/api/home'),
    getSummary: () => get<OperatorSummary>('/api/summary'),
    getHealthDetails: () => get<OperatorHealthDetails>('/api/health/details'),
    runOperationsSmoke: () => get<OperatorSmokePayload>('/api/operations/smoke'),
    getCronLogs: (limit = 20, lines = 80) => get<CronLogsPayload>(`/api/operations/cron-logs?limit=${limit}&lines=${lines}`),
    getOperatorCommands: (limit = 25) => get<OperatorCommandsPayload>(`/api/operations/commands?limit=${limit}`),
    getOperatorApiErrors: (limit = 50) => get<OperatorApiErrorsPayload>(`/api/operations/api-errors?limit=${limit}`),
    runOperatorCommand: (body: Record<string, unknown>) => post<OperatorCommandRunResult>('/api/operations/commands/run', body),
    updateSlowIssueStatus: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/operations/slow-issues/status', body),
    getEventModelPromotionCheck: () => get<EventModelPromotionCheckPayload>('/api/research/event-model-promotion-check'),
    getEventModelArtifacts: () => get<EventModelArtifactsPayload>('/api/research/event-model-artifacts'),
    getManualReview: (limit = 100) => get<ManualReviewPayload>(`/api/manual-review?limit=${limit}`),
    decideManualReview: (body: Record<string, unknown>) => post<ManualReviewDecisionResult>('/api/manual-review/decision', body),
    getActions: (params: Record<string, unknown> = {}) => get<OperatorActions>(`/api/actions${query(params)}`),
    getActionDetail: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/actions/detail${query(params)}`),
    getSignalRefresh: (params: Record<string, unknown> = {}) => get<SignalRefreshPayload>(`/api/signal-refresh${query(params)}`),
    getPortfolio: (params: Record<string, unknown> = {}) => get<OperatorPortfolio>(`/api/portfolio${query(params)}`),
    getPortfolioDetail: (symbol: string, params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/portfolio/${encodeURIComponent(symbol)}/detail${query(params)}`),
    getMarketContext: (limit = 50) => get<OperatorMarketContext>(`/api/market-context?limit=${limit}`),
    getTechnicalCalibration: (limit = 25) => get<TechnicalCalibrationPayload>(`/api/technical-calibration?limit=${limit}`),
    reviewTechnicalCalibration: (body: Record<string, unknown>) => post<TechnicalPromotionReviewResult>('/api/technical-calibration/promotion-review', body),
    getTechnicalPromotionReviews: (limit = 25) => get<TechnicalPromotionReviewsPayload>(`/api/technical-calibration/promotion-reviews?limit=${limit}`),
    decideTechnicalPromotionReview: (body: Record<string, unknown>) => post<TechnicalPromotionDecisionResult>('/api/technical-calibration/promotion-review/decision', body),
    getEvents: (limit = 50, params: Record<string, unknown> = {}) => get<OperatorEvents>(`/api/events${query({ limit, ...params })}`),
    getEventDetail: (uniqueId: string, params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/events/${encodeURIComponent(uniqueId)}/detail${query(params)}`),
    getEventPolicy: (limit = 100, actionType = 'ALL') => get<EventPolicyPayload>(`/api/event-policy?limit=${limit}&action_type=${encodeURIComponent(actionType)}`),
    getEventPolicyEvaluation: (limit = 100) => get<EventPolicyEvaluationPayload>(`/api/event-policy/evaluation?limit=${limit}`),
    getEventTrace: (uniqueId: string) => get<EventTrace>(`/api/events/${encodeURIComponent(uniqueId)}/trace`),
    getEventTraceSummary: (uniqueId: string) => get<TraceSummary>(`/api/events/${encodeURIComponent(uniqueId)}/trace/summary`),
    getSymbolTrace: (symbol: string, limit = 200) => get<SymbolTrace>(`/api/symbols/${encodeURIComponent(symbol)}/trace?limit=${limit}`),
    getSymbolTraceSummary: (symbol: string, limit = 200) => get<TraceSummary>(`/api/symbols/${encodeURIComponent(symbol)}/trace/summary?limit=${limit}`),
    getHypotheses: (limit = 100) => get<HypothesesPayload>(`/api/hypotheses?limit=${limit}`),
    getWaitSignals: (params: Record<string, unknown> = {}) => get<WaitSignalsPayload>(`/api/wait-signals${query(params)}`),
    createHypothesis: (body: Record<string, unknown>) => post<HypothesisCreateResult>('/api/hypotheses', body),
    updateHypothesis: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisCreateResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}`, body),
    previewHypothesis: (body: Record<string, unknown>) => post<HypothesisPreviewResult>('/api/hypotheses/preview', body),
    runPromotionAudit: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisPromotionAuditResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}/promotion-audit`, body),
    runHypotheses: (body: Record<string, unknown>) => post<HypothesisRunResult>('/api/hypotheses/run', body)
  }
}
