import { AlertTriangle, Mic, RefreshCw, RotateCcw, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { apiClient, ApiError, requiresNewOperationId } from "../api/client";
import type { AsrView } from "../api/types";

const AUTO_STOP_SAFETY_MARGIN_MS = 800;

type RecorderState =
  | "idle"
  | "recording"
  | "uploading"
  | "recognizing"
  | "recognized"
  | "failed"
  | "interrupted";

export type RecordingUnavailableOutcome =
  | "permission-denied"
  | "prompt-dismissed"
  | "device-unavailable"
  | "capability-unavailable"
  | "recorder-failed";

interface VoiceRecorderProps {
  sessionId: string;
  turnIndex: number;
  disabled?: boolean;
  onRecognized: (result: AsrView) => void;
  onReset?: () => void;
  onUnavailable?: (outcome: RecordingUnavailableOutcome) => void;
}

function getMimeType(): string | undefined {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  return candidates.find((type) => typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(type));
}

function audioFilename(mimeType: string): string {
  const normalizedType = mimeType.split(";", 1)[0].toLowerCase();
  if (normalizedType === "audio/mp4") {
    return "turn.mp4";
  }
  if (normalizedType === "audio/ogg") {
    return "turn.ogg";
  }
  return "turn.webm";
}

function formatRemainingTime(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
}

function automaticStopDelayMs(maxDurationSeconds: number): number {
  return Math.max(0, maxDurationSeconds * 1000 - AUTO_STOP_SAFETY_MARGIN_MS);
}

function formatMicrophoneError(error: unknown): string {
  const errorName = error instanceof Error ? error.name : "";
  switch (errorName) {
    case "NotAllowedError":
      return "请允许麦克风权限后重试。";
    case "NotFoundError":
      return "未检测到可用麦克风，请连接设备后重试。";
    case "AbortError":
      return "麦克风授权未完成，请重新操作。";
    default:
      return "浏览器未能开始录音，请检查麦克风和浏览器设置后重试。";
  }
}

async function microphonePermissionIsDenied(): Promise<boolean> {
  if (!("permissions" in navigator) || typeof navigator.permissions.query !== "function") {
    return false;
  }

  try {
    const permission = await navigator.permissions.query({
      name: "microphone" as PermissionName,
    });
    return permission.state === "denied";
  } catch {
    return false;
  }
}

export function VoiceRecorder({
  sessionId,
  turnIndex,
  disabled = false,
  onRecognized,
  onReset,
  onUnavailable,
}: VoiceRecorderProps) {
  const [state, setState] = useState<RecorderState>("idle");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [maxDurationSeconds, setMaxDurationSeconds] = useState<number | null>(null);
  const [remainingSeconds, setRemainingSeconds] = useState(0);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const audioBlobRef = useRef<Blob | null>(null);
  const asrOperationIdRef = useRef<string | null>(null);
  const recordingDeadlineRef = useRef<number | null>(null);
  const stopRecordingRef = useRef<() => Promise<void>>(async () => undefined);
  const unavailableNotifiedRef = useRef(false);

  const isBusy =
    disabled ||
    maxDurationSeconds === null ||
    state === "uploading" ||
    state === "recognizing";

  useEffect(() => {
    let active = true;
    void apiClient
      .getRuntimeConfig()
      .then((config) => {
        const configuredDuration = config.asr_max_duration_seconds;
        if (!Number.isInteger(configuredDuration) || configuredDuration <= 0) {
          throw new Error("Invalid recorder runtime configuration.");
        }
        if (active) {
          setMaxDurationSeconds(configuredDuration);
          setRemainingSeconds(configuredDuration);
        }
      })
      .catch(() => {
        if (active) {
          setState("failed");
          setErrorMessage("无法加载录音配置，请刷新页面重试。");
        }
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    return () => {
      if (mediaRecorderRef.current?.state === "recording") {
        mediaRecorderRef.current.stop();
      }
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, []);

  useEffect(() => {
    if (state !== "recording" || recordingDeadlineRef.current === null) {
      return;
    }

    const updateCountdown = () => {
      const deadline = recordingDeadlineRef.current;
      if (deadline === null) {
        return;
      }
      const nextRemaining = Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
      setRemainingSeconds(nextRemaining);
      if (nextRemaining === 0) {
        void stopRecordingRef.current();
      }
    };

    updateCountdown();
    const timerId = window.setInterval(updateCountdown, 250);
    return () => window.clearInterval(timerId);
  }, [state]);

  const notifyUnavailable = (
    outcome: RecordingUnavailableOutcome,
    message: string,
  ) => {
    if (unavailableNotifiedRef.current) {
      return;
    }
    unavailableNotifiedRef.current = true;
    recordingDeadlineRef.current = null;
    setState("failed");
    setErrorMessage(message);
    onUnavailable?.(outcome);
  };

  const startRecording = async () => {
    setErrorMessage(null);
    onReset?.();
    audioBlobRef.current = null;
    asrOperationIdRef.current = null;
    unavailableNotifiedRef.current = false;

    if (maxDurationSeconds === null) {
      setState("failed");
      setErrorMessage("录音配置尚未加载，请稍后重试。");
      return;
    }

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      notifyUnavailable("capability-unavailable", "当前浏览器不支持录音。");
      return;
    }

    let acquiredStream: MediaStream | null = null;
    try {
      acquiredStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const stream = acquiredStream;
      const recorder = new MediaRecorder(stream, getMimeType() ? { mimeType: getMimeType() } : undefined);
      chunksRef.current = [];
      streamRef.current = stream;
      mediaRecorderRef.current = recorder;

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorder.onerror = () => {
        notifyUnavailable("recorder-failed", "录音过程中出现错误。");
        stream.getTracks().forEach((track) => track.stop());
      };

      stream.getAudioTracks().forEach((track) => {
        track.addEventListener(
          "ended",
          () => {
            notifyUnavailable(
              "device-unavailable",
              "未检测到可用麦克风，请连接设备后重试。",
            );
          },
          { once: true },
        );
      });

      recorder.start();
      recordingDeadlineRef.current = Date.now() + automaticStopDelayMs(maxDurationSeconds);
      setRemainingSeconds(maxDurationSeconds);
      setState("recording");
    } catch (error) {
      acquiredStream?.getTracks().forEach((track) => track.stop());
      const errorName = error instanceof Error ? error.name : "";
      if (errorName === "NotAllowedError" || errorName === "PermissionDeniedError") {
        const permissionDenied = await microphonePermissionIsDenied();
        if (permissionDenied) {
          notifyUnavailable("permission-denied", "请允许麦克风权限后重试。");
        } else {
          notifyUnavailable("prompt-dismissed", "麦克风授权未完成，请重新操作。");
        }
      } else if (errorName === "NotFoundError" || errorName === "DevicesNotFoundError") {
        notifyUnavailable(
          "device-unavailable",
          "未检测到可用麦克风，请连接设备后重试。",
        );
      } else if (errorName === "AbortError") {
        notifyUnavailable("prompt-dismissed", "麦克风授权未完成，请重新操作。");
      } else {
        notifyUnavailable("recorder-failed", formatMicrophoneError(error));
      }
    }
  };

  const transcribeRecording = async (blob: Blob, operationId: string) => {
    setState("recognizing");

    try {
      const result = await apiClient.uploadAsr(
        sessionId,
        blob,
        turnIndex,
        audioFilename(blob.type),
        operationId,
      );
      if (result.asr_status !== "success" || !result.asr_text) {
        audioBlobRef.current = null;
        asrOperationIdRef.current = null;
        setState(result.retry_count >= result.max_retry_per_turn ? "interrupted" : "failed");
        setErrorMessage(
          result.retry_count >= result.max_retry_per_turn
            ? "识别多次失败，当前会话已中断。"
            : "语音识别未成功，请重新录音。",
        );
        return;
      }

      audioBlobRef.current = null;
      asrOperationIdRef.current = null;
      setState("recognized");
      onRecognized(result);
    } catch (error) {
      if (requiresNewOperationId(error)) {
        asrOperationIdRef.current = null;
      }
      setState("failed");
      setErrorMessage(error instanceof ApiError ? error.detail : "上传录音失败。");
    }
  };

  const stopRecording = async () => {
    const recorder = mediaRecorderRef.current;
    if (!recorder || recorder.state !== "recording") {
      return;
    }

    setState("uploading");
    recordingDeadlineRef.current = null;
    setRemainingSeconds(0);

    const blob = await new Promise<Blob>((resolve) => {
      recorder.onstop = () => {
        const mimeType = recorder.mimeType || "audio/webm";
        resolve(new Blob(chunksRef.current, { type: mimeType }));
      };
      recorder.stop();
    });

    streamRef.current?.getTracks().forEach((track) => track.stop());
    audioBlobRef.current = blob;
    const operationId = crypto.randomUUID();
    asrOperationIdRef.current = operationId;
    await transcribeRecording(blob, operationId);
  };

  stopRecordingRef.current = stopRecording;

  const retryTranscription = async () => {
    const blob = audioBlobRef.current;
    if (!blob) {
      return;
    }

    setErrorMessage(null);
    setState("uploading");
    const operationId = asrOperationIdRef.current ?? crypto.randomUUID();
    asrOperationIdRef.current = operationId;
    await transcribeRecording(blob, operationId);
  };

  const reset = () => {
    if (mediaRecorderRef.current?.state === "recording") {
      mediaRecorderRef.current.stop();
    }
    streamRef.current?.getTracks().forEach((track) => track.stop());
    mediaRecorderRef.current = null;
    streamRef.current = null;
    chunksRef.current = [];
    audioBlobRef.current = null;
    asrOperationIdRef.current = null;
    recordingDeadlineRef.current = null;
    setRemainingSeconds(maxDurationSeconds ?? 0);
    setState(maxDurationSeconds === null ? "failed" : "idle");
    setErrorMessage(
      maxDurationSeconds === null ? "无法加载录音配置，请刷新页面重试。" : null,
    );
    onReset?.();
  };

  const stopLabel = state === "uploading" || state === "recognizing" ? "转写中" : "停止并转写";

  return (
    <div className="voice-recorder-controls">
      <div className="recorder-actions">
        <button
          className="primary-button"
          type="button"
          onClick={() => void startRecording()}
          disabled={isBusy || state === "recording" || state === "interrupted"}
        >
          <Mic size={16} aria-hidden="true" />
          <span>开始录音</span>
        </button>
        <button
          className="secondary-button"
          type="button"
          onClick={() => void stopRecording()}
          disabled={disabled || state !== "recording"}
        >
          <Square size={16} aria-hidden="true" />
          <span>{stopLabel}</span>
        </button>
        <button className="secondary-button" type="button" onClick={reset} disabled={disabled || state === "recording"}>
          <RotateCcw size={16} aria-hidden="true" />
          <span>重录</span>
        </button>
        {state === "failed" && audioBlobRef.current ? (
          <button
            className="secondary-button"
            type="button"
            onClick={() => void retryTranscription()}
            disabled={isBusy}
          >
            <RefreshCw size={16} aria-hidden="true" />
            <span>重试转写</span>
          </button>
        ) : null}
        <div className="recording-countdown" role="timer" aria-label="录音剩余时间">
          <span>{state === "recording" ? "剩余" : "录音上限"}</span>
          <strong>
            {maxDurationSeconds === null
              ? "--:--"
              : formatRemainingTime(
                  state === "recording" ? remainingSeconds : maxDurationSeconds,
                )}
          </strong>
        </div>
      </div>

      {errorMessage ? (
        <p className="transcription-alert" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>{errorMessage}</span>
        </p>
      ) : null}
    </div>
  );
}
