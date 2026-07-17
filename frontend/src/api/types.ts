export type Condition = "human" | "tool";
export type Subcondition =
  | "qa"
  | "planning"
  | "chat"
  | "decision"
  | "execution";
export type InputMode = "voice" | "text_test_only";
export type PresentationMode = "conversation" | "execution";
export type ArtifactKind = "schedule_table" | "copy_editor" | null;
export type ArtifactStatus = "none" | "awaiting_input" | "completed" | "failed";
export type ArtifactType =
  | "table"
  | "copy_versions"
  | "decision_matrix"
  | "preference_cards"
  | "plan_card"
  | "weather_card";

export interface LoginRequest {
  name: string;
  phone: string;
}

export interface AdminLoginRequest {
  username: string;
  password: string;
}

export interface PretestSubmissionRequest {
  demographics: Record<string, unknown>;
  scales: Record<string, unknown>;
  slider_touch_state: Record<string, boolean>;
  page_progress: Record<string, unknown>;
  client_timestamp: string;
}

export interface PretestStatusView {
  status: string;
  autosave_count: number;
  has_draft: boolean;
  has_final: boolean;
  last_saved_at?: string | null;
  submitted_at?: string | null;
}

export interface ParticipantDayView {
  day_index: number;
  calendar_date: string;
  status: string;
  can_start_experiment: boolean;
}

export type ParticipationState =
  | "needs_pretest"
  | "ready_for_experiment"
  | "completed"
  | "blocked"
  | "not_scheduled_today";

export interface PretestResponseView {
  day_index: number;
  status: string;
  autosave_count: number;
  payload: Record<string, unknown>;
  last_saved_at?: string | null;
  submitted_at?: string | null;
  can_start_experiment: boolean;
}

export interface ParticipantView {
  participant_id: number;
  attempt_id: number;
  attempt_no: number;
  name: string;
  masked_phone: string;
  participant_type: string;
  target_days: number;
  current_status: string;
  participation_state: ParticipationState;
  participation_message?: string | null;
  current_day: ParticipantDayView;
  pretest_status: PretestStatusView;
}

export type MicrophonePermission =
  | "granted"
  | "denied"
  | "prompt"
  | "unavailable";

export interface ClientInfo {
  device_type: "desktop" | "mobile" | "tablet";
  viewport_width: number;
  is_secure_context: boolean;
  browser_name: string;
  browser_version?: string | null;
  microphone_available: boolean;
  microphone_permission: MicrophonePermission;
}

export interface TestScenarioControls {
  condition: Condition;
  subcondition: Subcondition;
  topic_key: string;
  error_type_id: string;
  planned_error_turn: number;
}

interface SessionStartRequestBase {
  is_test: boolean;
  client_info: ClientInfo;
}

export type SessionStartRequest = SessionStartRequestBase & Partial<TestScenarioControls>;

export interface TurnSubmitRequest {
  session_id: string;
  operation_id?: string;
  turn_index?: number;
  input_mode: InputMode;
  user_text?: string;
  asr_result_id?: string;
}

export interface RatingSubmitRequest {
  stance_score: number;
  trust_score: number;
  client_elapsed_ms?: number | null;
}

export interface ClientTimingSubmitRequest {
  client_message_sent_at: string;
  assistant_render_completed_at: string;
  client_response_latency_ms: number;
  client_timing_interrupted: boolean;
}

export interface ClientTimingView extends ClientTimingSubmitRequest {
  turn_id: number;
  render_timing_received_at: string;
}

export interface TurnRatingView {
  turn_id: number;
  stance_score: number;
  trust_score: number;
  submitted_at: string;
  client_elapsed_ms?: number | null;
}

export interface OptionalArtifactEnvelope {
  artifact_type?: ArtifactType | string | null;
  artifact_payload?: unknown;
  graph_trace?: unknown;
  provider_attempts?: unknown;
  evaluator_result?: unknown;
}

export interface TurnView extends OptionalArtifactEnvelope {
  turn_id: number;
  session_id?: string;
  turn_index: number;
  user_text: string;
  user_input_mode: string;
  assistant_text: string;
  error_planned?: boolean;
  error_presented?: boolean;
  error_presentation?: string;
  session_is_test?: boolean;
  rating?: TurnRatingView | null;
}

export interface AsrView {
  asr_result_id: string;
  asr_status: "success" | "failed" | "timeout";
  asr_text?: string | null;
  retry_count: number;
  max_retry_per_turn: number;
}

export interface RatingView extends TurnRatingView {}

export interface SessionView extends OptionalArtifactEnvelope {
  session_id: string;
  day_index: number;
  status: string;
  condition?: Condition;
  subcondition?: Subcondition;
  topic_key?: string;
  error_type_id?: string;
  planned_error_turn?: number;
  topic_title?: string;
  topic_description?: string;
  started_at: string;
  completed_at?: string | null;
  is_test: boolean;
  client_info?: ClientInfo;
  expected_turn_index?: number | null;
  presentation_mode: PresentationMode;
  artifact_kind: ArtifactKind;
  artifact_status: ArtifactStatus;
  turns: TurnView[];
}

export type RatingSubmitResponse = RatingView | SessionView;

export interface ApiErrorPayload {
  detail?: unknown;
}

export interface AdminLoginView {
  admin_user: string;
  ok: boolean;
}

export interface AdminSessionView {
  authenticated: boolean;
  admin_user: string | null;
}

export interface RecruitmentStatusView {
  status: "open" | "closed";
  accepting_new_participants: boolean;
}

export interface AdminOverviewView {
  total_participants: number;
  completed_sessions: number;
  active_sessions: number;
  today_started: number;
  today_completed: number;
  risk_sessions: number;
  completion_by_type: Record<string, number>;
  assignment_matrix: Array<Record<string, unknown>>;
  api_failures: number;
  asr_failures: number;
}

export interface AdminSystemMetricsView {
  generated_at: string;
  service: {
    status: string;
    label?: string | null;
  };
  database: {
    path: string;
    size_bytes: number;
  };
  data_directory: {
    path: string;
    disk_usage: {
      total_bytes: number;
      used_bytes: number;
      free_bytes: number;
    };
  };
  audio_directory: {
    path: string;
    files: number;
    size_bytes: number;
  };
  exports_directory: {
    path: string;
    files: number;
    size_bytes: number;
  };
  experiment: {
    today_started: number;
    today_completed: number;
    risk_sessions: number;
    api_failures: number;
    asr_failures: number;
  };
  host_metrics: Record<string, unknown>;
}

export interface ProviderUsageRow {
  route?: string;
  provider: string;
  model: string | null;
  calls: number;
  successes: number;
  failures: number;
  timeout_count: number;
  success_rate: number | null;
  avg_latency_ms: number | null;
  p95_latency_ms: number | null;
  cooldown_applied_count: number;
  last_called_at: string | null;
  last_failure_summary: string | null;
  last_failure_code: string | null;
}

export interface DeepSeekConfigurationView {
  status: "configured" | "not_configured";
  provider: "deepseek";
  model: string;
  base_url: string;
  timeout_seconds: number;
}

export interface DeepSeekTestResultView {
  status: string;
  provider: "deepseek";
  model: string;
  latency_ms: number | null;
  error_code?: string | null;
}

export interface ProviderUsageWindow {
  window: "all_time" | "last_24h" | string;
  label: string;
  since: string | null;
  total_calls: number;
  total_successes: number;
  total_failures: number;
  provider_model_rows: ProviderUsageRow[];
  route_rows: ProviderUsageRow[];
}

export interface ProviderModelUsageView {
  generated_at: string;
  deepseek_configuration: DeepSeekConfigurationView;
  windows: ProviderUsageWindow[];
  notes: string[];
}

export interface AdminMonitorSessionRow {
  participant_id: number;
  attempt_id: number | null;
  name: string;
  masked_phone: string;
  phone_hash: string;
  participant_type: string | null;
  condition: string | null;
  subcondition: string | null;
  topic_key: string | null;
  error_type_id: string | null;
  attempt_status: string | null;
  session_id: string;
  day_index: number;
  session_status: string;
  started_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  risk_flags?: string[];
}

export interface AdminDataMetricsView {
  generated_at: string;
  metrics: AdminOverviewView & {
    short_completed: number;
    long_completed: number;
    clean_data_eligible: number;
    clean_data_review_needed: number;
    clean_data_excluded: number;
  };
  incomplete_sessions: AdminMonitorSessionRow[];
  recent_sessions: AdminMonitorSessionRow[];
  risk_sessions: AdminMonitorSessionRow[];
  notes: string[];
}

export interface AdminParticipantRow {
  participant_id: number;
  name: string;
  masked_phone: string;
  phone_hash: string;
  participant_type: string;
  condition: string;
  subcondition: string;
  topic_key: string;
  error_type_id: string;
  current_status: string;
  created_at: string;
}

export interface AdminParticipantsView {
  query: string;
  count: number;
  items: AdminParticipantRow[];
}

export interface AssignmentCellView {
  participant_type: string;
  condition: string;
  subcondition: string;
  error_type_id: string;
  count: number;
  active_assignment_count: number;
  complete_no_external_error_count: number;
  cap: number | null;
  enabled: boolean;
  remaining: number | null;
  updated_at: string | null;
}

export interface AdminAssignmentControlView {
  participant_types: Record<
    string,
    {
      participant_type: string;
      cells: AssignmentCellView[];
    }
  >;
  current_flags: {
    test_channel_enabled: boolean;
  };
  next_assignment_preview: Record<string, Record<string, unknown>>;
  notes: string[];
}

export interface AdminAssignmentControlUpdateRequest {
  operation?: "cell";
  participant_type?: string;
  condition?: string;
  subcondition?: string;
  error_type_id?: string;
  cap?: number | null;
  enabled?: boolean;
}

export interface AdminAssignmentCellIdentifier {
  participant_type: string;
  condition: string;
  subcondition: string;
  error_type_id: string;
}

export interface AdminAssignmentBatchScope {
  cells?: AdminAssignmentCellIdentifier[];
  filter?: {
    participant_type?: string;
    condition?: string;
    subcondition?: string;
    error_type_id?: string;
    enabled?: boolean;
    cap_status?: "capped" | "uncapped" | "reached";
  };
}

export interface AdminAssignmentBatchChanges {
  cap?: number | null;
  enabled?: boolean;
}

export interface AdminAssignmentCellUpdate
  extends AdminAssignmentCellIdentifier,
    AdminAssignmentBatchChanges {}

export interface AdminAssignmentBatchPreviewRequest {
  scope: AdminAssignmentBatchScope;
  changes: AdminAssignmentBatchChanges;
  cell_updates?: AdminAssignmentCellUpdate[];
}

export interface AdminAssignmentBatchPreviewView {
  scope_version: string;
  affected_count: number;
  scope: {
    kind: "explicit_cells" | "filter_snapshot";
    description: string;
    selected_cells: AdminAssignmentCellIdentifier[];
  };
  changes: AdminAssignmentBatchChanges;
  cell_updates?: AdminAssignmentCellUpdate[];
}

export interface AdminAssignmentBatchMutationRequest
  extends AdminAssignmentBatchPreviewRequest {
  scope_version: string;
}

export interface AdminAssignmentBatchMutationView
  extends AdminAssignmentBatchPreviewView {
  result: {
    updated_cells: number;
  };
  assignment_control: AdminAssignmentControlView;
}

export interface CleanDataAuditRow {
  participant_id: number;
  attempt_id: number | null;
  name: string;
  phone_hash: string;
  participant_type: string | null;
  condition: string | null;
  subcondition: string | null;
  topic_key: string | null;
  error_type_id: string | null;
  status: string;
  reasons: string[];
  reviewer_note: string | null;
  reviewed_by: string | null;
  reviewed_at: string | null;
  computed_at: string;
}

export interface CleanDataAuditsView {
  status?: string;
  count: number;
  last_updated_at?: string | null;
  items: CleanDataAuditRow[];
  summary?: {
    scanned: number;
    persisted: number;
    status_counts: Record<string, number>;
  };
}

export interface ApiHealthSummaryView {
  routes: Array<Record<string, unknown>>;
  cooldowns: Array<Record<string, unknown>>;
  failure_reasons: Array<Record<string, unknown>>;
  evaluator_success_rate: Record<string, unknown>;
  asr_success_rate: Record<string, unknown>;
  manual_test_runs: Array<Record<string, unknown>>;
  notes: string[];
}

export interface SystemLogsSummaryView {
  backend_status_counts: Record<string, number>;
  api_log_counts: Array<{
    route: string;
    status: string;
    count: number;
  }>;
  asr_status_counts: Record<string, number>;
  database_size_bytes: number;
  disk_usage: {
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
  };
  audio_directory: {
    path: string;
    files: number;
    size_bytes: number;
  };
  exports_directory: string;
  sanitized_package_path: string;
  notes: string[];
}

export interface ExportJobView {
  job_uuid: string;
  export_type: "experiment_data" | "complete_no_external_error_data" | "reimbursement" | string;
  filters: Record<string, unknown>;
  include_test: boolean;
  status: "queued" | "running" | "succeeded" | "failed" | string;
  progress_message: string | null;
  output_path: string | null;
  created_by: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
}

export interface ExportJobsListView {
  items: ExportJobView[];
}

export interface ExportJobCreateRequest {
  export_type: ExportJobView["export_type"];
  filters?: Record<string, unknown>;
  include_test?: boolean;
}

export interface TableArtifactRow {
  [key: string]: string | number | boolean | null | undefined;
}

export interface TableArtifactPayload {
  columns?: string[];
  rows?: TableArtifactRow[];
  errorInjected?: {
    kind?: string;
    rowIndex?: number;
    fieldName?: string;
    originalValue?: unknown;
    mutatedValue?: unknown;
  };
}

export interface CopyVersionItem {
  id?: string;
  label?: string;
  text?: string;
}

export interface CopyVersionsPayload {
  versions?: CopyVersionItem[];
  selected_version?: {
    version_id?: string;
    reason?: string;
  };
  revision_notes?: string[];
}

export interface DecisionMatrixOption {
  id?: string;
  label?: string;
  attributes?: Record<string, unknown>;
}

export interface DecisionMatrixPayload {
  options?: DecisionMatrixOption[];
  constraints?: Array<Record<string, unknown>>;
  weights?: Array<Record<string, unknown>>;
  recommendation?: Record<string, unknown>;
  reasons?: string[];
}

export interface PreferenceDecisionPayload {
  mood?: string;
  preferences?: string[];
  options?: Array<{
    id?: string;
    title?: string;
    signals?: string[];
  }>;
  ai_preference?: {
    option_id?: string;
    summary?: string;
  };
  friend_like_reason?: string;
}
