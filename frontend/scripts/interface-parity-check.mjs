import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { build as buildVite } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const frontendRoot = join(here, "..");
const projectRoot = join(frontendRoot, "..");
const siblingInterfaceRoot = join(projectRoot, "..", "interface");
const canonicalInterfaceRoot = existsSync(siblingInterfaceRoot)
  ? siblingInterfaceRoot
  : join(projectRoot, "..", "..", "..", "interface");

function read(relativePath) {
  return readFileSync(join(frontendRoot, relativePath), "utf8");
}

function readIfExists(relativePath, label) {
  const absolutePath = join(frontendRoot, relativePath);
  if (!existsSync(absolutePath)) {
    failures.push(`${relativePath}: missing ${label}`);
    return "";
  }
  return readFileSync(absolutePath, "utf8");
}

function readCanonical(relativePath) {
  return readFileSync(join(canonicalInterfaceRoot, relativePath), "utf8");
}

function readProject(relativePath) {
  return readFileSync(join(projectRoot, relativePath), "utf8");
}

const failures = [];

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function expectIncludes(filePath, haystack, needle, label) {
  if (!haystack.includes(needle)) {
    failures.push(`${filePath}: missing ${label}: ${needle}`);
  }
}

function expectNotIncludes(filePath, haystack, needle, label) {
  if (haystack.includes(needle)) {
    failures.push(`${filePath}: forbidden ${label}: ${needle}`);
  }
}

function expectMatches(filePath, haystack, pattern, label) {
  if (!pattern.test(haystack)) {
    failures.push(`${filePath}: missing ${label}: ${pattern}`);
  }
}

function expectNotMatches(filePath, haystack, pattern, label) {
  if (pattern.test(haystack)) {
    failures.push(`${filePath}: forbidden ${label}: ${pattern}`);
  }
}

function expectEqual(label, actual, expected) {
  if (actual !== expected) {
    failures.push(`${label}: expected ${JSON.stringify(expected)}, received ${JSON.stringify(actual)}`);
  }
}

function expectDeepEqual(label, actual, expected) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    failures.push(`${label}: expected ${JSON.stringify(expected)}, received ${JSON.stringify(actual)}`);
  }
}

function expectParticipantSafe(label, message) {
  const forbiddenFragments = [
    "Value error",
    "NotAllowedError",
    "PermissionDeniedError",
    "NotFoundError",
    "DevicesNotFoundError",
    "AbortError",
    "Request failed:",
    "raw stack content",
  ];
  forbiddenFragments.forEach(fragment => {
    if (message.includes(fragment)) {
      failures.push(`${label}: exposed forbidden raw text: ${fragment}`);
    }
  });
}

async function loadExecutableFormatters() {
  const virtualEntryId = "virtual:interface-parity-runtime";
  const resolvedVirtualEntryId = `\0${virtualEntryId}`;
  const buildResult = await buildVite({
    configFile: false,
    root: frontendRoot,
    logLevel: "silent",
    plugins: [{
      name: "interface-parity-runtime",
      resolveId(id) {
        return id === virtualEntryId ? resolvedVirtualEntryId : null;
      },
      load(id) {
        if (id !== resolvedVirtualEntryId) return null;
        return `
          export { ApiError } from "${join(frontendRoot, "src/api/client.ts")}";
          export { validateLoginFields } from "${join(frontendRoot, "src/features/login/loginValidation.ts")}";
          export { formatWelcomeLoginError } from "${join(frontendRoot, "src/features/login/welcomeErrorMessages.ts")}";
          export { reconcileRecruitmentUpdate } from "${join(frontendRoot, "src/features/admin/recruitmentState.ts")}";
          export { deriveExecutionPanelState } from "${join(frontendRoot, "src/features/experiment/executionPanelState.ts")}";
          export { safeExecutionArtifact } from "${join(frontendRoot, "src/features/experiment/safeArtifact.ts")}";
          export {
            formatFormalEnvironmentFailure,
            runFormalEnvironmentGate,
          } from "${join(frontendRoot, "src/features/environment/formalEnvironmentGate.ts")}";
        `;
      },
    }],
    build: {
      write: false,
      minify: false,
      rollupOptions: {
        input: virtualEntryId,
        preserveEntrySignatures: "strict",
        output: {
          format: "cjs",
          entryFileNames: "interface-parity-runtime.cjs",
        },
      },
    },
  });
  const buildOutputs = Array.isArray(buildResult) ? buildResult : [buildResult];
  const entryChunk = buildOutputs
    .flatMap(output => output.output)
    .find(output => output.type === "chunk" && output.isEntry);
  if (!entryChunk) {
    throw new Error("Interface parity formatter bundle did not produce an entry chunk.");
  }
  const runtimeModule = { exports: {} };
  const executeBundle = new Function("module", "exports", entryChunk.code);
  executeBundle(runtimeModule, runtimeModule.exports);
  return runtimeModule.exports;
}

async function runExecutableFormatterChecks() {
  const {
    ApiError,
    formatFormalEnvironmentFailure,
    formatWelcomeLoginError,
    reconcileRecruitmentUpdate,
    deriveExecutionPanelState,
    safeExecutionArtifact,
    runFormalEnvironmentGate,
    validateLoginFields,
  } = await loadExecutableFormatters();

  const scheduleArtifact = {
    actionType: "schedule_table",
    columns: ["日期", "时间", "地点", "任务", "备注"],
    rows: [{
      date: "7月13日",
      time: "09:00",
      location: "会议室A",
      task: "项目会",
      note: "带材料",
    }],
  };
  const normalizedScheduleArtifact = {
    actionType: "schedule_table",
    columns: ["日期", "时间", "地点", "任务", "备注"],
    rows: [{
      日期: "7月13日",
      时间: "09:00",
      地点: "会议室A",
      任务: "项目会",
      备注: "带材料",
    }],
  };
  const copyArtifact = {
    actionType: "copy_editor",
    versions: [
      { id: "v1", label: "直接版", text: "今晚会晚到十分钟，请大家先开始。" },
      { id: "v2", label: "礼貌版", text: "抱歉，我今晚会晚到十分钟，麻烦大家先开始。" },
    ],
  };
  const executionCases = [
    {
      label: "empty session start",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "none",
        artifactPayload: null,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "empty", artifact: null, ratingPhase: false },
    },
    {
      label: "loading transient",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "none",
        artifactPayload: null,
        isSubmitting: true,
        ratingPhase: false,
      },
      expected: { phase: "loading", artifact: null, ratingPhase: false },
    },
    {
      label: "awaiting input",
      input: {
        artifactKind: "copy_editor",
        artifactStatus: "awaiting_input",
        artifactPayload: null,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "awaiting", artifact: null, ratingPhase: false },
    },
    {
      label: "completed schedule",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "completed",
        artifactPayload: scheduleArtifact,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "success", artifact: normalizedScheduleArtifact, ratingPhase: false },
    },
    {
      label: "failed without history",
      input: {
        artifactKind: "copy_editor",
        artifactStatus: "failed",
        artifactPayload: null,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "failure", artifact: null, ratingPhase: false },
    },
    {
      label: "awaiting preserves latest success",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "awaiting_input",
        artifactPayload: scheduleArtifact,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "awaiting", artifact: normalizedScheduleArtifact, ratingPhase: false },
    },
    {
      label: "failure preserves latest success",
      input: {
        artifactKind: "copy_editor",
        artifactStatus: "failed",
        artifactPayload: copyArtifact,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "failure", artifact: copyArtifact, ratingPhase: false },
    },
    {
      label: "loading preserves latest success",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "completed",
        artifactPayload: scheduleArtifact,
        isSubmitting: true,
        ratingPhase: false,
      },
      expected: { phase: "loading", artifact: normalizedScheduleArtifact, ratingPhase: false },
    },
    {
      label: "rating phase remains independent",
      input: {
        artifactKind: "copy_editor",
        artifactStatus: "completed",
        artifactPayload: copyArtifact,
        isSubmitting: false,
        ratingPhase: true,
      },
      expected: { phase: "success", artifact: copyArtifact, ratingPhase: true },
    },
    {
      label: "completed invalid payload fails safely",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "completed",
        artifactPayload: { columns: ["日期"], rows: [] },
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "failure", artifact: null, ratingPhase: false },
    },
    {
      label: "artifact kind mismatch fails safely",
      input: {
        artifactKind: "schedule_table",
        artifactStatus: "completed",
        artifactPayload: copyArtifact,
        isSubmitting: false,
        ratingPhase: false,
      },
      expected: { phase: "failure", artifact: null, ratingPhase: false },
    },
  ];
  executionCases.forEach(({ label, input, expected }) => {
    expectDeepEqual(
      `execution panel reducer ${label}`,
      deriveExecutionPanelState(input),
      expected,
    );
  });
  expectDeepEqual(
    "schedule artifact normalizes canonical rows for rendered columns",
    safeExecutionArtifact("schedule_table", scheduleArtifact),
    normalizedScheduleArtifact,
  );
  expectEqual(
    "login validation empty name",
    validateLoginFields({ name: "", phone: "13800138000" }),
    "请输入有效的姓名。",
  );
  expectEqual(
    "login validation empty phone",
    validateLoginFields({ name: "测试用户", phone: "" }),
    "请输入有效的中国大陆手机号码。",
  );
  const welcomeCases = [
    {
      label: "phone validation",
      error: new ApiError(422, [
        { loc: ["body", "phone"], msg: "Value error, raw phone validation" },
      ]),
      expected: "请输入有效的中国大陆手机号码。",
    },
    {
      label: "name validation",
      error: new ApiError(422, [
        { loc: ["body", "name"], msg: "Value error, raw name validation" },
      ]),
      expected: "请输入有效的姓名。",
    },
    {
      label: "recruitment closure",
      error: new ApiError(403, {
        code: "recruitment_closed",
        message: "Request failed: recruitment closed",
      }),
      expected: "正式实验招募暂未开放，请稍后再试。",
    },
    {
      label: "generic non-field API failure",
      error: new ApiError(400, [{ loc: ["body"], msg: "raw validation detail" }]),
      expected: "登录信息有误，请检查后重试。",
    },
    {
      label: "request timeout",
      error: new ApiError(408, "Request failed: 408"),
      expected: "请求超时，请稍后重试。",
    },
    {
      label: "server failure",
      error: new ApiError(503, "Request failed: 503"),
      expected: "服务暂时不可用，请稍后重试。",
    },
    {
      label: "network failure",
      error: new TypeError("Request failed: network"),
      expected: "网络连接失败，请检查网络后重试。",
    },
    {
      label: "aborted request",
      error: new DOMException("NotAllowedError raw detail", "AbortError"),
      expected: "请求超时，请稍后重试。",
    },
    {
      label: "unknown failure",
      error: new Error("raw stack content"),
      expected: "登录失败，请稍后重试。",
    },
  ];
  expectEqual(
    "API validation field mapping",
    welcomeCases[0].error.fieldErrors?.phone,
    "Value error, raw phone validation",
  );
  welcomeCases.forEach(({ label, error, expected }) => {
    const message = formatWelcomeLoginError(error);
    expectEqual(`welcome formatter ${label}`, message, expected);
    expectParticipantSafe(`welcome formatter ${label}`, message);
  });

  const gateMessages = {
    device: "请使用电脑参加正式实验。",
    viewport: "请将浏览器窗口宽度调整到至少 1024 像素。",
    browser: "请使用 Chrome 或 Edge 浏览器。",
    secureContext: "请通过 HTTPS 安全连接访问实验页面。",
    microphone: "未检测到浏览器录音能力，请检查浏览器和麦克风设备。",
    permission: "请在浏览器设置中允许麦克风权限后重试。",
  };
  const stateWithFailures = keys => ({
    statuses: keys.map(key => ({ key, passed: false })),
  });
  Object.entries(gateMessages).forEach(([key, expected]) => {
    const message = formatFormalEnvironmentFailure(stateWithFailures([key]), true);
    expectEqual(`environment formatter ${key}`, message, expected);
    expectParticipantSafe(`environment formatter ${key}`, message);
  });
  const recordingMessage = formatFormalEnvironmentFailure(stateWithFailures([]), false);
  expectEqual(
    "environment formatter recording",
    recordingMessage,
    "当前浏览器不支持录音，请使用最新版 Chrome 或 Edge。",
  );
  expectParticipantSafe("environment formatter recording", recordingMessage);

  const microphoneCases = [
    {
      label: "permission denial",
      outcome: "permission-denied",
      expected: "请在浏览器设置中允许麦克风权限后重试。",
    },
    {
      label: "missing device",
      outcome: "device-unavailable",
      expected: "未检测到可用麦克风，请连接设备后重试。",
    },
    {
      label: "dismissed prompt",
      outcome: "prompt-dismissed",
      expected: "麦克风授权未完成，请重新点击登录并在浏览器提示中允许访问。",
    },
    {
      label: "permission query unavailable",
      outcome: "permission-query-unavailable",
      expected: "当前浏览器无法检查麦克风权限，请使用最新版 Chrome 或 Edge 后重试。",
    },
    {
      label: "permission query failure",
      outcome: "permission-query-failed",
      expected: "浏览器未能检查麦克风权限，请刷新页面后重试。",
    },
    {
      label: "request failure",
      outcome: "request-failed",
      expected: "浏览器未能启用麦克风，请检查浏览器设置后重试。",
    },
  ];
  microphoneCases.forEach(({ label, outcome, expected }) => {
    const message = formatFormalEnvironmentFailure(
      stateWithFailures(["permission"]),
      true,
      outcome,
    );
    expectEqual(`environment formatter ${label}`, message, expected);
    expectParticipantSafe(`environment formatter ${label}`, message);
  });

  const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalNavigatorDescriptor = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  const originalMediaRecorderDescriptor = Object.getOwnPropertyDescriptor(
    globalThis,
    "MediaRecorder",
  );
  const originalConsoleInfo = console.info;
  const setGlobal = (key, value) => {
    Object.defineProperty(globalThis, key, {
      configurable: true,
      value,
    });
  };
  const restoreGlobal = (key, descriptor) => {
    if (descriptor) {
      Object.defineProperty(globalThis, key, descriptor);
    } else {
      delete globalThis[key];
    }
  };
  const runMicrophoneGateCase = async ({
    label,
    permissionQuery,
    getUserMedia,
    expected,
    expectedPassed = false,
  }) => {
    setGlobal("window", {
      innerWidth: 1280,
      isSecureContext: true,
    });
    setGlobal("navigator", {
      userAgent: "Mozilla/5.0 Chrome/126.0.0.0 Safari/537.36",
      permissions: { query: permissionQuery },
      mediaDevices: getUserMedia ? { getUserMedia } : undefined,
    });
    setGlobal("MediaRecorder", class MediaRecorder {});
    const result = await runFormalEnvironmentGate();
    expectEqual(`formal gate ${label}`, result.message, expected);
    expectEqual(`formal gate ${label} pass state`, result.passed, expectedPassed);
    expectParticipantSafe(`formal gate ${label}`, result.message ?? "");
  };

  console.info = () => {};
  try {
    await runMicrophoneGateCase({
      label: "permission denial",
      permissionQuery: async () => ({ state: "denied" }),
      getUserMedia: async () => {
        throw new DOMException("Permission denied", "NotAllowedError");
      },
      expected: "请在浏览器设置中允许麦克风权限后重试。",
    });
    await runMicrophoneGateCase({
      label: "missing device",
      permissionQuery: async () => ({ state: "prompt" }),
      getUserMedia: async () => {
        throw new DOMException("No microphone", "NotFoundError");
      },
      expected: "未检测到可用麦克风，请连接设备后重试。",
    });
    await runMicrophoneGateCase({
      label: "dismissed prompt",
      permissionQuery: async () => ({ state: "prompt" }),
      getUserMedia: async () => {
        throw new DOMException("Prompt dismissed", "NotAllowedError");
      },
      expected: "麦克风授权未完成，请重新点击登录并在浏览器提示中允许访问。",
    });
    await runMicrophoneGateCase({
      label: "permission query unavailable",
      permissionQuery: null,
      getUserMedia: async () => ({
        getTracks: () => [{ stop() {} }],
      }),
      expected: "当前浏览器无法检查麦克风权限，请使用最新版 Chrome 或 Edge 后重试。",
    });
    await runMicrophoneGateCase({
      label: "permission query failure",
      permissionQuery: async () => {
        throw new DOMException("Query failed", "NotAllowedError");
      },
      getUserMedia: async () => ({
        getTracks: () => [{ stop() {} }],
      }),
      expected: "浏览器未能检查麦克风权限，请刷新页面后重试。",
    });
    await runMicrophoneGateCase({
      label: "request failure",
      permissionQuery: async () => ({ state: "prompt" }),
      getUserMedia: async () => {
        throw new Error("Raw device failure");
      },
      expected: "浏览器未能启用麦克风，请检查浏览器设置后重试。",
    });
    await runMicrophoneGateCase({
      label: "capability failure",
      permissionQuery: async () => ({ state: "prompt" }),
      getUserMedia: null,
      expected: "未检测到浏览器录音能力，请检查浏览器和麦克风设备。",
    });
    await runMicrophoneGateCase({
      label: "granted pass",
      permissionQuery: async () => ({ state: "granted" }),
      getUserMedia: async () => ({
        getTracks: () => [{ stop() {} }],
      }),
      expected: null,
      expectedPassed: true,
    });
  } finally {
    console.info = originalConsoleInfo;
    restoreGlobal("window", originalWindowDescriptor);
    restoreGlobal("navigator", originalNavigatorDescriptor);
    restoreGlobal("MediaRecorder", originalMediaRecorderDescriptor);
  }

  const reconciliationCalls = [];
  const confirmedOpen = { status: "open", accepting_new_participants: true };
  const confirmedClosed = { status: "closed", accepting_new_participants: false };
  const lostResponseResult = await reconcileRecruitmentUpdate(true, {
    setRecruitment: async () => {
      reconciliationCalls.push("POST");
      throw new TypeError("response lost");
    },
    getRecruitmentStatus: async () => {
      reconciliationCalls.push("GET");
      return confirmedOpen;
    },
  });
  expectEqual("recruitment lost-response transport order", reconciliationCalls.join(","), "POST,GET");
  expectEqual("recruitment lost-response confirmed state", lostResponseResult.status, confirmedOpen);
  expectEqual("recruitment lost-response corrective error", lostResponseResult.errorMessage, null);

  reconciliationCalls.length = 0;
  const rejectedResult = await reconcileRecruitmentUpdate(true, {
    setRecruitment: async () => {
      reconciliationCalls.push("POST");
      throw new Error("rejected");
    },
    getRecruitmentStatus: async () => {
      reconciliationCalls.push("GET");
      return confirmedClosed;
    },
  });
  expectEqual("recruitment rejected transport order", reconciliationCalls.join(","), "POST,GET");
  expectEqual("recruitment rejected retained server state", rejectedResult.status, confirmedClosed);
  expectEqual(
    "recruitment rejected error",
    rejectedResult.errorMessage,
    "正式招募状态更新失败，请稍后重试。",
  );

  reconciliationCalls.length = 0;
  const inconsistentResult = await reconcileRecruitmentUpdate(true, {
    setRecruitment: async () => {
      reconciliationCalls.push("POST");
      return confirmedOpen;
    },
    getRecruitmentStatus: async () => {
      reconciliationCalls.push("GET");
      return confirmedClosed;
    },
  });
  expectEqual("recruitment inconsistent transport order", reconciliationCalls.join(","), "POST,GET");
  expectEqual("recruitment inconsistent authoritative state", inconsistentResult.status, confirmedClosed);
  expectEqual(
    "recruitment inconsistent error",
    inconsistentResult.errorMessage,
    "正式招募状态更新失败，请稍后重试。",
  );

  reconciliationCalls.length = 0;
  const unavailableResult = await reconcileRecruitmentUpdate(false, {
    setRecruitment: async () => {
      reconciliationCalls.push("POST");
      return confirmedClosed;
    },
    getRecruitmentStatus: async () => {
      reconciliationCalls.push("GET");
      throw new TypeError("refresh failed");
    },
  });
  expectEqual("recruitment unavailable transport order", reconciliationCalls.join(","), "POST,GET");
  expectEqual("recruitment unavailable state", unavailableResult.status, null);
  expectEqual(
    "recruitment unavailable error",
    unavailableResult.errorMessage,
    "暂时无法确认正式招募状态，请刷新后重试。",
  );
}

function expectCopyMatch(sourceLabel, sourceText, expected, label) {
  const pattern = new RegExp(escapeRegExp(expected));
  expectMatches(sourceLabel, sourceText, pattern, label);
}

function expectCopyFunctionMatch(sourceLabel, sourceText, functionName, expected, label) {
  const pattern = new RegExp(
    `${escapeRegExp(functionName)}\\s*\\([^)]*\\)\\s*\\{[\\s\\S]*?return\\s+\`${escapeRegExp(expected)}\`;`,
  );
  expectMatches(sourceLabel, sourceText, pattern, label);
}

function expectSourceBackedCopy({
  label,
  canonicalPath,
  canonicalSource,
  canonicalNeedle,
  copyNeedles,
}) {
  const needles = Array.isArray(copyNeedles) ? copyNeedles : [copyNeedles];
  expectCopyMatch(canonicalPath, canonicalSource, canonicalNeedle, `${label} canonical copy`);
  needles.forEach(needle => {
    expectCopyMatch(copyPath, copySource, needle, `${label} v2 copy`);
  });
}

const canonicalIndexPath = "../interface/public/index.html";
const canonicalMainPath = "../interface/public/js/main.js";
const canonicalConfigPath = "../interface/public/js/config.js";
const canonicalIndexSource = readCanonical("public/index.html");
const canonicalMainSource = readCanonical("public/js/main.js");
const canonicalConfigSource = readCanonical("public/js/config.js");

const copyPath = "src/experiment/interfaceCopy.ts";
const copySource = read(copyPath);
const appRouterPath = "src/app/routes.tsx";
const appRouterSource = read(appRouterPath);
const formalEnvironmentGatePath = "src/features/environment/formalEnvironmentGate.ts";
const formalEnvironmentGateSource = read(formalEnvironmentGatePath);
const desktopGatePath = "src/components/DesktopGate.tsx";
const desktopGateSource = read(desktopGatePath);
const adminAppPath = "src/features/admin/AdminApp.tsx";
const adminAppSource = readIfExists(adminAppPath, "React admin dashboard entry");
const adminShellPath = "src/features/admin/AdminShell.tsx";
const adminShellSource = readIfExists(adminShellPath, "React admin dashboard shell");
const adminTypesPath = "src/features/admin/adminTypes.ts";
const adminTypesSource = readIfExists(adminTypesPath, "React admin section definitions");
const apiTypesPath = "src/api/types.ts";
const apiTypesSource = readIfExists(apiTypesPath, "frontend API contracts");
const adminSystemPath = "src/features/admin/sections/SystemMonitorSection.tsx";
const adminSystemSource = readIfExists(adminSystemPath, "React admin system monitor section");
const adminDataPath = "src/features/admin/sections/DataMonitorSection.tsx";
const adminDataSource = readIfExists(adminDataPath, "React admin data monitor section");
const adminExportPath = "src/features/admin/sections/ExportSection.tsx";
const adminExportSource = readIfExists(adminExportPath, "React admin export section");
const adminControlPath = "src/features/admin/sections/ControlSection.tsx";
const adminControlSource = readIfExists(adminControlPath, "React admin assignment control section");
const executionPanelPath = "src/features/experiment/ExecutionPanel.tsx";
const executionPanelSource = readIfExists(executionPanelPath, "participant execution panel");
const experimentShellPath = "src/features/experiment/ExperimentShell.tsx";
const experimentShellSource = readIfExists(experimentShellPath, "participant experiment shell");
expectIncludes(executionPanelPath, executionPanelSource, 'kind === "copy_editor"', "copy editor kind rendering");
expectIncludes(executionPanelPath, executionPanelSource, 'kind === "schedule_table"', "schedule table kind rendering");
expectIncludes(executionPanelPath, executionPanelSource, "deriveExecutionPanelState", "execution state projection");
expectIncludes(executionPanelPath, executionPanelSource, "请完成本轮评分。", "visible independent rating phase");
expectNotIncludes(executionPanelPath, executionPanelSource, "topicKey", "internal topic key presentation dependency");
expectIncludes(experimentShellPath, experimentShellSource, 'session.presentation_mode === "execution"', "public execution presentation contract");
expectNotIncludes(experimentShellPath, experimentShellSource, "DecisionPanel", "decision-specific participant panel");
expectNotIncludes(experimentShellPath, experimentShellSource, "DECISION_ARTIFACT_TYPES", "decision artifact inference");
expectIncludes(experimentShellPath, experimentShellSource, "session.artifact_payload", "latest successful session artifact");
expectNotIncludes(experimentShellPath, experimentShellSource, "inferPresentationKind", "artifact presentation inference");
expectNotIncludes(experimentShellPath, experimentShellSource, "EXECUTION_ARTIFACT_TYPES", "execution artifact type inference");
expectNotIncludes(experimentShellPath, experimentShellSource, "payloadRecord", "execution payload shape inference");
expectNotIncludes(executionPanelPath, executionPanelSource, 'payload.candidates', "hidden copy candidate rendering");
expectMatches(apiTypesPath, apiTypesSource, /PresentationMode = "conversation" \| "execution";/, "exact presentation mode union");
expectMatches(apiTypesPath, apiTypesSource, /ArtifactKind = "schedule_table" \| "copy_editor" \| null;/, "exact artifact kind union");
expectMatches(apiTypesPath, apiTypesSource, /ArtifactStatus = "none" \| "awaiting_input" \| "completed" \| "failed";/, "exact artifact status union");
const globalStylesPath = "src/styles/global.css";
const globalStylesSource = readIfExists(globalStylesPath, "global participant styles");
expectMatches(
  globalStylesPath,
  globalStylesSource,
  /\.container\.execution-panel-active\s*\{[^}]*width:\s*min\(1360px,\s*100%\)/,
  "execution workspace overrides base container width",
);
expectCopyMatch(copyPath, copySource, `"测试入口"`, "test entry copy");
expectCopyMatch(copyPath, copySource, `"管理入口"`, "admin entry copy");
expectMatches(
  appRouterPath,
  appRouterSource,
  /session\?\.session_id === route\.sessionId[\s\S]*session\?\.is_test/,
  "test session route detection before participant restore",
);
expectNotIncludes(
  appRouterPath,
  appRouterSource,
  'const restorableParticipantRoutes = ["pretest", "experiment"].includes(route.name);',
  "unconditional participant restore for experiment routes",
);
expectIncludes(
  appRouterPath,
  appRouterSource,
  "restoreFinalizedPretestRoute",
  "finalized pretest canonical route recovery",
);
expectIncludes(
  appRouterPath,
  appRouterSource,
  "pretestGatePassed",
  "participant-state pretest route guard",
);
expectMatches(
  appRouterPath,
  appRouterSource,
  /participant\.participation_state === "completed"[\s\S]*participant\.current_day\.day_index > 1[\s\S]*participant\.pretest_status\.has_final/,
  "completed and post-Day-1 pretest route canonicalization",
);
expectMatches(
  appRouterPath,
  appRouterSource,
  /restoredSession\.status === "completed"[\s\S]*navigate\("\/complete", true\);[\s\S]*void apiClient\s*\.me\(\)/,
  "completed session immediate canonical route before best-effort participant refresh",
);
expectNotMatches(
  appRouterPath,
  appRouterSource,
  /restoredSession\.status === "completed"[\s\S]{0,240}await apiClient\.me\(\)/,
  "completed session route blocked on participant refresh",
);
expectIncludes(
  appRouterPath,
  appRouterSource,
  "formalExperimentEntryState",
  "formal experiment entry gate state",
);
expectMatches(
  appRouterPath,
  appRouterSource,
  /getSession\([\s\S]*runFormalEnvironmentGate\([\s\S]*startSession\(/,
  "formal experiment refresh revalidates before session resume",
);
expectIncludes(
  formalEnvironmentGatePath,
  formalEnvironmentGateSource,
  'key: "recording"',
  "recording capability gate",
);
expectIncludes(appRouterPath, appRouterSource, '{ name: "admin" }', "admin route match");
expectIncludes(appRouterPath, appRouterSource, '<AdminApp />', "admin app route render");
expectIncludes(adminAppPath, adminAppSource, "AdminShell", "admin shell composition");
expectIncludes(adminShellPath, adminShellSource, "ADMIN_SECTIONS", "admin shell uses section definitions");
expectIncludes(adminAppPath, adminAppSource, "registerUnsavedNavigationGuard", "admin app stores unsaved navigation guard");
expectIncludes(adminAppPath, adminAppSource, "requestSectionChange", "admin app confirms section changes");
expectIncludes(adminTypesPath, adminTypesSource, "系统监控", "admin sidebar system monitor");
expectIncludes(adminTypesPath, adminTypesSource, "数据监控", "admin sidebar data monitor");
expectIncludes(adminTypesPath, adminTypesSource, "数据导出", "admin sidebar export");
expectIncludes(adminTypesPath, adminTypesSource, "实验控制", "admin sidebar controls");
expectIncludes(adminTypesPath, adminTypesSource, "数据分析", "admin sidebar analysis placeholder");
expectIncludes(adminSystemPath, adminSystemSource, "全部累计", "provider usage all-time window");
expectIncludes(adminSystemPath, adminSystemSource, "最近 24 小时", "provider usage 24-hour window");
expectIncludes(adminSystemPath, adminSystemSource, "服务商与模型调用统计", "provider usage Chinese h2");
expectIncludes(apiTypesPath, apiTypesSource, "DeepSeekConfigurationView", "DeepSeek configuration API type");
expectIncludes(apiTypesPath, apiTypesSource, "DeepSeekTestResultView", "DeepSeek test result API type");
expectIncludes(apiTypesPath, apiTypesSource, "timeout_count: number", "provider timeout count API field");
expectIncludes(apiTypesPath, apiTypesSource, "deepseek_configuration: DeepSeekConfigurationView", "provider usage DeepSeek configuration");
expectIncludes(adminSystemPath, adminSystemSource, "usage.deepseek_configuration.status", "DeepSeek configuration status display");
expectIncludes(adminSystemPath, adminSystemSource, "测试 DeepSeek", "DeepSeek test command");
expectMatches(
  adminSystemPath,
  adminSystemSource,
  /disabled=\{isTestingDeepSeek\}/,
  "DeepSeek test loading lock",
);
expectIncludes(adminSystemPath, adminSystemSource, "testResult.provider", "DeepSeek test provider display");
expectIncludes(adminSystemPath, adminSystemSource, "testResult.model", "DeepSeek test model display");
expectIncludes(adminSystemPath, adminSystemSource, "testResult.latency_ms", "DeepSeek test latency display");
expectIncludes(adminSystemPath, adminSystemSource, "超时", "provider timeout column");
expectNotIncludes(adminSystemPath, adminSystemSource, "api_key", "DeepSeek API key field");
expectIncludes(adminDataPath, adminDataSource, "searchParticipants", "data monitor search loads participants separately");
expectIncludes(adminDataPath, adminDataSource, "请输入搜索条件后显示被试信息。", "data monitor hides participants before search");
expectIncludes(adminDataPath, adminDataSource, "admin-review-list", "data monitor stacks review tables");
expectIncludes(adminDataPath, adminDataSource, "reviewSessionColumns", "data monitor aligns incomplete and review columns");
expectIncludes(adminDataPath, adminDataSource, "未完成与需复核列表", "data monitor Chinese review heading");
expectIncludes(adminDataPath, adminDataSource, "包含 API/ASR 失败、缺少评分、系统标记异常或中途放弃的实验记录。", "data monitor review explanation");
expectIncludes(adminExportPath, adminExportSource, "开始日期", "export start date selector");
expectIncludes(adminExportPath, adminExportSource, "结束日期", "export end date selector");
expectIncludes(adminExportPath, adminExportSource, "filters.start_date", "export start date filter payload");
expectIncludes(adminExportPath, adminExportSource, "filters.end_date", "export end date filter payload");
expectIncludes(adminExportPath, adminExportSource, "deleteAdminExportJob", "export job delete action");
expectIncludes(adminExportPath, adminExportSource, "删除作业", "export job delete Chinese label");
expectIncludes(adminControlPath, adminControlSource, "完整无外源错误", "assignment clean count column");
expectIncludes(adminControlPath, adminControlSource, "已分配正在实验", "assignment active count column");
expectIncludes(adminControlPath, adminControlSource, "剩余分配", "assignment remaining allocation column");
expectIncludes(adminControlPath, adminControlSource, "saveAllChanges", "assignment save all dirty changes");
expectIncludes(adminControlPath, adminControlSource, "beforeunload", "assignment browser close dirty prompt");
expectIncludes(adminControlPath, adminControlSource, "实验控制有未保存修改", "assignment unsaved changes prompt copy");
expectIncludes(adminControlPath, adminControlSource, "正式招募", "recruitment control heading");
expectIncludes(adminControlPath, adminControlSource, "recruitmentOpenDraft", "recruitment draft state");
expectIncludes(adminControlPath, adminControlSource, "saveRecruitment", "recruitment save action");
expectIncludes(adminControlPath, adminControlSource, ">开放<", "open status label");
expectIncludes(adminControlPath, adminControlSource, ">暂停<", "paused status label");
expectNotIncludes(adminControlPath, adminControlSource, "需确认", "removed confirmation badge");
expectNotIncludes(adminControlPath, adminControlSource, "确认开放正式招募", "removed open dialog");
expectNotIncludes(adminControlPath, adminControlSource, "确认关闭正式招募", "removed close dialog");
expectNotIncludes(adminControlPath, adminControlSource, "pause_new_participants", "retired pause flag");
expectNotIncludes(adminControlPath, adminControlSource, "test_channel_only", "retired test-only flag");
expectNotIncludes(apiTypesPath, apiTypesSource, "global_controls", "retired assignment global controls contract");
expectNotIncludes(apiTypesPath, apiTypesSource, "pause_new_participants", "retired assignment pause contract");
expectNotIncludes(apiTypesPath, apiTypesSource, "test_channel_only", "retired assignment test-only contract");
expectNotIncludes(apiTypesPath, apiTypesSource, "test_channel_provider", "retired test-channel provider contract");
expectIncludes(adminControlPath, adminControlSource, "getRecruitmentStatus", "recruitment status loading");
expectIncludes(adminControlPath, adminControlSource, "setAdminRecruitment", "audited recruitment update");
expectIncludes(adminControlPath, adminControlSource, "reconcileRecruitmentUpdate", "server-confirmed recruitment reconciliation");
expectMatches(
  adminControlPath,
  adminControlSource,
  /setRecruitment\(null\);[\s\S]{0,400}getRecruitmentStatus/,
  "refresh marks recruitment unconfirmed before transport",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /recruitment === null\s*\?[\s\S]{0,160}\{isLoading \? "加载中" : "状态不可用"\}/,
  "recruitment null loading and unavailable status",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /onChange=\{\(event\) => \{\s*const nextOpen = event\.target\.checked;\s*setRecruitmentOpenDraft\(nextOpen\);\s*setRecruitmentDraftDirty\(\s*recruitment === null \|\|\s*nextOpen !== recruitment\.accepting_new_participants,?\s*\);\s*\}\}/,
  "recruitment switch marks away dirty and confirmed value clean",
);
expectNotMatches(
  adminControlPath,
  adminControlSource,
  /onChange=\{[^}]{0,240}(?:saveRecruitment|reconcileRecruitmentUpdate|setAdminRecruitment)/,
  "recruitment switch API mutation",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /onClick=\{\(\) => void saveRecruitment\(\)\}[\s\S]{0,160}disabled=\{controlsLocked \|\| !recruitmentChanged\}/,
  "recruitment panel save button disabled for clean draft",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /reconcileRecruitmentUpdate\([\s\S]{0,360}setRecruitment\(result\.status\)[\s\S]{0,160}setErrorMessage\(result\.errorMessage\)/,
  "recruitment update uses reconciled server state",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /disabled=\{controlsLocked \|\| recruitment === null\}/,
  "recruitment switch locked until server confirmation",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /if \(recruitmentChanged && !await saveRecruitment\(\)\) return false;[\s\S]{0,160}if \(dirtyCells\.length > 0 && !await saveAllChanges\(\)\) return false;/,
  "recruitment saves before assignment cells on navigation",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /if \(isSaving \|\| isRecruitmentSaving\) \{\s*registerUnsavedNavigationGuard\(async \(\) => false\)/,
  "recruitment update navigation guard",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /const hasUnsavedChanges = recruitmentChanged \|\| dirtyCells\.length > 0;[\s\S]{0,8000}if \(!hasUnsavedChanges\) \{\s*registerUnsavedNavigationGuard\(null\)/,
  "clean recruitment draft clears navigation guard",
);
expectMatches(
  adminControlPath,
  adminControlSource,
  /if \(!hasUnsavedChanges\) \{\s*return undefined;[\s\S]{0,260}addEventListener\("beforeunload"/,
  "clean recruitment draft clears beforeunload guard",
);
expectSourceBackedCopy({
  label: "login title",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "人机信任实验",
  copyNeedles: `"人机信任实验"`,
});
expectSourceBackedCopy({
  label: "login subtitle",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "Human-AI Trust Experiment",
  copyNeedles: `"Human-AI Trust Experiment"`,
});
expectSourceBackedCopy({
  label: "login name placeholder",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "请输入您的姓名",
  copyNeedles: `"请输入您的姓名"`,
});
expectSourceBackedCopy({
  label: "login phone placeholder",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "请填写支付宝绑定的手机号",
  copyNeedles: `"请填写支付宝绑定的手机号"`,
});
expectSourceBackedCopy({
  label: "login submit label",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "登录",
  copyNeedles: `"登录"`,
});
expectSourceBackedCopy({
  label: "login footer",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "浙江大学 · 人机交互研究",
  copyNeedles: `"浙江大学 · 人机交互研究"`,
});
expectSourceBackedCopy({
  label: "welcome title",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "欢迎参加实验",
  copyNeedles: `"欢迎参加实验"`,
});
expectSourceBackedCopy({
  label: "welcome base message",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "在这个实验中，您将使用语音和AI助手进行对话，您的反馈将帮助我们更好的评估、改进AI助手。",
  copyNeedles: [
    "message(name: string) {",
    "在这个实验中，您将使用语音和AI助手进行对话，您的反馈将帮助我们更好的评估、改进AI助手。",
  ],
});
expectSourceBackedCopy({
  label: "welcome long-term message",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "实验<strong>需要连续3天参与</strong>，这是您<strong>第${plannedDay}天</strong>参与实验。",
  copyNeedles: [
    "longTermMessage(dayIndex: number) {",
    "实验<strong>需要连续3天参与</strong>，这是您<strong>第${dayIndex}天</strong>参与实验。",
  ],
});
expectSourceBackedCopy({
  label: "blocked missed long-term message",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您未按要求连续三天完成实验，已无法参与实验。",
  copyNeedles: `"您未按要求连续三天完成实验，已无法参与实验。"`,
});
expectSourceBackedCopy({
  label: "pretest title",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "前测问卷",
  copyNeedles: `"前测问卷"`,
});
expectSourceBackedCopy({
  label: "pretest intro",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "在进入正式实验之前，请您先完成一份前测问卷，问卷填写时间大约为8-10分钟。",
  copyNeedles: `"在进入正式实验之前，请您先完成一份前测问卷，问卷填写时间大约为8-10分钟。"`,
});
expectSourceBackedCopy({
  label: "pretest fill action",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "填写问卷",
  copyNeedles: `"填写问卷"`,
});
expectSourceBackedCopy({
  label: "pretest birth date",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "您的出生年月",
  copyNeedles: `"您的出生年月"`,
});
expectSourceBackedCopy({
  label: "pretest gender",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "您的性别",
  copyNeedles: `"您的性别"`,
});
expectSourceBackedCopy({
  label: "pretest male",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "男",
  copyNeedles: `"男"`,
});
expectSourceBackedCopy({
  label: "pretest female",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "女",
  copyNeedles: `"女"`,
});
expectSourceBackedCopy({
  label: "pretest id number",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "您的身份证/护照",
  copyNeedles: `"您的身份证/护照"`,
});
expectSourceBackedCopy({
  label: "pretest id note",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "注:仅用于被试费报销，不会用于任何其他用途）",
  copyNeedles: ["idNumberNote: \"注:仅用于被试费报销，不会用于任何其他用途）\"", "idNumberNote"],
});
expectSourceBackedCopy({
  label: "pretest next",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "下一页",
  copyNeedles: `"下一页"`,
});
expectSourceBackedCopy({
  label: "pretest enter experiment",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "进入实验",
  copyNeedles: `"进入实验"`,
});
expectSourceBackedCopy({
  label: "pretest save success",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "前测问卷已成功保存。点击下方按钮进入正式实验",
  copyNeedles: `"前测问卷已成功保存。点击下方按钮进入正式实验"`,
});
expectSourceBackedCopy({
  label: "rating title",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "请基于本次对话进行评价",
  copyNeedles: `"请基于本次对话进行评价"`,
});
expectSourceBackedCopy({
  label: "rating stance question",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "我对这个AI的助手的印象（1-更像工具；5-更像人）：",
  copyNeedles: `"我对这个AI的助手的印象（1-更像工具；5-更像人）："` ,
});
expectSourceBackedCopy({
  label: "rating trust question",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "我能够信任这个AI助手（1-完全无法信任；7-完全可以信任）：",
  copyNeedles: `"我能够信任这个AI助手（1-完全无法信任；7-完全可以信任）："` ,
});
expectSourceBackedCopy({
  label: "rating submit",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "提交评价",
  copyNeedles: `"提交评价"`,
});
expectSourceBackedCopy({
  label: "completion title",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "实验完成",
  copyNeedles: `"实验完成"`,
});
expectSourceBackedCopy({
  label: "save label",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "保存数据",
  copyNeedles: `"保存数据"`,
});
expectSourceBackedCopy({
  label: "download label",
  canonicalPath: canonicalIndexPath,
  canonicalSource: canonicalIndexSource,
  canonicalNeedle: "下载数据",
  copyNeedles: `"下载数据"`,
});
expectSourceBackedCopy({
  label: "short saved",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成实验，数据保存成功。感谢您的支持和参与！被试费将在后续统一发放。",
  copyNeedles: `"您已完成实验，数据保存成功。感谢您的支持和参与！被试费将在后续统一发放。"`,
});
expectSourceBackedCopy({
  label: "short retry",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成实验，感谢您的支持和参与！后台查询到数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您已完成实验，感谢您的支持和参与！后台查询到数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "short failed",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成实验，感谢您的支持和参与！数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您已完成实验，感谢您的支持和参与！数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "long final saved",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成全部实验，数据保存成功。感谢您的支持和参与！被试费将在后续统一发放。",
  copyNeedles: `"您已完成全部实验，数据保存成功。感谢您的支持和参与！被试费将在后续统一发放。"`,
});
expectSourceBackedCopy({
  label: "long final retry",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您完成全部实验，感谢您的支持和参与！后台查询到数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您完成全部实验，感谢您的支持和参与！后台查询到数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "long final failed",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您完成全部实验，感谢您的支持和参与！数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您完成全部实验，感谢您的支持和参与！数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "long non-final saved",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成今天的实验，数据保存成功！<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。",
  copyNeedles: `"您已完成今天的实验，数据保存成功！<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。"`,
});
expectSourceBackedCopy({
  label: "long non-final retry",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成今天的实验，<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。后台查询到数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您已完成今天的实验，<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。后台查询到数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "long non-final failed",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成今天的实验，<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。数据未完整保存，请您点击保存数据进行保存。",
  copyNeedles: `"您已完成今天的实验，<strong>实验需要连续三天进行，请您明天继续登录该网站进行实验</strong>。数据未完整保存，请您点击保存数据进行保存。"`,
});
expectSourceBackedCopy({
  label: "download fallback",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "后台查询到数据未完整保存，请您点击下载数据后，将数据直接发送至邮箱${email}，无需修改文件名。",
  copyNeedles: [
    "downloadFallback(email: string) {",
    "后台查询到数据未完整保存，请您点击下载数据后，将数据直接发送至邮箱${email}，无需修改文件名。",
  ],
});
expectSourceBackedCopy({
  label: "test fallback",
  canonicalPath: canonicalMainPath,
  canonicalSource: canonicalMainSource,
  canonicalNeedle: "您已完成实验，感谢您的支持和参与！",
  copyNeedles: `"您已完成实验，感谢您的支持和参与！"`,
});

const routesPath = "src/app/routes.tsx";
const routesSource = read(routesPath);
expectNotIncludes(routesPath, routesSource, "/environment-check", "formal participant environment-check route");
expectNotIncludes(routesPath, routesSource, "EnvironmentCheckPage", "formal participant environment-check component import");
expectNotIncludes(routesPath, routesSource, "environmentReturnPath", "environment-check return path state");
expectIncludes(routesPath, routesSource, "runFormalEnvironmentGate", "embedded login/start gate");

const loginPath = "src/features/login/LoginForm.tsx";
const loginSource = read(loginPath);
const welcomeErrorPath = "src/features/login/welcomeErrorMessages.ts";
const welcomeErrorSource = readIfExists(welcomeErrorPath, "welcome error localization");
expectIncludes(loginPath, loginSource, "onBeforeLogin", "pre-login environment gate callback");
expectIncludes(loginPath, loginSource, "测试入口", "test entry button");
expectIncludes(loginPath, loginSource, "管理入口", "admin entry button");
expectIncludes(loginPath, loginSource, "console.info", "login environment diagnostic info");
expectIncludes(loginPath, loginSource, "participation_message", "login blocked reason message");
expectIncludes(loginPath, loginSource, "validateLoginFields", "application-owned login validation");
expectIncludes(loginPath, loginSource, "noValidate", "native login validation disabled");
expectNotMatches(loginPath, loginSource, /\srequired(?:\s|\n|\/?>)/, "native required login validation");
expectEqual(
  "login required-field accessibility semantics",
  [...loginSource.matchAll(/aria-required="true"/g)].length,
  2,
);
expectIncludes(welcomeErrorPath, welcomeErrorSource, "请输入有效的中国大陆手机号码。", "Chinese phone error");
expectIncludes(welcomeErrorPath, welcomeErrorSource, "正式实验招募暂未开放，请稍后再试。", "Chinese recruitment error");
expectIncludes(welcomeErrorPath, welcomeErrorSource, "网络连接失败，请检查网络后重试。", "Chinese network error");
expectNotIncludes(loginPath, loginSource, "error.detail", "raw API detail on welcome page");
expectNotIncludes(formalEnvironmentGatePath, formalEnvironmentGateSource, "error.message", "raw environment exception message");
expectIncludes(formalEnvironmentGatePath, formalEnvironmentGateSource, "formatFormalEnvironmentFailure", "central environment failure formatter");
expectIncludes(desktopGatePath, desktopGateSource, "桌面设备", "Chinese desktop device label");
expectIncludes(desktopGatePath, desktopGateSource, "手机", "Chinese mobile device label");
expectIncludes(desktopGatePath, desktopGateSource, "平板设备", "Chinese tablet device label");
expectNotIncludes(desktopGatePath, desktopGateSource, "${deviceType}", "raw device-type enum in participant copy");
expectMatches(
  loginPath,
  loginSource,
  /participant\.participation_state === "blocked"[\s\S]*participant\.participation_state === "not_scheduled_today"[\s\S]*setErrorMessage\([\s\S]*participation_message[\s\S]*return;[\s\S]*onSuccess\(participant\)/,
  "login stops blocked participants before welcome page",
);

const pretestPath = "src/features/pretest/PretestForm.tsx";
const pretestSource = read(pretestPath);
expectIncludes(pretestPath, pretestSource, "interfaceCopy.pretest", "pretest copy constants");
expectIncludes(pretestPath, pretestSource, "onEnterExperiment", "save-page enter experiment callback");
expectIncludes(pretestPath, pretestSource, "isEnteringExperiment", "save-page enter experiment in-flight state");
expectMatches(
  pretestPath,
  pretestSource,
  /disabled=\{isSaving \|\| isEnteringExperiment \|\| !finalResponse\}/,
  "save-page enter experiment button disables while entering",
);
expectNotIncludes(pretestPath, pretestSource, "onSubmitted", "removed duplicate pretest submitted callback");
expectIncludes(pretestPath, pretestSource, "apiClient.getCurrentPretest", "pretest draft restore request");
expectIncludes(pretestPath, pretestSource, "AUTOSAVE_DEBOUNCE_MS", "pretest autosave debounce");
expectIncludes(pretestPath, pretestSource, "acknowledgedPayloadRef", "pretest acknowledged draft tracking");
expectIncludes(pretestPath, pretestSource, "saveQueueRef", "pretest serialized save queue");
expectIncludes(pretestPath, pretestSource, "nextSaveVersionRef", "pretest save snapshot versions");
expectIncludes(
  pretestPath,
  pretestSource,
  "latestAcknowledgedVersionRef",
  "pretest ordered acknowledgement guard",
);
expectNotIncludes(pretestPath, pretestSource, "inFlightSaveRef", "single-slot autosave race");
expectMatches(
  pretestPath,
  pretestSource,
  /const previousSave = saveQueueRef\.current;[\s\S]*previousSave[\s\S]*\.catch\(\(\) => undefined\)[\s\S]*\.then/,
  "pretest saves chained behind the complete queue tail",
);
expectMatches(
  pretestPath,
  pretestSource,
  /saveVersion >= latestAcknowledgedVersionRef\.current[\s\S]*latestAcknowledgedVersionRef\.current = saveVersion[\s\S]*acknowledgedPayloadRef\.current/,
  "pretest acknowledgements cannot regress",
);
expectIncludes(pretestPath, pretestSource, "flushPendingDraft", "pretest final save flush");
expectIncludes(pretestPath, pretestSource, "error.fieldErrors", "pretest field-level server errors");
expectMatches(
  pretestPath,
  pretestSource,
  /catch \(error\) \{[\s\S]*setAutosaveMessage\([\s\S]*\}[\s\S]*finally/,
  "pretest autosave failure remains visible without replacing local draft",
);

const routesPretestSource = read("src/app/routes.tsx");
expectIncludes(
  "src/app/routes.tsx",
  routesPretestSource,
  "startFormalSession",
  "pretest-to-formal-session transition",
);
expectMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /handlePretestEnterExperiment[\s\S]*startFormalSession/,
  "post-pretest direct formal start handler",
);
expectMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /const handlePretestEnterExperiment = async \(\) => \{[\s\S]*const nextParticipant = await apiClient\.me\(\);[\s\S]*await startFormalSession\(\)/,
  "ordered post-pretest enter experiment handler",
);
expectIncludes(
  "src/app/routes.tsx",
  routesPretestSource,
  "isEnteringFormalSession",
  "post-pretest formal-session in-flight guard state",
);
expectMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /if \(isEnteringFormalSession\) \{[\s\S]*return;/,
  "post-pretest formal-session duplicate guard",
);
expectIncludes(
  "src/app/routes.tsx",
  routesPretestSource,
  "enterExperimentError",
  "post-pretest enter experiment error state",
);
expectMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /onComplete=\{async \(\) => \{[\s\S]*try \{[\s\S]*await apiClient\.me\(\)[\s\S]*catch[\s\S]*console\.info[\s\S]*finally \{[\s\S]*navigate\("\/complete"\)/,
  "formal completion participant-refresh failure still navigates to complete page",
);
expectNotMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /handlePretestSubmitted/,
  "removed duplicate pretest submitted handler",
);
expectMatches(
  "src/app/routes.tsx",
  routesPretestSource,
  /<PretestPage[\s\S]*onEnterExperiment=\{handlePretestEnterExperiment\}[\s\S]*\/>/,
  "pretest page promise-returning wiring",
);

const completePath = "src/pages/CompletePage.tsx";
const completeSource = read(completePath);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.shortSaved",
  "short saved completion message",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.longNonFinalSaved",
  "long non-final saved completion message",
);
expectIncludes(
  completePath,
  completeSource,
  "dangerouslySetInnerHTML",
  "completion strong markup support",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.saveButton",
  "completion save button copy",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.downloadButton",
  "completion download button copy",
);
expectIncludes(
  completePath,
  completeSource,
  "completion-actions",
  "completion actions layout",
);
expectIncludes(
  completePath,
  completeSource,
  "CompletionViewMode",
  "explicit completion view modes",
);
expectIncludes(
  completePath,
  completeSource,
  "buildCompletionViewState",
  "explicit completion view state builder",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.shortRetryRequired",
  "short retry-required completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.shortRetryFailed",
  "short retry-failed completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.longFinalRetryRequired",
  "long-final retry-required completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.longFinalRetryFailed",
  "long-final retry-failed completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.longNonFinalRetryRequired",
  "long-non-final retry-required completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.longNonFinalRetryFailed",
  "long-non-final retry-failed completion state",
);
expectIncludes(
  completePath,
  completeSource,
  "interfaceCopy.completion.downloadFallback",
  "download fallback completion state",
);
expectMatches(
  completePath,
  completeSource,
  /showSave:[\s\S]*effectiveMode === "retryRequired" \|\| effectiveMode === "retryFailed"/,
  "completion save action visible for retry states",
);
expectMatches(
  completePath,
  completeSource,
  /showDownload:[\s\S]*effectiveMode === "downloadFallback" \|\| effectiveMode === "test"/,
  "completion download action visible for fallback states",
);
expectNotIncludes(completePath, completeSource, "返回入口", "non-interface completion return button");
expectNotIncludes(completePath, completeSource, "onRestart", "removed completion restart prop");

const welcomePath = "src/pages/WelcomePage.tsx";
const welcomeSource = read(welcomePath);
expectIncludes(welcomePath, welcomeSource, "interfaceCopy.welcome.title", "welcome title copy constant");
expectIncludes(welcomePath, welcomeSource, "interfaceCopy.welcome.message", "welcome base message copy constant");
expectIncludes(welcomePath, welcomeSource, "interfaceCopy.welcome.longTermMessage", "welcome long-term copy constant");
expectIncludes(welcomePath, welcomeSource, "dangerouslySetInnerHTML", "welcome strong markup support");

const experimentPagePath = "src/pages/ExperimentPage.tsx";
const experimentPageSource = read(experimentPagePath);
expectNotIncludes(experimentPagePath, experimentPageSource, "DesktopGate", "formal experiment inline gate");
expectNotIncludes(experimentPagePath, experimentPageSource, "Formal Gate", "formal gate copy");
expectNotIncludes(
  experimentPagePath,
  experimentPageSource,
  "app-shell experiment-app-shell",
  "dashboard app shell",
);
expectNotIncludes(experimentPagePath, experimentPageSource, "page-stack", "dashboard page stack");

const shellPath = "src/features/experiment/ExperimentShell.tsx";
const shellSource = read(shellPath);
expectIncludes(shellPath, shellSource, "className={`container", "original container class");
expectIncludes(shellPath, shellSource, "topic-fixed", "topic fixed region");
expectIncludes(shellPath, shellSource, "chat-workspace", "chat workspace region");
expectIncludes(shellPath, shellSource, "chat-main", "chat main region");
expectIncludes(shellPath, shellSource, "tool-action-panel", "tool action panel region");
expectNotIncludes(shellPath, shellSource, "formal-experiment-layout", "old formal experiment layout");
expectNotIncludes(shellPath, shellSource, "complete-panel", "dashboard completion panel");
expectNotIncludes(
  shellPath,
  shellSource,
  "canCompleteSession",
  "separate rated-turn completion gate",
);
expectNotIncludes(
  shellPath,
  shellSource,
  "completion-inline",
  "separate completion action",
);
expectIncludes(
  shellPath,
  shellSource,
  '"status" in result',
  "fifth-rating completed session response",
);
expectIncludes(
  shellPath,
  shellSource,
  "apiClient.completeSession(session.session_id)",
  "lost fifth-rating response recovery",
);
expectIncludes(
  shellPath,
  shellSource,
  "setSubmitError",
  "completion failure visible error",
);
expectMatches(
  shellPath,
  shellSource,
  /onComplete: \(\) => Promise<void>/,
  "awaitable formal completion callback",
);
expectIncludes(
  shellPath,
  shellSource,
  "await onComplete()",
  "formal completion waits for completion navigation callback",
);
expectMatches(
  shellPath,
  shellSource,
  /session\.is_test[\s\S]*text-input-area[\s\S]*<TestVoiceTurnInput/,
  "active test branch renders text and voice inputs",
);
expectIncludes(
  shellPath,
  shellSource,
  'import { TestVoiceTurnInput } from "../test/TestVoiceTurnInput"',
  "active test voice input import",
);

const transcriptPath = "src/features/experiment/ChatTranscript.tsx";
const transcriptSource = read(transcriptPath);
expectIncludes(transcriptPath, transcriptSource, "chat-area", "original chat area class");
expectIncludes(
  transcriptPath,
  transcriptSource,
  "interfaceCopy.experiment.initialAssistantMessage",
  "initial assistant copy constant",
);

const domainPath = "backend/app/models/domain.py";
const domainSource = readProject(domainPath);
[
  "假定你在日常生活中遇到一些困惑",
  "今年我准备考研，希望你可以给我制定一个复习计划并督促我",
  "请把这段杂乱安排整理成",
  "我想发一条朋友圈，内容是今天工作结束后去散步",
].forEach(needle => {
  expectIncludes(canonicalConfigPath, canonicalConfigSource, needle, "canonical topic text");
  expectIncludes(domainPath, domainSource, needle, "v2 topic text");
});

const scenarioRegistryPath = "backend/app/scenarios/registry.py";
const scenarioRegistrySource = readProject(scenarioRegistryPath);
expectIncludes(
  scenarioRegistryPath,
  scenarioRegistrySource,
  "def require(self, *, condition: str, subcondition: str, topic_key: str)",
  "strict topic-level scenario lookup",
);
expectNotIncludes(
  scenarioRegistryPath,
  scenarioRegistrySource,
  "model_copy(",
  "active topic contract cloning",
);

const scenarioFiles = ["qa", "planning", "chat", "decision", "execution"];
const activeScenarios = scenarioFiles.flatMap(name => {
  const relativePath = `backend/app/scenarios/${name}.yaml`;
  try {
    const parsed = JSON.parse(readProject(relativePath));
    if (!Array.isArray(parsed)) {
      failures.push(`${relativePath}: expected a scenario array`);
      return [];
    }
    return parsed;
  } catch (error) {
    failures.push(`${relativePath}: invalid JSON-subset YAML: ${error.message}`);
    return [];
  }
});

const expectedScenarioContracts = {
  weather: { tool: "open_meteo", artifact: "weather_card" },
  physics: { tool: null, artifact: null },
  travelPlan: { tool: null, artifact: "plan_card" },
  hiking: { tool: null, artifact: "plan_card" },
  news: { tool: null, artifact: null },
  tech: { tool: null, artifact: null },
  valueDecision: { tool: null, artifact: null },
  taskExecution: { tool: null, artifact: "table" },
  advice: { tool: null, artifact: null },
  goalPlan: { tool: null, artifact: "plan_card" },
  funStory: { tool: null, artifact: null },
  preferenceDecision: { tool: null, artifact: null },
  collaborativeExecution: { tool: null, artifact: "copy_versions" },
};

expectEqual("active scenario contract count", activeScenarios.length, 13);
expectEqual(
  "active scenario topics",
  [...activeScenarios.map(scenario => scenario.topic_key)].sort().join(","),
  Object.keys(expectedScenarioContracts).sort().join(","),
);
expectEqual(
  "globally unique active scenario ids",
  new Set(activeScenarios.map(scenario => scenario.scenario_id)).size,
  13,
);

activeScenarios.forEach(scenario => {
  const expected = expectedScenarioContracts[scenario.topic_key];
  if (!expected) return;
  expectEqual(
    `${scenario.topic_key} primary tool`,
    scenario.tools?.allowed?.[0] ?? null,
    expected.tool,
  );
  expectEqual(
    `${scenario.topic_key} artifact`,
    scenario.artifact?.artifact_type ?? null,
    expected.artifact,
  );
  if (!scenario.required_context?.length || !scenario.clarification?.response_goal) {
    failures.push(`${scenario.topic_key}: missing required context or clarification policy`);
  }
  if (!scenario.fixtures?.normal || !scenario.fixtures?.clarification) {
    failures.push(`${scenario.topic_key}: missing normal or clarification fixture`);
  }
});

[["weather", "physics"], ["travelPlan", "hiking"], ["news", "tech"]].forEach(
  ([firstTopic, secondTopic]) => {
    const first = activeScenarios.find(scenario => scenario.topic_key === firstTopic);
    const second = activeScenarios.find(scenario => scenario.topic_key === secondTopic);
    if (!first || !second) return;
    if (first.scenario_id === second.scenario_id || first.system_prompt === second.system_prompt) {
      failures.push(`${firstTopic}/${secondTopic}: collapsed scenario contract`);
    }
  },
);

const taskCardPath = "src/features/experiment/TaskCard.tsx";
const taskCardSource = read(taskCardPath);
expectIncludes(taskCardPath, taskCardSource, "task-card-topic", "interface topic text region");
expectIncludes(taskCardPath, taskCardSource, "task-card-instruction", "interface topic instruction");

const voicePath = "src/features/experiment/VoiceTurnInput.tsx";
const voiceSource = read(voicePath);
expectIncludes(voicePath, voiceSource, "voice-input-area", "original voice input area class");
expectIncludes(voicePath, voiceSource, "interfaceCopy.experiment.voiceHint", "voice hint copy constant");
expectIncludes(
  voicePath,
  voiceSource,
  "interfaceCopy.experiment.send",
  "voice send button copy constant",
);
expectNotIncludes(voicePath, voiceSource, '"提交语音轮次"', "hard-coded voice submit copy");
expectNotIncludes(voicePath, voiceSource, "interfaceCopy.experiment.submitVoiceTurn", "old voice submit copy key");

const recorderPath = "src/components/VoiceRecorder.tsx";
const recorderSource = read(recorderPath);
expectNotIncludes(recorderPath, recorderSource, "recorder-status-card", "formal recorder status card");
expectIncludes(recorderPath, recorderSource, "transcription-alert", "failed transcription alert");
expectIncludes(recorderPath, recorderSource, "asrOperationIdRef", "stable ASR operation id");
expectIncludes(recorderPath, recorderSource, "audioBlobRef", "retryable recorded audio blob");
expectIncludes(recorderPath, recorderSource, "retryTranscription", "same-recording ASR retry action");
expectIncludes(recorderPath, recorderSource, "maxDurationSeconds", "configured recording duration limit");
expectIncludes(recorderPath, recorderSource, "remainingSeconds", "independent recording countdown state");
expectIncludes(recorderPath, recorderSource, "setInterval", "recording countdown timer");
expectIncludes(recorderPath, recorderSource, "stopRecordingRef", "maximum-duration automatic stop");
expectIncludes(recorderPath, recorderSource, 'className="recording-countdown"', "visible recording countdown");
expectMatches(
  recorderPath,
  recorderSource,
  /apiClient\s*\.getRuntimeConfig\(\)/,
  "backend runtime recording limit",
);
expectNotIncludes(recorderPath, recorderSource, "Math.min(\n      maxDurationSeconds", "clamped reported recording duration");
expectNotIncludes(voicePath, voiceSource, "VITE_ASR_MAX_DURATION_SECONDS", "build-time recording duration drift");
expectIncludes(voicePath, voiceSource, "turnOperationIdRef", "stable formal turn operation id");
expectIncludes(voicePath, voiceSource, "operationId", "formal turn operation id handoff");
expectIncludes(voicePath, voiceSource, "requiresNewOperationId", "formal failed-operation key rotation");
expectIncludes(voicePath, voiceSource, "turnIndex", "formal turn operation scope");
expectIncludes(shellPath, shellSource, "testTextOperationIdRef", "stable shell test-text operation id");
expectIncludes(shellPath, shellSource, "requiresNewOperationId", "shell test-text failed-operation key rotation");
expectIncludes(shellPath, shellSource, "pendingUserMessage", "browser-only pending user message");
expectIncludes(shellPath, shellSource, "submitPendingTurn", "shared text and voice submit lifecycle");
expectIncludes(shellPath, shellSource, "setTestText(trimmed)", "failed text submit restoration");
expectNotIncludes(shellPath, shellSource, "OperationRefreshError", "refresh failure resend path");
expectNotIncludes(copyPath, copySource, '"提交语音轮次"', "old voice submit copy");

const testVoicePath = "src/features/test/TestVoiceTurnInput.tsx";
const testVoiceSource = readIfExists(testVoicePath, "active test voice input");
expectIncludes(testVoicePath, testVoiceSource, "getDesktopGateState", "fresh microphone state check");
expectIncludes(testVoicePath, testVoiceSource, "requestMicrophoneAccess", "microphone permission retry");
expectIncludes(testVoicePath, testVoiceSource, 'typeof MediaRecorder !== "undefined"', "MediaRecorder capability check");
expectIncludes(testVoicePath, testVoiceSource, "<VoiceTurnInput", "shared voice-turn input");
expectIncludes(testVoicePath, testVoiceSource, "请求麦克风权限", "microphone permission action");
expectIncludes(testVoicePath, testVoiceSource, "重新检查", "microphone recheck action");
expectIncludes(testVoicePath, testVoiceSource, "文本输入仍可继续使用。", "non-blocking test voice failure copy");
expectMatches(
  testVoicePath,
  testVoiceSource,
  /useEffect\(\(\) => \{[\s\S]*refreshMicrophoneState/,
  "microphone state refresh on mount",
);

const testChannelPath = "src/pages/TestChannelPage.tsx";
const testChannelSource = read(testChannelPath);
expectIncludes(testChannelPath, testChannelSource, "getDesktopGateState", "fresh test-session client info");
expectIncludes(testChannelPath, testChannelSource, "client_info: gateState.clientInfo", "fresh client info submission");
expectNotIncludes(testChannelPath, testChannelSource, "defaultClientInfo", "stale test client info prop");

expectNotIncludes(routesPath, routesSource, "defaultClientInfo=", "stale test client info wiring");

[
  ["NotAllowedError", "请允许麦克风权限后重试。"],
  ["NotFoundError", "未检测到可用麦克风，请连接设备后重试。"],
  ["AbortError", "麦克风授权未完成，请重新操作。"],
].forEach(([errorName, safeMessage]) => {
  expectIncludes(recorderPath, recorderSource, errorName, "allowlisted microphone error name");
  expectIncludes(recorderPath, recorderSource, safeMessage, "safe microphone error message");
});
expectIncludes(
  recorderPath,
  recorderSource,
  "浏览器未能开始录音，请检查麦克风和浏览器设置后重试。",
  "safe default microphone error message",
);
expectNotIncludes(recorderPath, recorderSource, "error.message", "raw microphone exception message");
expectMatches(
  recorderPath,
  recorderSource,
  /export type RecordingUnavailableOutcome\s*=\s*[\s\S]*"permission-denied"[\s\S]*"prompt-dismissed"[\s\S]*"device-unavailable"[\s\S]*"capability-unavailable"[\s\S]*"recorder-failed"/,
  "typed recording-unavailable outcomes",
);
expectIncludes(
  recorderPath,
  recorderSource,
  "onUnavailable?: (outcome: RecordingUnavailableOutcome) => void",
  "optional recorder unavailable callback",
);
[
  "permission-denied",
  "prompt-dismissed",
  "device-unavailable",
  "capability-unavailable",
  "recorder-failed",
].forEach(outcome => {
  expectMatches(
    recorderPath,
    recorderSource,
    new RegExp(`notifyUnavailable\\(\\s*"${outcome}"`),
    `recording unavailable propagation for ${outcome}`,
  );
});
expectIncludes(
  voicePath,
  voiceSource,
  "onUnavailable?: (outcome: RecordingUnavailableOutcome) => void",
  "optional voice-turn unavailable callback",
);
expectIncludes(
  voicePath,
  voiceSource,
  "onUnavailable={onUnavailable}",
  "voice-turn unavailable callback forwarding",
);
expectIncludes(
  testVoicePath,
  testVoiceSource,
  "const handleVoiceUnavailable = (outcome: RecordingUnavailableOutcome)",
  "typed test voice unavailable handler",
);
expectMatches(
  testVoicePath,
  testVoiceSource,
  /handleVoiceUnavailable[\s\S]*setCanUseVoice\(false\)[\s\S]*onUnavailable=\{handleVoiceUnavailable\}/,
  "test voice fallback after recorder failure",
);
expectIncludes(
  testVoicePath,
  testVoiceSource,
  "语音录入当前不可用，文本输入仍可继续使用。",
  "fixed recording failure recovery text",
);

const obsoleteTestComponentSuffixes = ["ExperimentPage", "DebugPanel"];
obsoleteTestComponentSuffixes.forEach(suffix => {
  const deadPath = `src/features/test/Test${suffix}.tsx`;
  if (existsSync(join(frontendRoot, deadPath))) {
    failures.push(`${deadPath}: obsolete test component still exists`);
  }
});

const apiClientPath = "src/api/client.ts";
const apiClientSource = read(apiClientPath);
expectIncludes(apiClientPath, apiClientSource, "formatApiErrorDetail", "structured API error normalization");
expectIncludes(apiClientPath, apiClientSource, "testAdminDeepSeek", "DeepSeek admin test client method");
expectIncludes(apiClientPath, apiClientSource, '"/api/admin/providers/deepseek/test"', "DeepSeek admin test endpoint");
expectNotIncludes(apiClientPath, apiClientSource, "CLIENT_KEY_OVERRIDE", "DeepSeek client-side credential override");
expectIncludes(apiClientPath, apiClientSource, "requiresNewOperationId", "operation key rotation policy");
expectIncludes(apiClientPath, apiClientSource, 'getRuntimeConfig()', "public runtime config request");
expectIncludes(apiClientPath, apiClientSource, '"/api/runtime-config"', "runtime config endpoint");
expectNotIncludes(apiClientPath, apiClientSource, 'formData.append("duration_seconds"', "trusted client duration metadata");
expectMatches(
  apiClientPath,
  apiClientSource,
  /body: JSON\.stringify\(\{\s*\.\.\.payload,\s*operation_id: payload\.operation_id \?\? crypto\.randomUUID\(\),\s*\}\)/,
  "generated turn operation id survives payload spread",
);

const ratingPath = "src/components/RatingPanel.tsx";
const ratingSource = read(ratingPath);
expectIncludes(ratingPath, ratingSource, "interfaceCopy.rating.title", "rating title constant");
expectIncludes(ratingPath, ratingSource, "interfaceCopy.rating.stanceQuestion", "stance rating constant");
expectIncludes(ratingPath, ratingSource, "interfaceCopy.rating.trustQuestion", "trust rating constant");
expectNotIncludes(ratingPath, ratingSource, "请对本次对话进行评价", "old v2 rating title");

const readmePath = "README.md";
const readmeSource = readProject(readmePath);
const chineseReadmePath = "README.zh-CN.md";
const chineseReadmeSource = readProject(chineseReadmePath);
const manualSmokePath = "src/__checks__/manual-smoke.md";
const manualSmokeSource = read(manualSmokePath);
const deploymentEnvPath = "deployment/interface-v2.env.example";
const deploymentEnvSource = readProject(deploymentEnvPath);
const settingsPath = "backend/app/settings.py";
const settingsSource = readProject(settingsPath);
const deploymentGuidePath = "docs/interface_v2_local_test_and_deploy_zh.md";
const deploymentGuideSource = readProject(deploymentGuidePath);

[
  "DEEPSEEK_BASE_URL=https://api.deepseek.com",
  "DEEPSEEK_API_KEY=",
  "DEEPSEEK_MODEL=deepseek-v4-pro",
].forEach(needle => {
  expectMatches(
    deploymentEnvPath,
    deploymentEnvSource,
    new RegExp(`^${escapeRegExp(needle)}$`, "m"),
    "exact DeepSeek deployment setting",
  );
});
expectMatches(
  settingsPath,
  settingsSource,
  /deepseek_timeout_seconds:\s*float\s*=\s*Field\(default=15\.0,\s*gt=0,\s*le=60\)/,
  "DeepSeek timeout code default and bounds",
);
expectNotMatches(
  deploymentEnvPath,
  deploymentEnvSource,
  /^DEEPSEEK_TIMEOUT_SECONDS=/m,
  "stable DeepSeek timeout override in reduced deployment template",
);

const englishDeepSeekDocumentation = [
  "`DEEPSEEK_BASE_URL=https://api.deepseek.com`",
  "`DEEPSEEK_API_KEY=`",
  "`DEEPSEEK_MODEL=deepseek-v4-pro`",
  "`DEEPSEEK_TIMEOUT_SECONDS=15`",
  "DeepSeek request body adds `\"thinking\":{\"type\":\"disabled\"}`",
  "Formal chat order is six GPT routes -> DeepSeek -> fixed fallback.",
  "The test channel is DeepSeek-only.",
  "The formal and test error evaluator uses the configured official DeepSeek route.",
  "Every model-generated turn uses the legacy `interface` protocol: general error taxonomy -> topic-specific role -> current `normal` or assigned-error instruction.",
  "The DeepSeek evaluator receives every prior user and assistant message from the current session",
  "Evaluator formatting is attempted at most twice.",
  "The admin saves one unified recruitment draft as `开放` or `暂停`.",
  "Each compatibility trial exports `llmProvider` and `llmModel`",
  "Test sessions support both text turns and optional ASR voice turns.",
];
englishDeepSeekDocumentation.forEach(needle => {
  expectIncludes(readmePath, readmeSource, needle, "unified recruitment and DeepSeek documentation");
});

const chineseDeepSeekDocumentation = [
  "`DEEPSEEK_BASE_URL=https://api.deepseek.com`",
  "`DEEPSEEK_API_KEY=`",
  "`DEEPSEEK_MODEL=deepseek-v4-pro`",
  "`DEEPSEEK_TIMEOUT_SECONDS=15`",
  "DeepSeek 请求体会附加 `\"thinking\":{\"type\":\"disabled\"}`",
  "正式聊天顺序是六条 GPT 路由 -> DeepSeek -> 固定 fallback。",
  "测试通道只走 DeepSeek。",
  "正式实验和测试通道的错误评估器统一使用配置的官方 DeepSeek 路由。",
  "每个由模型生成的轮次都使用旧版 `interface` 协议：通用错误分类提示词 -> topic 专用角色 -> 当前 `normal` 或分配错误的具体提示词。",
  "DeepSeek 评估器接收当前 session 之前的全部用户和助手消息",
  "每个候选最多进行两次评估格式尝试",
  "管理端把统一的正式招募草稿保存为 `开放` 或 `暂停`。",
  "每个兼容 trial 都导出 `llmProvider` 和 `llmModel`",
  "测试 session 同时支持文本 turn 和可选 ASR 语音 turn。",
];
[
  [chineseReadmePath, chineseReadmeSource],
  [deploymentGuidePath, deploymentGuideSource],
].forEach(([filePath, source]) => {
  chineseDeepSeekDocumentation.forEach(needle => {
    expectIncludes(filePath, source, needle, "unified recruitment and DeepSeek documentation");
  });
});

[
  "DeepSeek request body adds `\"thinking\":{\"type\":\"disabled\"}`",
  "Ordinary formal chat order is six GPT routes -> DeepSeek -> fixed fallback; planned non-system errors use the same external failover without local content fallback.",
  "The test channel is DeepSeek-only.",
  "The admin saves one unified recruitment draft as `开放` or `暂停`.",
  "Each compatibility trial exports `llmProvider` and `llmModel`",
  "Test sessions support both text turns and optional ASR voice turns.",
].forEach(needle => {
  expectIncludes(manualSmokePath, manualSmokeSource, needle, "manual unified recruitment and DeepSeek check");
});

const currentBehaviorDocumentation = [
  [readmePath, readmeSource],
  [chineseReadmePath, chineseReadmeSource],
  [deploymentGuidePath, deploymentGuideSource],
  [manualSmokePath, manualSmokeSource],
];
const retiredDocumentationContracts = [
  "yi-zhan-only chat routing",
  "测试聊天路由只使用 yi-zhan",
  "pause_new_participants",
  "test_channel_only",
  "需确认",
  "opening or closing requires confirmation",
  "开启或关闭均需确认",
  "requires confirmation for both open and close",
  "确认开放正式招募",
  "确认关闭正式招募",
];
currentBehaviorDocumentation.forEach(([filePath, source]) => {
  retiredDocumentationContracts.forEach(needle => {
    expectNotIncludes(filePath, source, needle, "retired recruitment/provider documentation");
  });
  expectNotMatches(
    filePath,
    source,
    /\bglobal (?:control|toggle)s?\b|\bglobals\b/i,
    "retired user-facing assignment global-control synonym",
  );
});

expectIncludes(
  readmePath,
  readmeSource,
  "An incomplete active attempt and its data remain recoverable on relogin",
  "active-attempt recovery contract",
);
expectIncludes(
  chineseReadmePath,
  chineseReadmeSource,
  "未完成的 active attempt 及其数据在再次登录后仍可恢复",
  "Chinese active-attempt recovery contract",
);
[
  "login -> environment check -> welcome/pretest",
  "`/environment-check` 是登录后、进入欢迎页/前测/正式实验前的明确门禁页面",
  "当前 relogin 重新分配行为",
  "后端会放弃该 attempt，并分配一个新的 current attempt",
  "旧 attempt 上未完成的正式 session 及其音频文件会被删除",
  "`/environment-check`：登录后必须先完成实验环境检测",
].forEach(needle => {
  expectNotIncludes(chineseReadmePath, chineseReadmeSource, needle, "stale participant flow");
});
expectIncludes(
  chineseReadmePath,
  chineseReadmeSource,
  "正式环境门禁在登录和开始实验操作中执行",
  "in-action formal environment gate",
);
const publicationContracts = [
  [
    readmePath,
    readmeSource,
    "does not authorize external publication or invitations to real participants",
  ],
  [
    chineseReadmePath,
    chineseReadmeSource,
    "不代表已获准对外发布或邀请真实被试",
  ],
  [
    manualSmokePath,
    manualSmokeSource,
    "does not authorize external publication or invitations to real participants",
  ],
];
publicationContracts.forEach(([filePath, source, needle]) => {
  expectIncludes(filePath, source, needle, "local-runtime publication boundary");
});
[
  [readmePath, readmeSource, "real-browser, provider/ASR, and Linux deployment evidence remains pending"],
  [chineseReadmePath, chineseReadmeSource, "真实浏览器、供应商/ASR 和 Linux 部署证据仍待完成"],
  [manualSmokePath, manualSmokeSource, "real browser, provider, ASR, or Linux deployment validation"],
].forEach(([filePath, source, needle]) => {
  expectIncludes(filePath, source, needle, "pending publication evidence");
});

try {
  await runExecutableFormatterChecks();
} catch (error) {
  failures.push(`executable frontend contracts failed: ${error instanceof Error ? error.message : String(error)}`);
}

if (failures.length) {
  console.error("Interface parity check failed:");
  for (const failure of failures) {
    console.error(`- ${failure}`);
  }
  process.exit(1);
}

console.log("Interface parity check passed.");
