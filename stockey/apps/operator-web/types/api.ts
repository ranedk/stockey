export type Dict = Record<string, unknown>

export interface OperatorSummary {
  generated_at?: string
  asof_date?: string
  summary: Dict
  runtime_processes: Dict[]
  cron_status: Dict[]
  sync_state: Dict[]
}

export interface OperatorHome {
  generated_at?: string
  asof_date?: string
  summary: Dict
  runtime_processes: Dict[]
  cron_status: Dict[]
  sync_state: Dict[]
  top_action_recommendations: Dict[]
  today_recommendations: Dict[]
}

export interface OperatorHealthDetails {
  generated_at?: string
  status: string
  sections: Dict
  fix_hints?: Dict[]
}

export interface OperatorActions {
  generated_at?: string
  asof_date?: string
  top_action_recommendations: Dict[]
  action_recommendations: Dict[]
  alerts: Dict[]
}

export interface OperatorPortfolio {
  generated_at?: string
  asof_date?: string
  today_recommendations: Dict[]
  current_recommendations: Dict[]
  exited_recommendations: Dict[]
  portfolio: Dict[]
  lifecycle: Dict[]
}

export interface OperatorEvents {
  generated_at?: string
  asof_date?: string
  events: Dict[]
  operator_feed: Dict[]
  alerts: Dict[]
}

export interface EventPolicyPayload {
  generated_at?: string
  status: string
  asof_date?: string
  summary: Dict
  rows: Dict[]
}

export interface EventPolicyEvaluationPayload {
  generated_at?: string
  status: string
  summary: Dict[]
}

export interface OperatorMarketContext {
  generated_at?: string
  asof_date?: string
  summary: Dict
  top_universe: Dict[]
}

export interface TechnicalCalibrationPayload {
  generated_at?: string
  status: string
  summary: Dict[]
  top_configs: Dict[]
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

export interface EventTrace {
  unique_id: string
  processing: Dict[]
  traces: Dict[]
  steps: Dict[]
}

export interface SymbolTrace {
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
}

export interface TraceSummary {
  symbol?: string
  unique_id?: string
  processing: TraceStage[]
  decisions: TraceDecision[]
  action_conflicts: TraceConflict[]
  raw_counts: Dict
}

export interface HypothesesPayload {
  hypotheses: Dict[]
  matches: Dict[]
  action_plans: Dict[]
  promotion_audits?: Dict[]
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
