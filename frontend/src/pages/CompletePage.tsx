import { CheckCircle2 } from "lucide-react";
import { useEffect, useState } from "react";

import type { ParticipantView } from "../api/types";
import { interfaceCopy } from "../experiment/interfaceCopy";

export type CompletionViewMode =
  | "saved"
  | "retryRequired"
  | "retryFailed"
  | "downloadFallback"
  | "test";

interface CompletionViewState {
  message: string;
  showSave: boolean;
  showDownload: boolean;
}

interface CompletePageProps {
  participant: ParticipantView | null;
  mode?: CompletionViewMode;
  supportEmail?: string;
  onSaveData?: () => Promise<CompletionViewMode | void> | CompletionViewMode | void;
}

const DEFAULT_SUPPORT_EMAIL = "XXX@XX.com";

function isLongParticipant(participant: ParticipantView): boolean {
  return participant.participant_type === "long";
}

function isFinalLongDay(participant: ParticipantView): boolean {
  return participant.current_day.day_index >= participant.target_days;
}

function completionMessage(participant: ParticipantView | null, mode: CompletionViewMode): string {
  if (mode === "test" || !participant) {
    return interfaceCopy.completion.testFallback;
  }

  if (mode === "downloadFallback") {
    return interfaceCopy.completion.downloadFallback(DEFAULT_SUPPORT_EMAIL);
  }

  if (!isLongParticipant(participant)) {
    if (mode === "retryRequired") {
      return interfaceCopy.completion.shortRetryRequired;
    }
    if (mode === "retryFailed") {
      return interfaceCopy.completion.shortRetryFailed;
    }
    return interfaceCopy.completion.shortSaved;
  }

  if (isFinalLongDay(participant)) {
    if (mode === "retryRequired") {
      return interfaceCopy.completion.longFinalRetryRequired;
    }
    if (mode === "retryFailed") {
      return interfaceCopy.completion.longFinalRetryFailed;
    }
    return interfaceCopy.completion.longFinalSaved;
  }

  if (mode === "retryRequired") {
    return interfaceCopy.completion.longNonFinalRetryRequired;
  }
  if (mode === "retryFailed") {
    return interfaceCopy.completion.longNonFinalRetryFailed;
  }
  return interfaceCopy.completion.longNonFinalSaved;
}

export function buildCompletionViewState(
  participant: ParticipantView | null,
  mode: CompletionViewMode,
  supportEmail = DEFAULT_SUPPORT_EMAIL,
): CompletionViewState {
  const effectiveMode = participant ? mode : "test";
  const message =
    effectiveMode === "downloadFallback"
      ? interfaceCopy.completion.downloadFallback(supportEmail)
      : completionMessage(participant, effectiveMode);

  return {
    message,
    showSave: effectiveMode === "retryRequired" || effectiveMode === "retryFailed",
    showDownload: effectiveMode === "downloadFallback" || effectiveMode === "test",
  };
}

function downloadCompletionBackup(
  participant: ParticipantView | null,
  viewState: CompletionViewState,
) {
  const completedAt = new Date().toISOString();
  const backup = {
    participant_id: participant?.participant_id ?? null,
    attempt_id: participant?.attempt_id ?? null,
    participant_type: participant?.participant_type ?? null,
    day_index: participant?.current_day.day_index ?? null,
    message: viewState.message,
    completed_at: completedAt,
  };
  const blob = new Blob([JSON.stringify(backup, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `experiment_completion_${participant?.participant_id ?? "backup"}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

export function CompletePage({
  participant,
  mode = participant ? "saved" : "test",
  supportEmail = DEFAULT_SUPPORT_EMAIL,
  onSaveData,
}: CompletePageProps) {
  const [currentMode, setCurrentMode] = useState<CompletionViewMode>(mode);

  useEffect(() => {
    setCurrentMode(mode);
  }, [mode]);

  const viewState = buildCompletionViewState(participant, currentMode, supportEmail);

  const retrySave = async () => {
    const nextMode = await onSaveData?.();
    setCurrentMode(nextMode ?? "retryFailed");
  };

  return (
    <main className="flow-page">
      <section className="flow-card">
        <div className="flow-logo" aria-hidden="true">
          <CheckCircle2 size={34} />
        </div>
        <h1 className="flow-title">{interfaceCopy.completion.title}</h1>
        <p
          className="flow-message"
          dangerouslySetInnerHTML={{ __html: viewState.message }}
        />
        <div className="completion-actions">
          <button
            className="login-btn"
            type="button"
            hidden={!viewState.showSave}
            onClick={() => void retrySave()}
          >
            {interfaceCopy.completion.saveButton}
          </button>
          <button
            className="login-btn secondary"
            type="button"
            hidden={!viewState.showDownload}
            onClick={() => downloadCompletionBackup(participant, viewState)}
          >
            {interfaceCopy.completion.downloadButton}
          </button>
        </div>
        <div className="flow-footer">浙江大学 · 人机交互研究</div>
      </section>
    </main>
  );
}
