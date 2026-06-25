import type { ActionConflictRulesPayload, ActionConflictRuleWriteResult, ConfigChangeApplicationResult, ConfigChangeApplicationsPayload, ConfigChangePreviewResult, CronLogsPayload, CronStatusPayload, EventModelArtifactsPayload, EventModelPromotionCheckPayload, FeatureFreshnessPayload, HypothesesPayload, HypothesisCreateResult, HypothesisPreviewResult, HypothesisPromotionAuditResult, HypothesisRunResult, IdentityIssueResolutionPayload, IdentityIssuesPayload, OperatorActions, OperatorApiErrorsPayload, OperatorCommandRunResult, OperatorCommandsPayload, OperatorDetailPayload, OperatorEvents, OperatorHealthDetails, OperatorPortfolio, OperatorRuntime, OperatorSmokePayload, PromptRegistryPayload, RegimeOverlayDecisionResult, RegimeOverlaysPayload, ResearchLedgerPayload, ScreenerCoveragePayload, ScreenerFailuresPayload, ScreenerPreviewPayload, SignalQualityPayload, SignalQualityPromotionDecisionResult, SignalQualityPromotionReviewResult, SignalQualityPromotionReviewsPayload, TechnicalCalibrationPayload, TechnicalPromotionDecisionResult, TechnicalPromotionReviewResult, TechnicalPromotionReviewsPayload, TraceSummary, WaitSignalsPayload } from '~/types/api'

export function useOperatorApi() {
  const config = useRuntimeConfig()
  const apiBase = String(config.public.apiBase || '').replace(/\/$/, '')
  const apiTimeoutMs = Number(config.public.apiTimeoutMs || 12000)

  const get = async <T>(path: string): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`, { timeout: apiTimeoutMs })
  }
  const post = async <T>(path: string, body: Record<string, unknown>): Promise<T> => {
    return await $fetch<T>(`${apiBase}${path}`, { method: 'POST', body, timeout: apiTimeoutMs })
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
    getRuntime: () => get<OperatorRuntime>('/api/runtime'),
    getHealthDetails: (params: Record<string, unknown> = {}) => get<OperatorHealthDetails>(`/api/health/details${query({ compact: true, ...params })}`),
    runOperationsSmoke: () => get<OperatorSmokePayload>('/api/operations/smoke'),
    getCronLogs: (limit = 20, lines = 80, params: Record<string, unknown> = {}) => get<CronLogsPayload>(`/api/operations/cron-logs${query({ limit, lines, ...params })}`),
    getCronStatus: (limit = 50, lines = 12, params: Record<string, unknown> = {}) => get<CronStatusPayload>(`/api/operations/cron-status${query({ limit, lines, ...params })}`),
    getOperatorCommands: (limit = 25) => get<OperatorCommandsPayload>(`/api/operations/commands?limit=${limit}`),
    getOperatorApiErrors: (limit = 50) => get<OperatorApiErrorsPayload>(`/api/operations/api-errors?limit=${limit}`),
    runOperatorCommand: (body: Record<string, unknown>) => post<OperatorCommandRunResult>('/api/operations/commands/run', body),
    getEventModelPromotionCheck: () => get<EventModelPromotionCheckPayload>('/api/research/event-model-promotion-check'),
    getEventModelArtifacts: (params: Record<string, unknown> = {}) => get<EventModelArtifactsPayload>(`/api/research/event-model-artifacts${query(params)}`),
    getResearchLedger: (params: Record<string, unknown> = {}) => get<ResearchLedgerPayload>(`/api/research/ledger${query(params)}`),
    getPromptRegistry: (params: Record<string, unknown> = {}) => get<PromptRegistryPayload>(`/api/research/prompt-registry${query(params)}`),
    previewScreener: (body: Record<string, unknown>) => post<ScreenerPreviewPayload>('/api/screeners/preview', body),
    getScreenerCoverage: (params: Record<string, unknown> = {}) => get<ScreenerCoveragePayload>(`/api/screeners/coverage${query(params)}`),
    getScreenerFailures: (params: Record<string, unknown> = {}) => get<ScreenerFailuresPayload>(`/api/screeners/failures${query(params)}`),
    getIdentityIssues: (params: Record<string, unknown> = {}) => get<IdentityIssuesPayload>(`/api/identity-issues${query(params)}`),
    getLlmDecisions: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/llm-decisions${query(params)}`),
    getRecommendationsUnified: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/recommendations-unified${query(params)}`),
    getPositions: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/positions${query(params)}`),
    takePosition: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/positions/take', body),
    exitPosition: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/positions/exit', body),
    getSymbolWhy: (symbol: string, params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/symbol/${encodeURIComponent(symbol)}/why${query(params)}`),
    getWorkbench: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/workbench${query(params)}`),
    getHealthHub: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/health-hub${query(params)}`),
    previewIdentityIssueResolution: (body: Record<string, unknown> = {}) => post<IdentityIssueResolutionPayload>('/api/identity-issues/resolve-preview', body),
    applyIdentityIssueResolution: (body: Record<string, unknown>) => post<IdentityIssueResolutionPayload>('/api/identity-issues/resolve-apply', body),
    getActions: (params: Record<string, unknown> = {}) => get<OperatorActions>(`/api/actions${query(params)}`),
    getActionConflictRules: () => get<ActionConflictRulesPayload>('/api/action-conflict-rules'),
    promoteActionConflictRule: (body: Record<string, unknown>) => post<ActionConflictRuleWriteResult>('/api/action-conflict-rules/promote', body),
    updateActionConflictRule: (ruleId: string, body: Record<string, unknown>) => post<ActionConflictRuleWriteResult>(`/api/action-conflict-rules/${encodeURIComponent(ruleId)}`, body),
    getPortfolio: (params: Record<string, unknown> = {}) => get<OperatorPortfolio>(`/api/portfolio${query(params)}`),
    getPortfolioDetail: (symbol: string, params: Record<string, unknown> = {}) => get<OperatorDetailPayload>(`/api/portfolio/${encodeURIComponent(symbol)}/detail${query(params)}`),
    getRegimeOverlays: (limit = 25, params: Record<string, unknown> = {}) => get<RegimeOverlaysPayload>(`/api/regime-overlays${query({ limit, ...params })}`),
    decideRegimeOverlay: (body: Record<string, unknown>) => post<RegimeOverlayDecisionResult>('/api/regime-overlays/decision', body),
    getTechnicalCalibration: (limit = 25) => get<TechnicalCalibrationPayload>(`/api/technical-calibration?limit=${limit}`),
    getSignalQuality: (limit = 10) => get<SignalQualityPayload>(`/api/signal-quality?limit=${limit}`),
    reviewSignalQualityOverlay: (body: Record<string, unknown>) => post<SignalQualityPromotionReviewResult>('/api/signal-quality/promotion-review', body),
    getSignalQualityPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<SignalQualityPromotionReviewsPayload>(`/api/signal-quality/promotion-reviews${query({ limit, ...params })}`),
    decideSignalQualityPromotionReview: (body: Record<string, unknown>) => post<SignalQualityPromotionDecisionResult>('/api/signal-quality/promotion-review/decision', body),
    getConfigChangeApplications: (limit = 25, params: Record<string, unknown> = {}) => get<ConfigChangeApplicationsPayload>(`/api/config-change/applications${query({ limit, ...params })}`),
    decideConfigChangeApplication: (body: Record<string, unknown>) => post<ConfigChangeApplicationResult>('/api/config-change/application-decision', body),
    previewTechnicalThresholdConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/technical-threshold-preview', body),
    previewSignalQualityConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/signal-quality-preview', body),
    reviewTechnicalCalibration: (body: Record<string, unknown>) => post<TechnicalPromotionReviewResult>('/api/technical-calibration/promotion-review', body),
    getTechnicalPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<TechnicalPromotionReviewsPayload>(`/api/technical-calibration/promotion-reviews${query({ limit, ...params })}`),
    decideTechnicalPromotionReview: (body: Record<string, unknown>) => post<TechnicalPromotionDecisionResult>('/api/technical-calibration/promotion-review/decision', body),
    getEvents: (limit = 50, params: Record<string, unknown> = {}) => get<OperatorEvents>(`/api/events${query({ limit, ...params })}`),
    getSymbolTraceSummary: (symbol: string, limit = 200) => get<TraceSummary>(`/api/symbols/${encodeURIComponent(symbol)}/trace/summary?limit=${limit}`),
    getFeatureFreshness: (symbol: string, params: Record<string, unknown> = {}) => get<FeatureFreshnessPayload>(`/api/symbols/${encodeURIComponent(symbol)}/feature-freshness${query(params)}`),
    getHypotheses: (limit = 100, params: Record<string, unknown> = {}) => get<HypothesesPayload>(`/api/hypotheses${query({ limit, ...params })}`),
    getWaitSignals: (params: Record<string, unknown> = {}) => get<WaitSignalsPayload>(`/api/wait-signals${query(params)}`),
    createHypothesis: (body: Record<string, unknown>) => post<HypothesisCreateResult>('/api/hypotheses', body),
    updateHypothesis: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisCreateResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}`, body),
    previewHypothesis: (body: Record<string, unknown>) => post<HypothesisPreviewResult>('/api/hypotheses/preview', body),
    runPromotionAudit: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisPromotionAuditResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}/promotion-audit`, body),
    runHypotheses: (body: Record<string, unknown>) => post<HypothesisRunResult>('/api/hypotheses/run', body)
  }
}
