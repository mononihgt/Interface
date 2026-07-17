import { LoaderCircle } from "lucide-react";
import { useRef, useState } from "react";

import { requiresNewOperationId } from "../../api/client";
import type { AsrView } from "../../api/types";
import { VoiceRecorder } from "../../components/VoiceRecorder";
import type { RecordingUnavailableOutcome } from "../../components/VoiceRecorder";
import { interfaceCopy } from "../../experiment/interfaceCopy";

interface VoiceTurnInputProps {
  sessionId: string;
  turnIndex: number;
  disabled?: boolean;
  isSubmitting?: boolean;
  onSubmit: (recognizedAsr: AsrView, operationId: string) => Promise<void>;
  onUnavailable?: (outcome: RecordingUnavailableOutcome) => void;
}

export function VoiceTurnInput({
  sessionId,
  turnIndex,
  disabled = false,
  isSubmitting = false,
  onSubmit,
  onUnavailable,
}: VoiceTurnInputProps) {
  const [recognizedAsr, setRecognizedAsr] = useState<AsrView | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const turnOperationIdRef = useRef<string | null>(null);

  const canSubmit =
    !disabled &&
    !isSubmitting &&
    recognizedAsr?.asr_status === "success" &&
    Boolean(recognizedAsr.asr_text);

  const submit = async () => {
    if (!recognizedAsr || !canSubmit) {
      return;
    }

    setSubmitError(null);
    const operationId = turnOperationIdRef.current ?? crypto.randomUUID();
    turnOperationIdRef.current = operationId;

    try {
      await onSubmit(recognizedAsr, operationId);
      turnOperationIdRef.current = null;
      setRecognizedAsr(null);
    } catch (error) {
      if (requiresNewOperationId(error)) {
        turnOperationIdRef.current = null;
      }
      setSubmitError(error instanceof Error ? error.message : "发送失败，请重试。");
    }
  };

  return (
    <section className="voice-input-area" aria-label="语音输入">
      <div className="voice-container">
        <div className="voice-visual">
          <div className="transcript-text">
            {recognizedAsr?.asr_status === "success" && recognizedAsr.asr_text
              ? `识别结果：${recognizedAsr.asr_text}`
              : interfaceCopy.experiment.voiceHint}
          </div>
        </div>
        <VoiceRecorder
          sessionId={sessionId}
          turnIndex={turnIndex}
          disabled={disabled || isSubmitting}
          onRecognized={(result) => {
            setSubmitError(null);
            turnOperationIdRef.current = null;
            setRecognizedAsr(result);
          }}
          onReset={() => {
            turnOperationIdRef.current = null;
            setRecognizedAsr(null);
          }}
          onUnavailable={onUnavailable}
        />
        <button
          className="send-button"
          type="button"
          onClick={() => void submit()}
          disabled={!canSubmit}
        >
          {isSubmitting ? (
            <span className="status-inline">
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
              <span>{interfaceCopy.rating.submitting}</span>
            </span>
          ) : (
            interfaceCopy.experiment.send
          )}
        </button>
      </div>
      <div className="timer-info">{interfaceCopy.experiment.timerWaiting}</div>
      {submitError ? (
        <p className="transcription-alert" role="alert">
          {submitError}
        </p>
      ) : null}
    </section>
  );
}
