import { AlertTriangle, CheckCircle2, Laptop2, Mic, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";

import type { ClientInfo, MicrophonePermission } from "../api/types";

type GateKey =
  | "device"
  | "viewport"
  | "browser"
  | "secureContext"
  | "microphone"
  | "permission";

interface GateStatus {
  key: GateKey;
  label: string;
  passed: boolean;
  detail: string;
}

export type MicrophoneCheckOutcome =
  | "prompt"
  | "granted"
  | "capability-unavailable"
  | "permission-query-unavailable"
  | "permission-query-failed"
  | "permission-denied"
  | "prompt-dismissed"
  | "device-unavailable"
  | "request-failed";

export interface MicrophoneAccessResult {
  outcome: MicrophoneCheckOutcome;
  message: string;
}

export interface DesktopGateState {
  clientInfo: ClientInfo;
  statuses: GateStatus[];
  microphoneCheckOutcome: MicrophoneCheckOutcome;
  isFormalReady: boolean;
  refreshedAt: number;
}

interface DesktopGateProps {
  onChange?: (state: DesktopGateState) => void;
  compact?: boolean;
}

const deviceTypeLabels: Record<ClientInfo["device_type"], string> = {
  desktop: "桌面设备",
  mobile: "手机",
  tablet: "平板设备",
};

function detectBrowser(userAgent: string): { name: string; version: string | null } {
  const edgeMatch = userAgent.match(/Edg\/([\d.]+)/);
  if (edgeMatch) {
    return { name: "edge", version: edgeMatch[1] };
  }

  const chromeMatch = userAgent.match(/Chrome\/([\d.]+)/);
  if (chromeMatch && !userAgent.includes("OPR/")) {
    return { name: "chrome", version: chromeMatch[1] };
  }

  return { name: "unsupported", version: null };
}

function inferDeviceType(width: number, userAgent: string): ClientInfo["device_type"] {
  const ua = userAgent.toLowerCase();
  if (/ipad|tablet/.test(ua)) {
    return "tablet";
  }
  if (/android|iphone|mobile/.test(ua)) {
    return "mobile";
  }
  if (width < 768) {
    return "mobile";
  }
  if (width < 1024) {
    return "tablet";
  }
  return "desktop";
}

interface MicrophonePermissionCheck {
  permission: MicrophonePermission;
  outcome: MicrophoneCheckOutcome;
}

async function readMicrophonePermission(): Promise<MicrophonePermissionCheck> {
  if (!navigator.mediaDevices?.getUserMedia) {
    return {
      permission: "unavailable",
      outcome: "capability-unavailable",
    };
  }

  if (!("permissions" in navigator) || typeof navigator.permissions.query !== "function") {
    return {
      permission: "prompt",
      outcome: "permission-query-unavailable",
    };
  }

  try {
    const result = await navigator.permissions.query({
      name: "microphone" as PermissionName,
    });
    if (result.state === "granted" || result.state === "denied" || result.state === "prompt") {
      return {
        permission: result.state,
        outcome:
          result.state === "granted"
            ? "granted"
            : result.state === "denied"
              ? "permission-denied"
              : "prompt",
      };
    }
  } catch {
    return {
      permission: "prompt",
      outcome: "permission-query-failed",
    };
  }

  return {
    permission: "prompt",
    outcome: "permission-query-failed",
  };
}

const microphoneAccessMessages: Record<MicrophoneCheckOutcome, string> = {
  prompt: "尚未完成麦克风授权，请在浏览器提示中允许访问。",
  granted: "麦克风权限检查已完成，请查看最新门禁状态。",
  "capability-unavailable": "当前浏览器无法请求麦克风权限。",
  "permission-query-unavailable": "当前浏览器无法检查麦克风权限，请使用最新版 Chrome 或 Edge 后重试。",
  "permission-query-failed": "浏览器未能检查麦克风权限，请刷新页面后重试。",
  "permission-denied": "麦克风权限仍未授予，请在浏览器中允许访问后重试。",
  "prompt-dismissed": "麦克风授权未完成，请重新点击并在浏览器提示中允许访问。",
  "device-unavailable": "未检测到可用麦克风，请连接设备后重试。",
  "request-failed": "浏览器未能完成麦克风授权，请检查设置后重试。",
};

function microphoneAccessResult(outcome: MicrophoneCheckOutcome): MicrophoneAccessResult {
  return {
    outcome,
    message: microphoneAccessMessages[outcome],
  };
}

export async function requestMicrophoneAccess(): Promise<MicrophoneAccessResult> {
  if (!navigator.mediaDevices?.getUserMedia) {
    return microphoneAccessResult("capability-unavailable");
  }

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((track) => track.stop());
    return microphoneAccessResult("granted");
  } catch (error) {
    const errorName = error instanceof DOMException ? error.name : "";
    if (errorName === "NotAllowedError" || errorName === "PermissionDeniedError") {
      const permissionCheck = await readMicrophonePermission();
      if (permissionCheck.outcome === "permission-denied") {
        return microphoneAccessResult("permission-denied");
      }
      if (
        permissionCheck.outcome === "permission-query-unavailable" ||
        permissionCheck.outcome === "permission-query-failed"
      ) {
        return microphoneAccessResult(permissionCheck.outcome);
      }
      return microphoneAccessResult("prompt-dismissed");
    }
    if (errorName === "NotFoundError" || errorName === "DevicesNotFoundError") {
      return microphoneAccessResult("device-unavailable");
    }
    if (errorName === "AbortError") {
      return microphoneAccessResult("prompt-dismissed");
    }
    return microphoneAccessResult("request-failed");
  }
}

async function collectGateState(): Promise<DesktopGateState> {
  const viewportWidth = window.innerWidth;
  const userAgent = navigator.userAgent;
  const browser = detectBrowser(userAgent);
  const permissionCheck = await readMicrophonePermission();
  const permission = permissionCheck.permission;
  const deviceType = inferDeviceType(viewportWidth, userAgent);
  const microphoneAvailable = Boolean(navigator.mediaDevices?.getUserMedia);

  const clientInfo: ClientInfo = {
    device_type: deviceType,
    viewport_width: viewportWidth,
    is_secure_context: window.isSecureContext,
    browser_name: browser.name,
    browser_version: browser.version,
    microphone_available: microphoneAvailable,
    microphone_permission: permission,
  };

  const statuses: GateStatus[] = [
    {
      key: "device",
      label: "设备类型",
      passed: deviceType === "desktop",
      detail:
        deviceType === "desktop"
          ? "已识别为桌面设备"
          : `当前识别为${deviceTypeLabels[deviceType]}`,
    },
    {
      key: "viewport",
      label: "窗口宽度",
      passed: viewportWidth >= 1024,
      detail: `${viewportWidth}px`,
    },
    {
      key: "browser",
      label: "浏览器",
      passed: browser.name === "chrome" || browser.name === "edge",
      detail:
        browser.name === "unsupported"
          ? "仅支持 Chrome / Edge"
          : `${browser.name} ${browser.version ?? ""}`.trim(),
    },
    {
      key: "secureContext",
      label: "安全上下文",
      passed: window.isSecureContext,
      detail: window.isSecureContext ? "已启用" : "需在安全上下文中访问",
    },
    {
      key: "microphone",
      label: "麦克风能力",
      passed: microphoneAvailable,
      detail: microphoneAvailable ? "浏览器支持录音" : "浏览器不可用",
    },
    {
      key: "permission",
      label: "麦克风权限",
      passed: permission === "granted",
      detail:
        permission === "granted"
          ? "已授权"
          : permission === "prompt"
            ? "尚未授权"
            : permission === "denied"
              ? "已拒绝"
              : "不可用",
    },
  ];

  return {
    clientInfo,
    statuses,
    microphoneCheckOutcome: permissionCheck.outcome,
    isFormalReady: statuses.every((status) => status.passed),
    refreshedAt: Date.now(),
  };
}

export function DesktopGate({ onChange, compact = false }: DesktopGateProps) {
  const [state, setState] = useState<DesktopGateState | null>(null);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isRequestingMic, setIsRequestingMic] = useState(false);
  const [permissionMessage, setPermissionMessage] = useState<string | null>(null);

  const refresh = async () => {
    setIsRefreshing(true);
    try {
      const nextState = await collectGateState();
      setState(nextState);
      onChange?.(nextState);
    } finally {
      setIsRefreshing(false);
    }
  };

  const requestMicrophoneAccessForPanel = async () => {
    setIsRequestingMic(true);
    setPermissionMessage(null);

    try {
      const result = await requestMicrophoneAccess();
      setPermissionMessage(result.message);
    } finally {
      await refresh();
      setIsRequestingMic(false);
    }
  };

  useEffect(() => {
    void refresh();

    const handleResize = () => {
      void refresh();
    };

    window.addEventListener("resize", handleResize);
    return () => {
      window.removeEventListener("resize", handleResize);
    };
  }, []);

  const summary = useMemo(() => {
    if (!state) {
      return "正在检查正式实验环境";
    }
    return state.isFormalReady ? "正式实验环境已就绪" : "当前环境不满足正式实验要求";
  }, [state]);

  return (
    <section className={`panel gate-panel${compact ? " gate-panel--compact" : ""}`}>
      <div className="panel-heading">
        <div>
          <h2>{summary}</h2>
        </div>
        <div className="button-row">
          <button
            className="secondary-button"
            type="button"
            onClick={() => void requestMicrophoneAccessForPanel()}
            disabled={isRequestingMic || !navigator.mediaDevices?.getUserMedia}
          >
            <Mic size={18} aria-hidden="true" />
            <span>{isRequestingMic ? "请求中" : "请求麦克风权限"}</span>
          </button>
          <button className="icon-button" type="button" onClick={() => void refresh()} disabled={isRefreshing}>
            <ShieldCheck size={18} aria-hidden="true" />
            <span>{isRefreshing ? "刷新中" : "刷新检查"}</span>
          </button>
        </div>
      </div>
      {permissionMessage ? <p className="status-inline">{permissionMessage}</p> : null}
      <div className="gate-grid">
        {(state?.statuses ?? []).map((status) => (
          <article className={`gate-item${status.passed ? " is-passed" : " is-failed"}`} key={status.key}>
            <div className="gate-item__head">
              {status.key === "browser" ? (
                <Laptop2 size={16} aria-hidden="true" />
              ) : status.key === "permission" || status.key === "microphone" ? (
                <Mic size={16} aria-hidden="true" />
              ) : status.passed ? (
                <CheckCircle2 size={16} aria-hidden="true" />
              ) : (
                <AlertTriangle size={16} aria-hidden="true" />
              )}
              <span>{status.label}</span>
            </div>
            <strong>{status.passed ? "通过" : "未通过"}</strong>
            <p>{status.detail}</p>
          </article>
        ))}
      </div>
    </section>
  );
}

export async function getDesktopGateState(): Promise<DesktopGateState> {
  return collectGateState();
}
