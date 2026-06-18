export type Dict = Record<string, unknown>

export interface OperatorApiSchema {
  name: string
  version: string
  endpoint: string
  generated_at?: string
  read_only: boolean
  broker_execution_enabled: boolean
}

export interface OperatorSummary {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  snapshot?: Dict
  snapshot_warning?: Dict | null
  summary: Dict
  runtime_processes: Dict[]
  cron_status: Dict[]
  sync_state: Dict[]
}

export interface OperatorHome {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  snapshot?: Dict
  snapshot_warning?: Dict | null
  summary: Dict
  runtime_processes: Dict[]
  cron_status: Dict[]
  sync_state: Dict[]
  top_action_recommendations: Dict[]
  today_recommendations: Dict[]
  ts_forecast_paper_summary?: Dict[]
}

export interface OperatorHealthDetails {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  sections: Dict
  deferred_diagnostics?: Dict
  fix_hints?: Dict[]
  current_blockers?: Dict
  compact?: boolean
  compact_meta?: Dict
}

export interface OperatorRuntime {
  api_schema?: OperatorApiSchema
  generated_at?: string
  status: string
  service: string
  process_started_at?: string
  uptime_seconds?: number
  git_rev?: string
  git_branch?: string
  git_dirty?: boolean
  latest_source_mtime?: string
  latest_source_path?: string
  stale_code?: boolean
  stale_reason?: string
  operator_action?: string
  live_trading_enabled?: boolean
  live_trading_disabled?: boolean
  live_trading_env_var?: string
  live_trading_operator_note?: string
  read_only: boolean
}

export interface FeatureFreshnessPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  symbol?: string
  asof_date?: string
  counts: Dict
  blockers: Dict[]
  inputs: Dict[]
  stage_gates?: Dict[]
  stage_gate_summary?: Dict
  notes?: string[]
}

export interface OperatorSmokePayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  operator_health?: Dict
  operator_smoke?: Dict
  fix_hints?: Dict[]
  trust_level?: string
  trust_status?: string
  recommendation?: string
  next_commands?: string[]
  read_only: boolean
  note?: string
}

export interface ExecutionApprovalsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict
  rows: Dict[]
  operator_boundary: Dict
  source_warnings?: Dict[]
  skipped?: Dict[]
}

export interface ExecutionApprovalDecisionResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  decision: Dict
  operator_boundary: Dict
  note?: string
}

export interface ExecutionApprovalContractUpdateResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  updated_row: Dict
  safety_contract: Dict
  operator_boundary: Dict
  note?: string
}

export interface ExecutionReconciliationRunResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  mode: string
  dry_run: boolean
  summary: Dict
  orders: Dict[]
  fills: Dict[]
  targets: Dict[]
  operator_boundary: Dict
  note?: string
}

export interface ExecutionEvidenceReviewResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  mode: string
  dry_run: boolean
  decision: string
  review: Dict
  evidence: Dict
  updated_row: Dict
  safety_contract: Dict
  operator_boundary: Dict
  note?: string
}

export interface ExecutionLiveAllowanceResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  mode: string
  dry_run: boolean
  decision: string
  allowance: Dict
  blockers: string[]
  updated_row: Dict
  safety_contract: Dict
  operator_boundary: Dict
  note?: string
}

export interface ExecutionLiveSubmitPreflightResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  decision: string
  summary: Dict
  expected_live_token?: string | null
  cli_command_preview?: string | null
  env_required: Dict
  blockers: string[]
  planned_orders: Dict[]
  skipped_orders: Dict[]
  operator_boundary: Dict
  note?: string
}

export interface CronLogsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  log_dir: string
  logs: Dict[]
  pagination?: Dict
}

export interface CronStatusPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  crontab_path?: string
  log_dir?: string
  counts: Dict
  jobs: Dict[]
  pagination?: Dict
}

export interface OperatorCommandsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  commands: Dict[]
  recent_runs: Dict[]
}

export interface OperatorApiErrorsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict
  errors: Dict[]
}

export interface IngestionStatePayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  filters: Dict
  summary: Dict
  operator_boundary: Dict
}

export interface SupersededCleanupPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  mode: string
  dry_run: boolean
  result: Dict
  counts: Dict
  operator_boundary: Dict
  audit_run?: Dict | null
  note?: string
}

export interface IdentityIssueResolutionPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  mode: string
  checked_rows: number
  counts: Dict
  results: Dict[]
  requested_issue_keys?: string[]
  operator_boundary?: Dict
  note?: string
}

export interface OperatorCommandRunResult {
  generated_at?: string
  status: string
  run: Dict
  note?: string
}

export interface EventModelPromotionCheckPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  decision: string
  ready_for_operator_review: boolean
  scorecard?: Dict
  promotion_mode: string
  artifact: Dict
  metadata?: Dict
  coverage: Dict
  weekly_runs: Dict
  score_freshness: Dict
  research_safety?: Dict
  gates: Dict[]
  failed_gates: string[]
  notes: string[]
}

export interface TsForecastPromotionCheckPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  decision?: string
  ready_for_operator_review?: boolean
  promotion_mode?: string
  scorecard: Dict
  evidence: Dict
  notes?: string[]
}

export interface TsForecastReviewRulesPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  config_path?: string
  rules: Dict[]
  issues: Dict[]
  summary: Dict
  operator_boundary?: Dict
}

export interface TsForecastPromotionReviewResult {
  status: string
  api_schema?: OperatorApiSchema
  reviewed_at?: string
  model_name: string
  horizon_days: number
  evidence_from_date?: string
  evidence_to_date?: string
  promotion_check: Dict
  group_evidence: Dict
  pending_patch: Dict
  llm_review: Dict
  review_model?: string
  review_status?: string
  review_error?: string
  applied?: boolean
}

export interface TsForecastPromotionReviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  reviews: Dict[]
  pagination?: Dict
}

export interface TsForecastPromotionDecisionResult {
  status: string
  api_schema?: OperatorApiSchema
  decided_at?: string
  reviewed_at?: string
  model_name: string
  horizon_days: number
  decision: string
  operator_id?: string
  decision_reason?: string
  final_patch: Dict
  review?: Dict
  applied: boolean
  note?: string
}

export interface EventModelArtifactsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  artifact: Dict
  latest_s3_heads: Dict[]
  pagination?: Dict
  operator_boundary?: Dict
  read_only: boolean
}

export interface ResearchLedgerPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  runs: Dict[]
  summary: Dict
  pagination?: Dict
  operator_boundary?: Dict
}

export interface ManualReviewPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict
  source_warnings?: Dict[]
  items: Dict[]
}

export interface IdentityIssuesPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict
  source_warnings?: Dict[]
  issues: Dict[]
  skipped?: Dict[]
}

export interface ManualReviewDecisionResult {
  api_schema?: OperatorApiSchema
  status: string
  decided_at?: string
  item_id: string
  decision: string
  closing_decision: boolean
  next_state?: string
  creates_wait_signal?: boolean
  wait_signal?: Dict | null
  manual_review_state?: Dict | null
  decision_effect?: Dict | null
  note?: string
}

export interface ActionConflictRulesPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  rules: Dict[]
  unresolved_conflicts: Dict[]
  row_count: number
  unresolved_count?: number
}

export interface ActionConflictRuleWriteResult {
  status: string
  generated_at?: string
  api_schema?: OperatorApiSchema
  rule: Dict
  condition?: Dict | null
  note?: string
}

export interface OperatorActions {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  snapshot?: Dict
  snapshot_warning?: Dict | null
  top_action_recommendations: Dict[]
  action_recommendations: Dict[]
  alerts: Dict[]
  pagination?: Dict
  meta?: Dict
}

export interface SignalRefreshPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  signals: Dict[]
  meta?: Dict
}

export interface OperatorJourneyPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  filters: Dict
  summary: Dict
  stages: Record<string, Dict[]>
  timeline: Dict[]
  skipped_sources: Dict[]
}

export interface OperatorPortfolio {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  snapshot?: Dict
  snapshot_warning?: Dict | null
  today_recommendations: Dict[]
  current_recommendations: Dict[]
  exited_recommendations: Dict[]
  portfolio: Dict[]
  lifecycle: Dict[]
  pagination?: Dict
  meta?: Dict
}

export interface OperatorDetailPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  kind: string
  filters: Dict
  rows: Dict[]
  row_count: number
  policy_changes?: Dict[]
  policy_change_count?: number
}

export interface OperatorEvents {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  snapshot?: Dict
  snapshot_warning?: Dict | null
  events: Dict[]
  operator_feed: Dict[]
  alerts: Dict[]
  pagination?: Dict
  meta?: Dict
}

export interface EventPolicyPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  asof_date?: string
  summary: Dict
  rows: Dict[]
}

export interface EventPolicyEvaluationPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict[]
}

export interface OperatorMarketContext {
  generated_at?: string
  api_schema?: OperatorApiSchema
  asof_date?: string
  summary: Dict
  top_universe: Dict[]
}

export interface RegimeOverlaysPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  proposals: Dict[]
  decisions: Dict[]
  summary: Dict
  pagination?: Dict
  operator_boundary?: Dict
}

export interface RegimeOverlayDecisionResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  decided_at?: string
  decision?: string
  proposal_id?: string
  proposal_status?: string
  decision_effect: Dict
  proposal: Dict
  operator_boundary: Dict
  note?: string
}

export interface TechnicalCalibrationPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary: Dict[]
  top_configs: Dict[]
}

export interface SignalQualityPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  latest_evaluated_at?: string | null
  summary: Dict[]
  examples: Dict[]
  coverage: Dict[]
  meta?: Dict
}

export interface SignalQualityPromotionReviewResult {
  status: string
  api_schema?: OperatorApiSchema
  reviewed_at?: string
  evaluated_at?: string
  horizon_days?: number
  variant: string
  signal_quality_evidence: Dict
  coverage?: Dict
  pending_patch: Dict
  llm_review: Dict
  review_model?: string
  review_status?: string
  review_error?: string
}

export interface SignalQualityPromotionReviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  reviews: Dict[]
  pagination?: Dict
}

export interface SignalQualityPromotionDecisionResult {
  status: string
  api_schema?: OperatorApiSchema
  decided_at?: string
  reviewed_at?: string
  evaluated_at?: string
  horizon_days?: number
  variant: string
  decision: string
  operator_id?: string
  decision_reason?: string
  final_patch: Dict
  review?: Dict
  applied: boolean
  note?: string
}

export interface EventPolicyPromotionReviewResult {
  status: string
  api_schema?: OperatorApiSchema
  reviewed_at?: string
  evaluated_at?: string
  horizon_days?: number
  group_type: string
  group_value: string
  event_policy_evidence: Dict
  pending_patch: Dict
  llm_review: Dict
  review_model?: string
  review_status?: string
  review_error?: string
}

export interface EventPolicyPromotionReviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  reviews: Dict[]
  pagination?: Dict
}

export interface EventPolicyPromotionDecisionResult {
  status: string
  api_schema?: OperatorApiSchema
  decided_at?: string
  reviewed_at?: string
  evaluated_at?: string
  horizon_days?: number
  group_type: string
  group_value: string
  decision: string
  operator_id?: string
  decision_reason?: string
  final_patch: Dict
  review?: Dict
  applied: boolean
  note?: string
}

export interface ConfigChangePreviewResult {
  status: string
  api_schema?: OperatorApiSchema
  preview_id?: string
  generated_at?: string
  source_type: string
  source_key?: string
  config_path: string
  review_status?: string
  decision_status?: string
  patch_payload?: Dict
  unified_diff: string
  rollback_note?: string
  safety_checks?: string[]
  applied: boolean
  decision?: Dict
}

export interface ConfigChangePreviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  previews: Dict[]
  pagination?: Dict
}

export interface ConfigChangeApplicationResult {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  application_id?: string
  preview_id?: string
  application_decision?: string
  verification_status?: string
  verification?: Dict
  safety_checks?: string[]
  decision_effect?: Dict
  operator_boundary?: Dict
  applied_by_system: boolean
  applied: boolean
  note?: string
}

export interface ConfigChangeApplicationsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  applications: Dict[]
  pagination?: Dict
}

export interface PromptRegistryPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  contracts: Dict[]
  summary: Dict
  notes?: string[]
  pagination?: Dict
}

export interface ScreenerPreviewPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  query_name?: string
  query_slug?: string
  query_hash?: string
  screener_url?: string
  validation_issues: Dict[]
  row_count: number
  rows: Dict[]
  headers?: unknown[]
  meta?: Dict
  operator_boundary?: Dict
}

export interface ScreenerCoveragePayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  asof_date?: string
  lookback_days?: number
  window?: Dict
  summary: Dict
  screeners: Dict[]
  notes?: string[]
}

export interface ScreenerFailuresPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  message?: string
  window_hours?: number
  active_count?: number
  validation_count?: number
  fetch_count?: number
  parse_count?: number
  counts_by_stage?: Dict
  rows: Dict[]
  operator_boundary?: Dict
}

export interface TechnicalPromotionReviewResult {
  status: string
  api_schema?: OperatorApiSchema
  reviewed_at?: string
  setup_id: string
  config_id: string
  horizon_days?: number
  current_thresholds: Dict
  candidate_thresholds: Dict
  calibration_evidence: Dict
  horizon_summary?: Dict
  pending_patch: Dict
  llm_review: Dict
  review_model?: string
  review_status?: string
  review_error?: string
  applied?: boolean
}

export interface TechnicalPromotionReviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  reviews: Dict[]
  pagination?: Dict
}

export interface TechnicalPromotionDecisionResult {
  status: string
  api_schema?: OperatorApiSchema
  decided_at?: string
  reviewed_at?: string
  setup_id: string
  config_id: string
  decision: string
  operator_id?: string
  decision_reason?: string
  final_patch: Dict
  review?: Dict
  applied: boolean
  note?: string
}

export interface EventDetailPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  kind: string
  filters: Dict
  rows: Dict[]
  row_count: number
}

export interface EventTrace {
  api_schema?: OperatorApiSchema
  unique_id: string
  processing: Dict[]
  traces: Dict[]
  steps: Dict[]
  action_conflicts?: Dict[]
  pagination?: Dict
}

export interface SymbolTrace {
  api_schema?: OperatorApiSchema
  symbol: string
  processing: Dict[]
  traces: Dict[]
  steps: Dict[]
  action_conflicts: Dict[]
  pagination?: Dict
}

export interface TraceStage {
  step_idx?: number
  domain?: string
  stage: string
  status: string
  symbol?: string
  source_type?: string
  reason?: string
  error?: string
  started_at?: string
  completed_at?: string
  input_hash?: string
  output_hash?: string
  payload?: Dict
}

export interface TraceDecision {
  domain?: string
  trace_id: string
  asof_date?: string
  updated_at?: string
  symbol?: string
  unique_id?: string
  setup_id?: string
  trigger_type: string
  previous_action?: string
  new_action?: string
  action_changed?: boolean
  final_action?: string
  final_reason?: string
  source_table?: string
  source_key?: string
  payload?: Dict
  steps: TraceStage[]
}

export interface TraceConflict {
  asof_date?: string
  symbol?: string
  winning_action_code?: string
  losing_action_code?: string
  winning_source?: string
  losing_source?: string
  losing_setup_id?: string
  losing_unique_id?: string
  lost_reason?: string
  resolution_status?: string
  resolution_rule_id?: string
  resolution_action?: string
  resolution_reason?: string
  requires_manual_resolution?: boolean
}

export interface ManualReviewWaitSignalLink {
  decided_at?: string
  manual_review_item_id?: string
  manual_review_item_type?: string
  manual_review_source_table?: string
  manual_review_source_key?: string
  symbol?: string
  unique_id?: string
  setup_id?: string
  decision?: string
  rationale?: string
  follow_up_event?: string
  operator_id?: string
  wait_signal_created_at?: string
  signal_id?: string
  wait_signal_status?: string
  signal_type?: string
  expected_action?: string
  operator_summary?: string
  wait_question?: string
  condition?: Dict
  valid_until?: string
  generated_by?: string
  matched_at?: string
  match_status?: string
  match_score?: unknown
  match_source_table?: string
  match_source_key?: string
  observed_at?: string
  match_reason?: string
  evidence?: Dict
}

export interface TraceSummary {
  api_schema?: OperatorApiSchema
  symbol?: string
  unique_id?: string
  processing: TraceStage[]
  decisions: TraceDecision[]
  action_conflicts: TraceConflict[]
  manual_review_wait_signal_links?: ManualReviewWaitSignalLink[]
  raw_counts: Dict
  pagination?: Dict
  _trace_summary_cache?: Dict
}

export interface HypothesesPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status?: string
  hypotheses: Dict[]
  matches: Dict[]
  action_plans: Dict[]
  wait_signals?: Dict[]
  wait_signal_matches?: Dict[]
  promotion_audits?: Dict[]
  pagination?: Dict
}

export interface WaitSignalsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  summary?: Dict
  source_warnings?: Dict[]
  sections?: Record<string, Dict[]>
  signals: Dict[]
  matches: Dict[]
  match_result?: Dict | null
}

export interface WaitSignalMatchPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  match_result: Dict
}

export interface HypothesisCreateResult {
  status: string
  hypothesis: Dict
}

export interface HypothesisPreviewResult {
  status: string
  normalized_payload: Dict
  db_row_preview: Dict
  production_note: string
}

export interface HypothesisRunResult {
  status: string
  hypothesis_count: number
  source_event_count: number
  match_count: number
  action_plan_count?: number
  matches: Dict[]
  action_plans?: Dict[]
}

export interface HypothesisPromotionAuditResult {
  status: string
  audit: Dict
}
