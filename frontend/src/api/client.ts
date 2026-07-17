import type {
  ClientTimingSubmitRequest,
  ClientTimingView,
  AdminAssignmentBatchMutationRequest,
  AdminAssignmentBatchMutationView,
  AdminAssignmentBatchPreviewRequest,
  AdminAssignmentBatchPreviewView,
  AdminAssignmentControlUpdateRequest,
  AdminAssignmentControlView,
  AdminDataMetricsView,
  AdminParticipantsView,
  AdminLoginRequest,
  AdminLoginView,
  AdminOverviewView,
  AdminSessionView,
  AdminSystemMetricsView,
  ApiErrorPayload,
  ApiHealthSummaryView,
  AsrView,
  CleanDataAuditsView,
  DeepSeekTestResultView,
  ExportJobCreateRequest,
  ExportJobView,
  ExportJobsListView,
  LoginRequest,
  ParticipantView,
  PretestResponseView,
  PretestSubmissionRequest,
  ProviderModelUsageView,
  RatingSubmitRequest,
  RatingSubmitResponse,
  RecruitmentStatusView,
  SessionStartRequest,
  SessionView,
  SystemLogsSummaryView,
  TurnSubmitRequest,
  TurnView,
} from "./types";

export class ApiError extends Error {
  status: number;

  detail: string;

  code?: string;

  retryable?: boolean;

  retryAfterMs?: number;

  fieldErrors?: Record<string, string>;

  constructor(status: number, detail: unknown, fallback = `Request failed: ${status}`) {
    const formatted = formatApiErrorDetail(detail, fallback);
    super(formatted.message);
    this.name = "ApiError";
    this.status = status;
    this.detail = formatted.message;
    this.code = formatted.code;
    this.retryable = formatted.retryable;
    this.retryAfterMs = formatted.retryAfterMs;
    this.fieldErrors = formatted.fieldErrors;
  }
}

export class OperationRefreshError extends Error {
  originalError: unknown;

  constructor(error: unknown) {
    super(error instanceof Error ? error.message : "会话状态刷新失败。");
    this.name = "OperationRefreshError";
    this.originalError = error;
  }
}

export function requiresNewOperationId(error: unknown): boolean {
  return (
    !(error instanceof OperationRefreshError) &&
    error instanceof ApiError &&
    (error.status >= 500 ||
      (error.status === 409 && error.code !== "external_operation_pending"))
  );
}

const structuredErrorMessages: Record<string, string> = {
  external_operation_pending: "请求仍在处理中，请稍后重试。",
  idempotency_key_reused: "提交内容已发生变化，请重新开始本次提交。",
  external_operation_failed: "上次请求未完成，请重新提交。",
  recruitment_closed: "正式实验招募暂未开放，请稍后再试。",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function formatApiErrorDetail(
  detail: unknown,
  fallback: string,
): {
  message: string;
  code?: string;
  retryable?: boolean;
  retryAfterMs?: number;
  fieldErrors?: Record<string, string>;
} {
  if (typeof detail === "string" && detail.trim()) {
    return { message: detail };
  }

  if (Array.isArray(detail)) {
    const validationEntries = detail.flatMap((item) => {
      if (!isRecord(item) || typeof item.msg !== "string") return [];
      const location = Array.isArray(item.loc) ? item.loc : [];
      const field = [...location].reverse().find((part) => typeof part === "string");
      return [{ field: typeof field === "string" ? field : "form", message: item.msg }];
    });
    return {
      message: validationEntries.length
        ? validationEntries.map((entry) => entry.message).join("；")
        : fallback,
      fieldErrors: Object.fromEntries(
        validationEntries.map((entry) => [entry.field, entry.message]),
      ),
    };
  }

  if (!isRecord(detail)) {
    return { message: fallback };
  }

  const code = typeof detail.code === "string" ? detail.code : undefined;
  const explicitMessage = typeof detail.message === "string" ? detail.message : undefined;
  const fieldErrors = isRecord(detail.field_errors)
    ? Object.fromEntries(
        Object.entries(detail.field_errors).filter(
          (entry): entry is [string, string] => typeof entry[1] === "string",
        ),
      )
    : undefined;
  return {
    message: explicitMessage ?? (code ? structuredErrorMessages[code] ?? code : fallback),
    code,
    retryable: typeof detail.retryable === "boolean" ? detail.retryable : undefined,
    retryAfterMs:
      typeof detail.retry_after_ms === "number" ? detail.retry_after_ms : undefined,
    fieldErrors,
  };
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const payload = (await safeParseJson<ApiErrorPayload>(response)) ?? {};
    throw new ApiError(response.status, payload.detail);
  }

  return response.json() as Promise<T>;
}

async function safeParseJson<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) {
    return null;
  }

  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

export const apiClient = {
  adminLogin(payload: AdminLoginRequest) {
    return requestJson<AdminLoginView>("/api/admin/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  getAdminOverview() {
    return requestJson<AdminOverviewView>("/api/admin/overview");
  },

  getAdminSession() {
    return requestJson<AdminSessionView>("/api/admin/session");
  },

  adminLogout() {
    return requestJson<{ ok: boolean }>("/api/admin/logout", {
      method: "POST",
    });
  },

  getRecruitmentStatus() {
    return requestJson<RecruitmentStatusView>("/api/recruitment-status");
  },

  setAdminRecruitment(open: boolean) {
    return requestJson<RecruitmentStatusView>("/api/admin/recruitment", {
      method: "POST",
      body: JSON.stringify({ open }),
    });
  },

  getAdminSystemMetrics() {
    return requestJson<AdminSystemMetricsView>("/api/admin/system-metrics");
  },

  getAdminDataMetrics() {
    return requestJson<AdminDataMetricsView>("/api/admin/data-metrics");
  },

  getAdminProviderModelUsage() {
    return requestJson<ProviderModelUsageView>("/api/admin/provider-model-usage");
  },

  testAdminDeepSeek() {
    return requestJson<DeepSeekTestResultView>("/api/admin/providers/deepseek/test", {
      method: "POST",
    });
  },

  getAdminParticipants(query = "") {
    const params = new URLSearchParams();
    if (query.trim()) {
      params.set("query", query.trim());
    }
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return requestJson<AdminParticipantsView>(`/api/admin/participants${suffix}`);
  },

  getAdminAssignmentControl() {
    return requestJson<AdminAssignmentControlView>("/api/admin/assignment-control");
  },

  updateAdminAssignmentControl(payload: AdminAssignmentControlUpdateRequest) {
    return requestJson<AdminAssignmentControlView>("/api/admin/assignment-control", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  previewAdminAssignmentBatch(payload: AdminAssignmentBatchPreviewRequest) {
    return requestJson<AdminAssignmentBatchPreviewView>(
      "/api/admin/assignment-control/batch/preview",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  applyAdminAssignmentBatch(payload: AdminAssignmentBatchMutationRequest) {
    return requestJson<AdminAssignmentBatchMutationView>(
      "/api/admin/assignment-control/batch",
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  getAdminCleanDataAudits(status = "") {
    const params = new URLSearchParams();
    if (status) {
      params.set("status", status);
    }
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return requestJson<CleanDataAuditsView>(`/api/admin/clean-data-audits${suffix}`);
  },

  recomputeAdminCleanDataAudits() {
    return requestJson<CleanDataAuditsView>("/api/admin/clean-data-audits/recompute", {
      method: "POST",
    });
  },

  getAdminApiHealth() {
    return requestJson<ApiHealthSummaryView>("/api/admin/api-health");
  },

  getAdminSystemLogs() {
    return requestJson<SystemLogsSummaryView>("/api/admin/system-logs");
  },

  listAdminExportJobs() {
    return requestJson<ExportJobsListView>("/api/admin/export-jobs");
  },

  createAdminExportJob(payload: ExportJobCreateRequest) {
    return requestJson<ExportJobView>("/api/admin/export-jobs", {
      method: "POST",
      body: JSON.stringify({
        filters: {},
        include_test: false,
        ...payload,
      }),
    });
  },

  deleteAdminExportJob(jobUuid: string) {
    return requestJson<{ ok: boolean; job_uuid: string; deleted_file: boolean }>(
      `/api/admin/export-jobs/${jobUuid}`,
      {
        method: "DELETE",
      },
    );
  },

  login(payload: LoginRequest) {
    return requestJson<ParticipantView>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  me() {
    return requestJson<ParticipantView>("/api/me");
  },

  getCurrentPretest() {
    return requestJson<PretestResponseView | null>("/api/pretest/current");
  },

  savePretestDraft(payload: PretestSubmissionRequest) {
    return requestJson<PretestResponseView>("/api/pretest/draft", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  submitPretestFinal(payload: PretestSubmissionRequest) {
    return requestJson<PretestResponseView>("/api/pretest/final", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  startSession(payload: SessionStartRequest) {
    return requestJson<SessionView>("/api/sessions/start", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  startTestSession(payload: SessionStartRequest) {
    return requestJson<SessionView>("/api/test/sessions/start", {
      method: "POST",
      body: JSON.stringify({ ...payload, is_test: true }),
    });
  },

  getSession(sessionId: string) {
    return requestJson<SessionView>(`/api/sessions/${sessionId}`);
  },

  getRuntimeConfig() {
    return requestJson<{ asr_max_duration_seconds: number }>("/api/runtime-config", {
      cache: "no-store",
    });
  },

  async uploadAsr(
    sessionId: string,
    audio: Blob,
    turnIndex: number,
    filename = "turn.webm",
    operationId: string = crypto.randomUUID(),
  ) {
    const formData = new FormData();
    formData.append("session_id", sessionId);
    formData.append("operation_id", operationId);
    formData.append("turn_index", String(turnIndex));
    formData.append("audio", audio, filename);

    const response = await fetch("/api/asr", {
      method: "POST",
      body: formData,
      credentials: "include",
    });

    if (!response.ok) {
      const payload = (await safeParseJson<ApiErrorPayload>(response)) ?? {};
      throw new ApiError(response.status, payload.detail);
    }

    return response.json() as Promise<AsrView>;
  },

  submitTurn(payload: TurnSubmitRequest) {
    return requestJson<TurnView>("/api/turns", {
      method: "POST",
      body: JSON.stringify({
        ...payload,
        operation_id: payload.operation_id ?? crypto.randomUUID(),
      }),
    });
  },

  submitClientTiming(turnId: number, payload: ClientTimingSubmitRequest) {
    return requestJson<ClientTimingView>(`/api/turns/${turnId}/client-timing`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  submitRating(turnId: number, payload: RatingSubmitRequest) {
    return requestJson<RatingSubmitResponse>(`/api/turns/${turnId}/rating`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  completeSession(sessionId: string) {
    return requestJson<SessionView>(`/api/sessions/${sessionId}/complete`, {
      method: "POST",
    });
  },
};
