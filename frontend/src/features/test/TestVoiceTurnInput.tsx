import { Mic, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";

import type { AsrView } from "../../api/types";
import {
  getDesktopGateState,
  requestMicrophoneAccess,
} from "../../components/DesktopGate";
import type { RecordingUnavailableOutcome } from "../../components/VoiceRecorder";
import { VoiceTurnInput } from "../experiment/VoiceTurnInput";

interface TestVoiceTurnInputProps {
  sessionId: string;
  turnIndex: number;
  disabled?: boolean;
  isSubmitting?: boolean;
  onSubmit: (recognizedAsr: AsrView, operationId: string) => Promise<void>;
}

const recordingUnavailableMessages: Record<RecordingUnavailableOutcome, string> = {
  "permission-denied": "麦克风权限未授予，文本输入仍可继续使用。",
  "prompt-dismissed": "麦克风授权未完成，文本输入仍可继续使用。",
  "device-unavailable": "未检测到可用麦克风，文本输入仍可继续使用。",
  "capability-unavailable": "当前浏览器不支持语音录入，文本输入仍可继续使用。",
  "recorder-failed": "语音录入当前不可用，文本输入仍可继续使用。",
};

export function TestVoiceTurnInput({
  sessionId,
  turnIndex,
  disabled = false,
  isSubmitting = false,
  onSubmit,
}: TestVoiceTurnInputProps) {
  const [canUseVoice, setCanUseVoice] = useState(false);
  const [isChecking, setIsChecking] = useState(true);
  const [isRequesting, setIsRequesting] = useState(false);
  const [statusMessage, setStatusMessage] = useState("正在检查麦克风状态。");

  const refreshMicrophoneState = async () => {
    setIsChecking(true);
    try {
      const gateState = await getDesktopGateState();
      const hasMediaRecorder = typeof MediaRecorder !== "undefined";
      const hasMicrophone = gateState.clientInfo.microphone_available;
      const permissionAllowsRecording = !["denied", "unavailable"].includes(
        gateState.clientInfo.microphone_permission,
      );
      const isAvailable = hasMediaRecorder && hasMicrophone && permissionAllowsRecording;

      setCanUseVoice(isAvailable);
      if (!hasMediaRecorder || !hasMicrophone) {
        setStatusMessage("当前浏览器不支持语音录入，文本输入仍可继续使用。");
      } else if (!permissionAllowsRecording) {
        setStatusMessage("麦克风权限尚未授予，文本输入仍可继续使用。");
      } else {
        setStatusMessage("");
      }
    } catch {
      setCanUseVoice(false);
      setStatusMessage("无法检查麦克风状态，文本输入仍可继续使用。");
    } finally {
      setIsChecking(false);
    }
  };

  useEffect(() => {
    void refreshMicrophoneState();
  }, []);

  const handleVoiceUnavailable = (outcome: RecordingUnavailableOutcome) => {
    setCanUseVoice(false);
    setStatusMessage(recordingUnavailableMessages[outcome]);
  };

  const requestPermission = async () => {
    setIsRequesting(true);
    try {
      const result = await requestMicrophoneAccess();
      await refreshMicrophoneState();
      if (result.outcome !== "granted") {
        setCanUseVoice(false);
        setStatusMessage(`${result.message}文本输入仍可继续使用。`);
      }
    } catch {
      setCanUseVoice(false);
      setStatusMessage("浏览器未能完成麦克风授权，文本输入仍可继续使用。");
    } finally {
      setIsRequesting(false);
    }
  };

  if (canUseVoice) {
    return (
      <VoiceTurnInput
        sessionId={sessionId}
        turnIndex={turnIndex}
        disabled={disabled}
        isSubmitting={isSubmitting}
        onSubmit={onSubmit}
        onUnavailable={handleVoiceUnavailable}
      />
    );
  }

  return (
    <section className="voice-input-area" aria-label="测试语音输入">
      <div className="voice-container">
        <p className="transcription-alert" role="status">
          {statusMessage}
        </p>
        <div className="recorder-actions">
          <button
            className="secondary-button"
            type="button"
            onClick={() => void requestPermission()}
            disabled={disabled || isChecking || isRequesting}
          >
            <Mic size={16} aria-hidden="true" />
            <span>{isRequesting ? "请求中" : "请求麦克风权限"}</span>
          </button>
          <button
            className="secondary-button"
            type="button"
            onClick={() => void refreshMicrophoneState()}
            disabled={disabled || isChecking || isRequesting}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>{isChecking ? "检查中" : "重新检查"}</span>
          </button>
        </div>
      </div>
    </section>
  );
}
