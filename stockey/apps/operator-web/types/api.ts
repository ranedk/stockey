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
}

export interface OperatorHealthDetails {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  sections: Dict
  fix_hints?: Dict[]
  current_blockers?: Dict
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

export interface CronLogsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  log_dir: string
  logs: Dict[]
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
  promotion_mode: string
  artifact: Dict
  metadata?: Dict
  coverage: Dict
  weekly_runs: Dict
  score_freshness: Dict
  gates: Dict[]
  failed_gates: string[]
  notes: string[]
}

export interface EventModelArtifactsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  artifact: Dict
  latest_s3_heads: Dict[]
  read_only: boolean
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
  meta?: Dict
}

export interface SignalRefreshPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  signals: Dict[]
  meta?: Dict
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
}

export interface PromptRegistryPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  contracts: Dict[]
  summary: Dict
  notes?: string[]
}

export interface TechnicalPromotionReviewResult {
  status: string
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
}

export interface TechnicalPromotionReviewsPayload {
  generated_at?: string
  api_schema?: OperatorApiSchema
  status: string
  reviews: Dict[]
}

export interface TechnicalPromotionDecisionResult {
  status: string
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
}

export interface SymbolTrace {
  api_schema?: OperatorApiSchema
  symbol: string
  processing: Dict[]
  traces: Dict[]
  steps: Dict[]
  action_conflicts: Dict[]
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
