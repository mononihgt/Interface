const INTERNAL_ARTIFACT_KEYS = new Set([
  "errorInjected",
  "error_injected",
  "errorTypeId",
  "error_type_id",
  "originalValue",
  "mutatedValue",
  "mutatedField",
  "plannedErrorTurn",
  "planned_error_turn",
  "scenarioId",
  "scenario_id",
  "condition",
  "subcondition",
  "topicKey",
  "topic_key",
]);

export function sanitizeArtifactPayload(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(sanitizeArtifactPayload);
  }
  if (!value || typeof value !== "object") {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([key]) => !INTERNAL_ARTIFACT_KEYS.has(key))
      .map(([key, nestedValue]) => [key, sanitizeArtifactPayload(nestedValue)]),
  );
}

export function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export interface ScheduleTableArtifact extends Record<string, unknown> {
  actionType: "schedule_table";
  columns: string[];
  rows: Array<Record<string, string>>;
}

export interface CopyEditorArtifact extends Record<string, unknown> {
  actionType: "copy_editor";
  versions: Array<{
    id: string;
    label: string;
    text: string;
  }>;
}

export type SafeExecutionArtifact = ScheduleTableArtifact | CopyEditorArtifact;

const SCHEDULE_COLUMNS = ["日期", "时间", "地点", "任务", "备注"];
const SCHEDULE_ROW_KEYS = ["date", "time", "location", "task", "note"];

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function normalizeScheduleTableArtifact(
  record: Record<string, unknown>,
): ScheduleTableArtifact | null {
  if (
    record.actionType !== "schedule_table" ||
    !Array.isArray(record.columns) ||
    record.columns.length !== SCHEDULE_COLUMNS.length ||
    !record.columns.every((column, index) => column === SCHEDULE_COLUMNS[index]) ||
    !Array.isArray(record.rows) ||
    record.rows.length === 0
  ) {
    return null;
  }
  const canonicalRows = record.rows.map((row) => {
    const rowRecord = asRecord(row);
    if (!rowRecord || !SCHEDULE_ROW_KEYS.every((key) => typeof rowRecord[key] === "string")) {
      return null;
    }
    return Object.fromEntries(
      SCHEDULE_COLUMNS.map((column, index) => [column, rowRecord[SCHEDULE_ROW_KEYS[index]]]),
    ) as Record<string, string>;
  });
  if (canonicalRows.some((row) => row === null)) {
    return null;
  }
  return {
    actionType: "schedule_table",
    columns: [...SCHEDULE_COLUMNS],
    rows: canonicalRows as Array<Record<string, string>>,
  };
}

function isCopyEditorArtifact(record: Record<string, unknown>): record is CopyEditorArtifact {
  if (
    record.actionType !== "copy_editor" ||
    !Array.isArray(record.versions) ||
    record.versions.length < 2 ||
    record.versions.length > 3
  ) {
    return false;
  }
  return record.versions.every((version) => {
    const versionRecord = asRecord(version);
    return Boolean(
      versionRecord &&
        isNonEmptyString(versionRecord.id) &&
        isNonEmptyString(versionRecord.label) &&
        isNonEmptyString(versionRecord.text),
    );
  });
}

export function safeExecutionArtifact(
  kind: "schedule_table" | "copy_editor" | null,
  value: unknown,
): SafeExecutionArtifact | null {
  const record = asRecord(sanitizeArtifactPayload(value));
  if (!record || !kind) {
    return null;
  }
  if (kind === "schedule_table") {
    return normalizeScheduleTableArtifact(record);
  }
  if (kind === "copy_editor" && isCopyEditorArtifact(record)) {
    return record;
  }
  return null;
}

export function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") {
    return "-";
  }
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return JSON.stringify(value);
}
