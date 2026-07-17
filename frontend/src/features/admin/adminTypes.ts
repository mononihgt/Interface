export type AdminSectionKey = "system" | "data" | "export" | "control" | "analysis";

export interface AdminSectionDefinition {
  key: AdminSectionKey;
  label: string;
  disabled?: boolean;
}

export const ADMIN_SECTIONS: AdminSectionDefinition[] = [
  { key: "system", label: "系统监控" },
  { key: "data", label: "数据监控" },
  { key: "export", label: "数据导出" },
  { key: "control", label: "实验控制" },
  { key: "analysis", label: "数据分析", disabled: true },
];

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return new Intl.NumberFormat("zh-CN").format(value);
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  return `${value.toFixed(1)}%`;
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "-";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let size = value / 1024;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "-";
  }
  return value.replace("T", " ").replace(/\.\d+$/, "");
}

const PARTICIPANT_TYPE_LABELS: Record<string, string> = {
  short: "短程",
  long: "长程",
};

const CONDITION_LABELS: Record<string, string> = {
  human: "人类来源",
  tool: "工具来源",
};

const SUBCONDITION_LABELS: Record<string, string> = {
  qa: "问答",
  planning: "规划",
  chat: "聊天",
  decision: "决策",
  execution: "执行",
};

const ERROR_TYPE_LABELS: Record<string, string> = {
  factual_minor: "事实轻微",
  factual_major: "事实严重",
  logic_minor: "逻辑轻微",
  logic_major: "逻辑严重",
  social_minor: "社交轻微",
  social_major: "社交严重",
  system_failure: "系统失败",
};

const STATUS_LABELS: Record<string, string> = {
  active: "进行中",
  blocked: "已阻止",
  completed: "已完成",
  failed: "失败",
  invalid: "无效",
  interrupted: "中断",
  local_fallback: "本地兜底",
  queued: "排队中",
  review_needed: "需复核",
  running: "运行中",
  started: "已开始",
  succeeded: "已成功",
  success: "成功",
  timeout: "超时",
  http_error: "HTTP 错误",
  invalid_response: "响应无效",
  eligible: "合格",
  excluded: "已排除",
  abandoned: "已放弃",
  converted_to_short: "已转短程",
  withdrawn: "已退出",
};

const EXPORT_TYPE_LABELS: Record<string, string> = {
  experiment_data: "全部实验数据",
  complete_no_external_error_data: "完整无外源错误数据",
  reimbursement: "报销数据",
};

const RISK_FLAG_LABELS: Record<string, string> = {
  api_failure: "API 失败",
  local_fallback: "本地兜底",
  asr_failed: "ASR 失败",
  asr_repeated_failure: "ASR 重复失败",
  missing_rating: "缺少评分",
  error_not_presented: "错误未呈现",
  artifact_schema_error: "产物结构错误",
  abandoned: "中途放弃",
  long_term_missed_day: "长程缺席",
};

function labelFromMap(
  value: string | null | undefined,
  labels: Record<string, string>,
): string {
  if (!value) {
    return "-";
  }
  return labels[value] ?? value;
}

export function formatParticipantType(value: string | null | undefined): string {
  return labelFromMap(value, PARTICIPANT_TYPE_LABELS);
}

export function formatCondition(value: string | null | undefined): string {
  return labelFromMap(value, CONDITION_LABELS);
}

export function formatSubcondition(value: string | null | undefined): string {
  return labelFromMap(value, SUBCONDITION_LABELS);
}

export function formatErrorType(value: string | null | undefined): string {
  return labelFromMap(value, ERROR_TYPE_LABELS);
}

export function formatStatusLabel(value: string | null | undefined): string {
  return labelFromMap(value, STATUS_LABELS);
}

export function formatExportType(value: string | null | undefined): string {
  return labelFromMap(value, EXPORT_TYPE_LABELS);
}

export function formatRiskFlag(value: string | null | undefined): string {
  return labelFromMap(value, RISK_FLAG_LABELS);
}

export function formatAssignmentBatchScope(scope: {
  kind: "explicit_cells" | "filter_snapshot";
  description: string;
}): string {
  if (scope.kind === "filter_snapshot") {
    return `筛选快照（${scope.description}）`;
  }
  return `显式分配单元（${scope.description}）`;
}

export function formatAssignmentBatchSelectedCells(scope: {
  selected_cells: Array<{
    participant_type: string;
    condition: string;
    subcondition: string;
    error_type_id: string;
  }>;
}): string[] {
  const grouped = new Map<string, string[]>();
  for (const cell of scope.selected_cells) {
    const group = [
      formatParticipantType(cell.participant_type),
      formatCondition(cell.condition),
      formatSubcondition(cell.subcondition),
    ].join(" / ");
    grouped.set(group, [
      ...(grouped.get(group) ?? []),
      formatErrorType(cell.error_type_id),
    ]);
  }
  return Array.from(grouped, ([group, errorTypes]) =>
    `${group}：${errorTypes.join("、")}`,
  );
}
