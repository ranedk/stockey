import type { ActionConflictRulesPayload, ActionConflictRuleWriteResult, ConfigChangeApplicationResult, ConfigChangeApplicationsPayload, ConfigChangePreviewResult, ConfigChangePreviewsPayload, CronLogsPayload, CronStatusPayload, EventDetailPayload, EventModelArtifactsPayload, EventModelPromotionCheckPayload, EventPolicyEvaluationPayload, EventPolicyPayload, EventPolicyPromotionDecisionResult, EventPolicyPromotionReviewResult, EventPolicyPromotionReviewsPayload, EventTrace, ExecutionApprovalContractUpdateResult, ExecutionApprovalDecisionResult, ExecutionApprovalsPayload, ExecutionEvidenceReviewResult, ExecutionLiveAllowanceResult, ExecutionLiveSubmitPreflightResult, ExecutionReconciliationRunResult, FeatureFreshnessPayload, HypothesesPayload, HypothesisCreateResult, HypothesisPreviewResult, HypothesisPromotionAuditResult, HypothesisRunResult, IdentityIssueResolutionPayload, IdentityIssuesPayload, IngestionStatePayload, ManualReviewDecisionResult, ManualReviewPayload, OperatorActions, OperatorApiErrorsPayload, OperatorCommandRunResult, OperatorCommandsPayload, OperatorDetailPayload, OperatorEvents, OperatorHealthDetails, OperatorHome, OperatorJourneyPayload, OperatorMarketContext, OperatorPortfolio, OperatorRuntime, OperatorSmokePayload, OperatorSummary, PromptRegistryPayload, ResearchLedgerPayload, ScreenerCoveragePayload, ScreenerFailuresPayload, ScreenerPreviewPayload, SignalQualityPayload, SignalQualityPromotionDecisionResult, SignalQualityPromotionReviewResult, SignalQualityPromotionReviewsPayload, SignalRefreshPayload, SupersededCleanupPayload, SymbolTrace, TechnicalCalibrationPayload, TechnicalPromotionDecisionResult, TechnicalPromotionReviewResult, TechnicalPromotionReviewsPayload, TraceSummary, TsForecastPromotionCheckPayload, TsForecastPromotionDecisionResult, TsForecastPromotionReviewResult, TsForecastPromotionReviewsPayload, TsForecastReviewRulesPayload, WaitSignalMatchPayload, WaitSignalsPayload } from '~/types/api'

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
    getHome: () => get<OperatorHome>('/api/home'),
    getSummary: () => get<OperatorSummary>('/api/summary'),
    getRuntime: () => get<OperatorRuntime>('/api/runtime'),
    getHealthDetails: (params: Record<string, unknown> = {}) => get<OperatorHealthDetails>(`/api/health/details${query({ compact: true, ...params })}`),
    runOperationsSmoke: () => get<OperatorSmokePayload>('/api/operations/smoke'),
    getCronLogs: (limit = 20, lines = 80, params: Record<string, unknown> = {}) => get<CronLogsPayload>(`/api/operations/cron-logs${query({ limit, lines, ...params })}`),
    getCronStatus: (limit = 50, lines = 12, params: Record<string, unknown> = {}) => get<CronStatusPayload>(`/api/operations/cron-status${query({ limit, lines, ...params })}`),
    getOperatorCommands: (limit = 25) => get<OperatorCommandsPayload>(`/api/operations/commands?limit=${limit}`),
    getOperatorApiErrors: (limit = 50) => get<OperatorApiErrorsPayload>(`/api/operations/api-errors?limit=${limit}`),
    getIngestionState: (params: Record<string, unknown> = {}) => get<IngestionStatePayload>(`/api/operations/ingestion-state${query(params)}`),
    previewSupersededCleanup: (limit = 500) => get<SupersededCleanupPayload>(`/api/operations/superseded-cleanup?limit=${limit}`),
    applySupersededCleanup: (body: Record<string, unknown>) => post<SupersededCleanupPayload>('/api/operations/superseded-cleanup/apply', body),
    runOperatorCommand: (body: Record<string, unknown>) => post<OperatorCommandRunResult>('/api/operations/commands/run', body),
    updateSlowIssueStatus: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/operations/slow-issues/status', body),
    getEventModelPromotionCheck: () => get<EventModelPromotionCheckPayload>('/api/research/event-model-promotion-check'),
    getTsForecastPromotionCheck: (params: Record<string, unknown> = {}) => get<TsForecastPromotionCheckPayload>(`/api/research/ts-forecast-promotion-check${query(params)}`),
    getTsForecastReviewRules: () => get<TsForecastReviewRulesPayload>('/api/research/ts-forecast-review-rules'),
    reviewTsForecastPromotion: (body: Record<string, unknown>) => post<TsForecastPromotionReviewResult>('/api/research/ts-forecast-promotion-review', body),
    getTsForecastPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<TsForecastPromotionReviewsPayload>(`/api/research/ts-forecast-promotion-reviews${query({ limit, ...params })}`),
    decideTsForecastPromotionReview: (body: Record<string, unknown>) => post<TsForecastPromotionDecisionResult>('/api/research/ts-forecast-promotion-review/decision', body),
    getEventModelArtifacts: (params: Record<string, unknown> = {}) => get<EventModelArtifactsPayload>(`/api/research/event-model-artifacts${query(params)}`),
    getResearchLedger: (params: Record<string, unknown> = {}) => get<ResearchLedgerPayload>(`/api/research/ledger${query(params)}`),
    getPromptRegistry: (params: Record<string, unknown> = {}) => get<PromptRegistryPayload>(`/api/research/prompt-registry${query(params)}`),
    previewScreener: (body: Record<string, unknown>) => post<ScreenerPreviewPayload>('/api/screeners/preview', body),
    getScreenerCoverage: (params: Record<string, unknown> = {}) => get<ScreenerCoveragePayload>(`/api/screeners/coverage${query(params)}`),
    getScreenerFailures: (params: Record<string, unknown> = {}) => get<ScreenerFailuresPayload>(`/api/screeners/failures${query(params)}`),
    getManualReview: (limit = 100, params: Record<string, unknown> = {}) => get<ManualReviewPayload>(`/api/manual-review${query({ limit, include_raw: false, ...params })}`),
    getIdentityIssues: (params: Record<string, unknown> = {}) => get<IdentityIssuesPayload>(`/api/identity-issues${query(params)}`),
    previewIdentityIssueResolution: (body: Record<string, unknown> = {}) => post<IdentityIssueResolutionPayload>('/api/identity-issues/resolve-preview', body),
    applyIdentityIssueResolution: (body: Record<string, unknown>) => post<IdentityIssueResolutionPayload>('/api/identity-issues/resolve-apply', body),
    decideManualReview: (body: Record<string, unknown>) => post<ManualReviewDecisionResult>('/api/manual-review/decision', body),
    getActions: (params: Record<string, unknown> = {}) => get<OperatorActions>(`/api/actions${query(params)}`),
    getActionDetail: (params: Record<string, unknown> = {}) => get<OperatorDetailPayload>(`/api/actions/detail${query(params)}`),
    getOperatorPortfolioRecommendations: (params: Record<string, unknown> = {}) => get<Record<string, unknown>>(`/api/operator-portfolio/recommendations${query(params)}`),
    getOperatorPaperPortfolio: () => get<Record<string, unknown>>('/api/operator-portfolio'),
    applyOperatorPortfolioAction: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/operator-portfolio/action', body),
    resetOperatorPaperPortfolio: (body: Record<string, unknown>) => post<Record<string, unknown>>('/api/operator-portfolio/reset', body),
    getActionConflictRules: () => get<ActionConflictRulesPayload>('/api/action-conflict-rules'),
    promoteActionConflictRule: (body: Record<string, unknown>) => post<ActionConflictRuleWriteResult>('/api/action-conflict-rules/promote', body),
    updateActionConflictRule: (ruleId: string, body: Record<string, unknown>) => post<ActionConflictRuleWriteResult>(`/api/action-conflict-rules/${encodeURIComponent(ruleId)}`, body),
    getSignalRefresh: (params: Record<string, unknown> = {}) => get<SignalRefreshPayload>(`/api/signal-refresh${query(params)}`),
    getExecutionApprovals: (params: Record<string, unknown> = {}) => get<ExecutionApprovalsPayload>(`/api/execution/approvals${query(params)}`),
    decideExecutionApproval: (body: Record<string, unknown>) => post<ExecutionApprovalDecisionResult>('/api/execution/approval-decision', body),
    applyExecutionApprovalContract: (body: Record<string, unknown>) => post<ExecutionApprovalContractUpdateResult>('/api/execution/approval-contract-update', body),
    runExecutionReconciliation: (body: Record<string, unknown>) => post<ExecutionReconciliationRunResult>('/api/execution/reconcile', body),
    reviewExecutionEvidence: (body: Record<string, unknown>) => post<ExecutionEvidenceReviewResult>('/api/execution/evidence-review', body),
    applyExecutionLiveAllowance: (body: Record<string, unknown>) => post<ExecutionLiveAllowanceResult>('/api/execution/live-allowance', body),
    previewExecutionLiveSubmit: (body: Record<string, unknown> = {}) => post<ExecutionLiveSubmitPreflightResult>('/api/execution/live-submit-preflight', body),
    getOperatorJourney: (params: Record<string, unknown> = {}) => get<OperatorJourneyPayload>(`/api/operator-journey${query(params)}`),
    getPortfolio: (params: Record<string, unknown> = {}) => get<OperatorPortfolio>(`/api/portfolio${query(params)}`),
    getPortfolioDetail: (symbol: string, params: Record<string, unknown> = {}) => get<OperatorDetailPayload>(`/api/portfolio/${encodeURIComponent(symbol)}/detail${query(params)}`),
    getMarketContext: (limit = 50) => get<OperatorMarketContext>(`/api/market-context?limit=${limit}`),
    getTechnicalCalibration: (limit = 25) => get<TechnicalCalibrationPayload>(`/api/technical-calibration?limit=${limit}`),
    getSignalQuality: (limit = 10) => get<SignalQualityPayload>(`/api/signal-quality?limit=${limit}`),
    reviewSignalQualityOverlay: (body: Record<string, unknown>) => post<SignalQualityPromotionReviewResult>('/api/signal-quality/promotion-review', body),
    getSignalQualityPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<SignalQualityPromotionReviewsPayload>(`/api/signal-quality/promotion-reviews${query({ limit, ...params })}`),
    decideSignalQualityPromotionReview: (body: Record<string, unknown>) => post<SignalQualityPromotionDecisionResult>('/api/signal-quality/promotion-review/decision', body),
    getConfigChangePreviews: (limit = 25, params: Record<string, unknown> = {}) => get<ConfigChangePreviewsPayload>(`/api/config-change/previews${query({ limit, ...params })}`),
    getConfigChangeApplications: (limit = 25, params: Record<string, unknown> = {}) => get<ConfigChangeApplicationsPayload>(`/api/config-change/applications${query({ limit, ...params })}`),
    decideConfigChangeApplication: (body: Record<string, unknown>) => post<ConfigChangeApplicationResult>('/api/config-change/application-decision', body),
    previewTechnicalThresholdConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/technical-threshold-preview', body),
    previewSignalQualityConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/signal-quality-preview', body),
    previewEventPolicyConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/event-policy-preview', body),
    previewTsForecastConfigChange: (body: Record<string, unknown>) => post<ConfigChangePreviewResult>('/api/config-change/ts-forecast-preview', body),
    reviewTechnicalCalibration: (body: Record<string, unknown>) => post<TechnicalPromotionReviewResult>('/api/technical-calibration/promotion-review', body),
    getTechnicalPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<TechnicalPromotionReviewsPayload>(`/api/technical-calibration/promotion-reviews${query({ limit, ...params })}`),
    decideTechnicalPromotionReview: (body: Record<string, unknown>) => post<TechnicalPromotionDecisionResult>('/api/technical-calibration/promotion-review/decision', body),
    getEvents: (limit = 50, params: Record<string, unknown> = {}) => get<OperatorEvents>(`/api/events${query({ limit, ...params })}`),
    getEventDetail: (uniqueId: string, params: Record<string, unknown> = {}) => get<EventDetailPayload>(`/api/events/${encodeURIComponent(uniqueId)}/detail${query(params)}`),
    getEventPolicy: (limit = 100, actionType = 'ALL', params: Record<string, unknown> = {}) => get<EventPolicyPayload>(`/api/event-policy${query({ limit, action_type: actionType, include_raw: false, ...params })}`),
    getEventPolicyEvaluation: (limit = 100) => get<EventPolicyEvaluationPayload>(`/api/event-policy/evaluation?limit=${limit}`),
    reviewEventPolicyGroup: (body: Record<string, unknown>) => post<EventPolicyPromotionReviewResult>('/api/event-policy/promotion-review', body),
    getEventPolicyPromotionReviews: (limit = 25, params: Record<string, unknown> = {}) => get<EventPolicyPromotionReviewsPayload>(`/api/event-policy/promotion-reviews${query({ limit, ...params })}`),
    decideEventPolicyPromotionReview: (body: Record<string, unknown>) => post<EventPolicyPromotionDecisionResult>('/api/event-policy/promotion-review/decision', body),
    getEventTrace: (uniqueId: string) => get<EventTrace>(`/api/events/${encodeURIComponent(uniqueId)}/trace`),
    getEventTraceSummary: (uniqueId: string) => get<TraceSummary>(`/api/events/${encodeURIComponent(uniqueId)}/trace/summary`),
    getSymbolTrace: (symbol: string, limit = 200) => get<SymbolTrace>(`/api/symbols/${encodeURIComponent(symbol)}/trace?limit=${limit}`),
    getSymbolTraceSummary: (symbol: string, limit = 200) => get<TraceSummary>(`/api/symbols/${encodeURIComponent(symbol)}/trace/summary?limit=${limit}`),
    getFeatureFreshness: (symbol: string, params: Record<string, unknown> = {}) => get<FeatureFreshnessPayload>(`/api/symbols/${encodeURIComponent(symbol)}/feature-freshness${query(params)}`),
    getHypotheses: (limit = 100, params: Record<string, unknown> = {}) => get<HypothesesPayload>(`/api/hypotheses${query({ limit, ...params })}`),
    getWaitSignals: (params: Record<string, unknown> = {}) => get<WaitSignalsPayload>(`/api/wait-signals${query(params)}`),
    runWaitSignalMatch: (body: Record<string, unknown> = {}) => post<WaitSignalMatchPayload>('/api/wait-signals/match', body),
    createHypothesis: (body: Record<string, unknown>) => post<HypothesisCreateResult>('/api/hypotheses', body),
    updateHypothesis: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisCreateResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}`, body),
    previewHypothesis: (body: Record<string, unknown>) => post<HypothesisPreviewResult>('/api/hypotheses/preview', body),
    runPromotionAudit: (hypothesisId: string, body: Record<string, unknown>) => post<HypothesisPromotionAuditResult>(`/api/hypotheses/${encodeURIComponent(hypothesisId)}/promotion-audit`, body),
    runHypotheses: (body: Record<string, unknown>) => post<HypothesisRunResult>('/api/hypotheses/run', body)
  }
}
