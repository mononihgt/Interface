import type { ClientInfo } from "../../api/types";
import {
  getDesktopGateState,
  requestMicrophoneAccess,
  type DesktopGateState,
  type MicrophoneCheckOutcome,
} from "../../components/DesktopGate";

export interface FormalEnvironmentGateResult {
  passed: boolean;
  message: string | null;
  clientInfo: ClientInfo | null;
  state: DesktopGateState | null;
}

function recordingCapabilityStatus() {
  return {
    key: "recording",
    label: "录音能力",
    passed: typeof MediaRecorder !== "undefined",
  } as const;
}

const failureMessages: Record<string, string> = {
  device: "请使用电脑参加正式实验。",
  viewport: "请将浏览器窗口宽度调整到至少 1024 像素。",
  browser: "请使用 Chrome 或 Edge 浏览器。",
  secureContext: "请通过 HTTPS 安全连接访问实验页面。",
  microphone: "未检测到浏览器录音能力，请检查浏览器和麦克风设备。",
  permission: "请在浏览器设置中允许麦克风权限后重试。",
  recording: "当前浏览器不支持录音，请使用最新版 Chrome 或 Edge。",
};

const microphoneFailureMessages: Partial<Record<MicrophoneCheckOutcome, string>> = {
  "capability-unavailable": failureMessages.microphone,
  "permission-query-unavailable":
    "当前浏览器无法检查麦克风权限，请使用最新版 Chrome 或 Edge 后重试。",
  "permission-query-failed": "浏览器未能检查麦克风权限，请刷新页面后重试。",
  "permission-denied": failureMessages.permission,
  "prompt-dismissed": "麦克风授权未完成，请重新点击登录并在浏览器提示中允许访问。",
  "device-unavailable": "未检测到可用麦克风，请连接设备后重试。",
  "request-failed": "浏览器未能启用麦克风，请检查浏览器设置后重试。",
};

export function formatFormalEnvironmentFailure(
  state: DesktopGateState,
  recordingSupported: boolean,
  microphoneCheckOutcome: MicrophoneCheckOutcome = state.microphoneCheckOutcome,
): string {
  const failedKeys: string[] = state.statuses
    .filter((status) => !status.passed)
    .map((status) => status.key);
  const hasMicrophoneFailure =
    failedKeys.includes("microphone") || failedKeys.includes("permission");
  const microphoneFailureMessage = hasMicrophoneFailure
    ? microphoneFailureMessages[microphoneCheckOutcome]
    : undefined;
  if (microphoneFailureMessage) {
    failedKeys.splice(
      0,
      failedKeys.length,
      ...failedKeys.filter((key) => key !== "microphone" && key !== "permission"),
    );
  }
  if (!recordingSupported) failedKeys.push("recording");
  return [
    ...new Set(failedKeys.map((key) => failureMessages[key])),
    microphoneFailureMessage,
  ]
    .filter((message): message is string => Boolean(message))
    .join(" ");
}

export async function runFormalEnvironmentGate(): Promise<FormalEnvironmentGateResult> {
  let state = await getDesktopGateState();
  let microphoneCheckOutcome = state.microphoneCheckOutcome;

  if (
    state.clientInfo.microphone_available &&
    state.clientInfo.microphone_permission === "prompt"
  ) {
    const microphoneAccess = await requestMicrophoneAccess();
    microphoneCheckOutcome = microphoneAccess.outcome;
    state = await getDesktopGateState();
    if (
      microphoneCheckOutcome === "granted" &&
      state.microphoneCheckOutcome !== "granted"
    ) {
      microphoneCheckOutcome = state.microphoneCheckOutcome;
    }
  }

  const recordingSupported = recordingCapabilityStatus().passed;
  if (state.isFormalReady && recordingSupported) {
    return {
      passed: true,
      message: null,
      clientInfo: state.clientInfo,
      state,
    };
  }

  const message = formatFormalEnvironmentFailure(
    state,
    recordingSupported,
    microphoneCheckOutcome,
  );
  console.info(message, state.clientInfo);
  return {
    passed: false,
    message,
    clientInfo: state.clientInfo,
    state,
  };
}
