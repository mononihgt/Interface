import type { ArtifactKind, ArtifactStatus } from "../../api/types";
import { deriveExecutionPanelState, type ExecutionPanelPhase } from "./executionPanelState";
import { asRecord, displayValue } from "./safeArtifact";

interface ExecutionPanelProps {
  topicTitle?: string;
  artifactKind: ArtifactKind;
  artifactStatus: ArtifactStatus;
  payload: unknown;
  isSubmitting: boolean;
  ratingPhase: boolean;
}

const PHASE_MESSAGES: Record<Exclude<ExecutionPanelPhase, "awaiting" | "failure">, string> = {
  empty: "等待你提供材料后，助手会在这里生成结果。",
  loading: "助手正在处理本轮内容。",
  success: "执行结果已更新。",
};

function phaseMessage(phase: ExecutionPanelPhase, hasArtifact: boolean): string {
  if (phase === "awaiting") {
    return hasArtifact
      ? "还需要补充信息，已有结果会保留在下方。"
      : "还需要补充信息，请补充后继续。";
  }
  if (phase === "failure") {
    return hasArtifact
      ? "本轮未能生成有效结果，已有结果会保留在下方。"
      : "本轮未能生成有效结果，请重试。";
  }
  return PHASE_MESSAGES[phase];
}

function renderScheduleTable(payload: Record<string, unknown>) {
  const columns = payload.columns as string[];
  const rows = payload.rows as Array<Record<string, unknown>>;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column}>{column}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={rowIndex}>
              {columns.map((column) => (
                <td key={column}>{displayValue(row[column])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function renderCopyEditor(payload: Record<string, unknown>) {
  const versions = payload.versions as Array<Record<string, unknown>>;
  const selectedVersion = asRecord(payload.selected_version);
  const revisionNotes = Array.isArray(payload.revision_notes) ? payload.revision_notes : [];
  return (
    <>
      <div className="artifact-stack">
        {versions.map((version, index) => {
          const isSelected = version.id === selectedVersion?.version_id;
          return (
            <article
              className={`artifact-block${isSelected ? " is-selected" : ""}`}
              key={displayValue(version.id) || index}
            >
              <div className="artifact-block__header">
                <strong>{displayValue(version.label)}</strong>
                {isSelected ? <span className="tag">推荐</span> : null}
              </div>
              <p>{displayValue(version.text)}</p>
            </article>
          );
        })}
      </div>
      {selectedVersion?.reason ? (
        <p className="muted">推荐理由：{displayValue(selectedVersion.reason)}</p>
      ) : null}
      {revisionNotes.length ? (
        <ul className="plain-list">
          {revisionNotes.map((note, index) => (
            <li key={`${displayValue(note)}-${index}`}>{displayValue(note)}</li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

function renderExecutionArtifact(kind: ArtifactKind, payload: Record<string, unknown>) {
  if (kind === "schedule_table") {
    return renderScheduleTable(payload);
  }
  if (kind === "copy_editor") {
    return renderCopyEditor(payload);
  }
  return null;
}

export function ExecutionPanel({
  topicTitle,
  artifactKind,
  artifactStatus,
  payload,
  isSubmitting,
  ratingPhase,
}: ExecutionPanelProps) {
  const state = deriveExecutionPanelState({
    artifactKind,
    artifactStatus,
    artifactPayload: payload,
    isSubmitting,
    ratingPhase,
  });

  return (
    <aside
      className="execution-panel"
      aria-label="任务执行状态"
      data-execution-phase={state.phase}
      data-rating-phase={state.ratingPhase ? "active" : "inactive"}
    >
      <div className="execution-panel__header">
        <h2>执行状态</h2>
        <p>
          {topicTitle
            ? `${topicTitle}正在进行`
            : "助手将在此完成任务操作"}
        </p>
      </div>
      <div className="execution-panel__body">
        <p className={`execution-panel__status execution-panel__status--${state.phase}`}>
          {phaseMessage(state.phase, Boolean(state.artifact))}
        </p>
        {state.ratingPhase ? (
          <p className="execution-panel__rating-status">请完成本轮评分。</p>
        ) : null}
        {state.artifact
          ? renderExecutionArtifact(artifactKind, state.artifact)
          : null}
      </div>
    </aside>
  );
}
