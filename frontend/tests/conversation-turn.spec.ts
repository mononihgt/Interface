import { expect, test, type Page } from "@playwright/test";


function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

function session(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "e2e-session",
    day_index: 1,
    status: "started",
    topic_title: "咨询建议",
    topic_description: "请围绕主题展开对话。",
    started_at: "2026-07-13T10:00:00+08:00",
    is_test: true,
    expected_turn_index: 1,
    presentation_mode: "conversation",
    artifact_kind: null,
    artifact_status: "none",
    turns: [],
    ...overrides,
  };
}

const submittedTurn = {
  turn_id: 101,
  turn_index: 1,
  user_text: "保留真实对话",
  user_input_mode: "text_test_only",
  assistant_text: "这是已经落库的助手回复。",
  rating: null,
};

async function mockRuntimeConfig(page: Page) {
  await page.route("**/api/runtime-config", (route) =>
    route.fulfill({ json: { asr_max_duration_seconds: 60 } }),
  );
}

function textInput(page: Page) {
  return page.getByRole("region", { name: "测试文本输入" });
}

test("pending user message appears before the assistant response", async ({ page }) => {
  const turnStarted = deferred();
  const releaseTurn = deferred();
  let turnSubmitted = false;

  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({
      json: turnSubmitted
        ? session({ expected_turn_index: 2, turns: [submittedTurn] })
        : session(),
    }),
  );
  await page.route("**/api/turns", async (route) => {
    turnStarted.resolve();
    await releaseTurn.promise;
    turnSubmitted = true;
    await route.fulfill({
      json: {
        ...submittedTurn,
        user_text: "立即显示的用户消息",
      },
    });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = textInput(page);
  await inputRegion.getByPlaceholder("也可以在这里输入文字...").fill("立即显示的用户消息");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();
  await turnStarted.promise;

  await expect(page.getByText("立即显示的用户消息", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "助手正在思考" })).toBeVisible();

  releaseTurn.resolve();
  await expect(page.getByText("这是已经落库的助手回复。", { exact: true })).toBeVisible();
});

test("records send-to-render timing and retries the frozen receipt", async ({ page }) => {
  const releaseTurn = deferred();
  let turnSubmitted = false;
  const timingBodies: unknown[] = [];

  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({
      json: turnSubmitted
        ? session({ expected_turn_index: 2, turns: [submittedTurn] })
        : session(),
    }),
  );
  await page.route("**/api/turns", async (route) => {
    await releaseTurn.promise;
    turnSubmitted = true;
    await route.fulfill({ json: submittedTurn });
  });
  await page.route("**/api/turns/101/client-timing", async (route) => {
    const body = route.request().postDataJSON();
    timingBodies.push(body);
    if (timingBodies.length === 1) {
      await route.fulfill({ status: 503, json: { detail: "timing unavailable" } });
      return;
    }
    await route.fulfill({
      json: {
        turn_id: 101,
        ...body,
        render_timing_received_at: "2026-07-13T02:00:05",
      },
    });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = textInput(page);
  await inputRegion.getByPlaceholder("也可以在这里输入文字...").fill("保留真实对话");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();

  await expect(page.getByRole("status", { name: "助手正在思考" })).toBeVisible();
  await page.waitForTimeout(100);
  expect(timingBodies).toHaveLength(0);

  releaseTurn.resolve();
  await expect(page.getByText("这是已经落库的助手回复。", { exact: true })).toBeVisible();
  await expect.poll(() => timingBodies.length).toBe(2);

  expect(timingBodies[0]).toEqual(timingBodies[1]);
  const timing = timingBodies[0] as Record<string, unknown>;
  expect(timing.client_response_latency_ms).toEqual(expect.any(Number));
  expect(timing.client_response_latency_ms as number).toBeGreaterThanOrEqual(100);
  expect(timing.client_message_sent_at).toEqual(expect.any(String));
  expect(timing.assistant_render_completed_at).toEqual(expect.any(String));
  expect(timing.client_timing_interrupted).toBe(false);
});

test("marks response timing when the page is hidden during the wait", async ({ page }) => {
  const releaseTurn = deferred();
  let turnSubmitted = false;
  let timingBody: Record<string, unknown> | null = null;

  await page.addInitScript(() => {
    let testVisibilityState: DocumentVisibilityState = "visible";
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => testVisibilityState,
    });
    Object.defineProperty(window, "setTestVisibilityState", {
      configurable: true,
      value: (nextState: DocumentVisibilityState) => {
        testVisibilityState = nextState;
        document.dispatchEvent(new Event("visibilitychange"));
      },
    });
  });
  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({
      json: turnSubmitted
        ? session({ expected_turn_index: 2, turns: [submittedTurn] })
        : session(),
    }),
  );
  await page.route("**/api/turns", async (route) => {
    await releaseTurn.promise;
    turnSubmitted = true;
    await route.fulfill({ json: submittedTurn });
  });
  await page.route("**/api/turns/101/client-timing", async (route) => {
    timingBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        turn_id: 101,
        ...timingBody,
        render_timing_received_at: "2026-07-13T02:00:05",
      },
    });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = textInput(page);
  await inputRegion.getByPlaceholder("也可以在这里输入文字...").fill("保留真实对话");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();
  await page.evaluate(() => {
    const testWindow = window as typeof window & {
      setTestVisibilityState: (state: DocumentVisibilityState) => void;
    };
    testWindow.setTestVisibilityState("hidden");
  });
  releaseTurn.resolve();

  await expect(page.getByText("这是已经落库的助手回复。", { exact: true })).toBeVisible();
  await expect.poll(() => timingBody).not.toBeNull();
  expect(timingBody?.client_timing_interrupted).toBe(true);
});

test("turn failure removes the pending message and restores text input", async ({ page }) => {
  const turnStarted = deferred();
  const releaseTurn = deferred();

  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({ json: session() }),
  );
  await page.route("**/api/turns", async (route) => {
    turnStarted.resolve();
    await releaseTurn.promise;
    await route.fulfill({
      status: 503,
      json: { detail: "AI 暂时不可用" },
    });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = textInput(page);
  const input = inputRegion.getByPlaceholder("也可以在这里输入文字...");
  await input.fill("立即显示的用户消息");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();
  await turnStarted.promise;

  await expect(page.getByText("立即显示的用户消息", { exact: true })).toBeVisible();
  releaseTurn.resolve();

  await expect(page.getByText("立即显示的用户消息", { exact: true })).toHaveCount(0);
  await expect(input).toHaveValue("立即显示的用户消息");
  await expect(page.getByText("AI 暂时不可用", { exact: true })).toBeVisible();
});

test("successful turn remains visible when canonical session refresh fails", async ({ page }) => {
  const refreshStarted = deferred();
  const releaseRefresh = deferred();
  let turnSubmitted = false;

  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", async (route) => {
    if (!turnSubmitted) {
      await route.fulfill({ json: session() });
      return;
    }
    refreshStarted.resolve();
    await releaseRefresh.promise;
    await route.fulfill({
      status: 503,
      json: { detail: "会话同步暂时失败" },
    });
  });
  await page.route("**/api/turns", async (route) => {
    turnSubmitted = true;
    await route.fulfill({ json: submittedTurn });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = textInput(page);
  await inputRegion.getByPlaceholder("也可以在这里输入文字...").fill("保留真实对话");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();
  await refreshStarted.promise;

  await expect(page.getByText("保留真实对话", { exact: true })).toBeVisible();
  await expect(page.getByText("这是已经落库的助手回复。", { exact: true })).toBeVisible();
  await expect(page.locator(".rating-panel")).toBeVisible();

  releaseRefresh.resolve();
  await expect(page.getByText("会话同步暂时失败", { exact: true })).toBeVisible();
  await expect(page.getByText("保留真实对话", { exact: true })).toBeVisible();
  await expect(page.getByText("这是已经落库的助手回复。", { exact: true })).toBeVisible();
});

test("recognized voice message appears while its assistant response is pending", async ({ page }) => {
  const turnStarted = deferred();
  const releaseTurn = deferred();
  let turnSubmitted = false;
  const voiceTurn = {
    turn_id: 102,
    turn_index: 1,
    user_text: "语音即时显示",
    user_input_mode: "voice",
    assistant_text: "语音回复已经返回。",
    rating: null,
  };

  await page.addInitScript(() => {
    Object.defineProperty(navigator, "permissions", {
      configurable: true,
      value: { query: async () => ({ state: "granted" }) },
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop: () => undefined }],
          getAudioTracks: () => [{
            stop: () => undefined,
            addEventListener: () => undefined,
          }],
        }),
      },
    });
    class FakeMediaRecorder {
      static isTypeSupported() {
        return true;
      }

      state = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob(["recorded audio"], { type: this.mimeType }),
        });
        this.onstop?.();
      }
    }
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: FakeMediaRecorder,
    });
  });

  await mockRuntimeConfig(page);
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({
      json: turnSubmitted
        ? session({ expected_turn_index: 2, turns: [voiceTurn] })
        : session(),
    }),
  );
  await page.route("**/api/asr", (route) =>
    route.fulfill({
      json: {
        asr_result_id: "voice-result-reference-000000000001",
        asr_status: "success",
        asr_text: "语音即时显示",
        retry_count: 0,
        max_retry_per_turn: 3,
      },
    }),
  );
  await page.route("**/api/turns", async (route) => {
    turnStarted.resolve();
    await releaseTurn.promise;
    turnSubmitted = true;
    await route.fulfill({ json: voiceTurn });
  });

  await page.goto("/experiment/e2e-session");
  const voiceRegion = page.getByRole("region", { name: "语音输入" });
  await voiceRegion.getByRole("button", { name: "开始录音" }).click();
  await voiceRegion.getByRole("button", { name: "停止并转写" }).click();
  await expect(voiceRegion.getByText("识别结果：语音即时显示", { exact: true })).toBeVisible();
  await voiceRegion.getByRole("button", { name: "发送", exact: true }).click();
  await turnStarted.promise;

  await expect(page.getByText("语音即时显示", { exact: true })).toBeVisible();
  await expect(page.getByRole("status", { name: "助手正在思考" })).toBeVisible();

  releaseTurn.resolve();
  await expect(page.getByText("语音回复已经返回。", { exact: true })).toBeVisible();
});

test("automatically stops voice recording before the ASR duration limit", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "permissions", {
      configurable: true,
      value: { query: async () => ({ state: "granted" }) },
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: async () => ({
          getTracks: () => [{ stop: () => undefined }],
          getAudioTracks: () => [{
            stop: () => undefined,
            addEventListener: () => undefined,
          }],
        }),
      },
    });
    class FakeMediaRecorder {
      static isTypeSupported() {
        return true;
      }

      state = "inactive";
      mimeType = "audio/webm";
      ondataavailable: ((event: { data: Blob }) => void) | null = null;
      onstop: (() => void) | null = null;
      onerror: (() => void) | null = null;

      start() {
        this.state = "recording";
      }

      stop() {
        this.state = "inactive";
        this.ondataavailable?.({
          data: new Blob(["recorded audio"], { type: this.mimeType }),
        });
        this.onstop?.();
      }
    }
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: FakeMediaRecorder,
    });
  });

  await page.route("**/api/runtime-config", (route) =>
    route.fulfill({ json: { asr_max_duration_seconds: 2 } }),
  );
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({ json: session() }),
  );

  let uploadElapsedMs: number | null = null;
  let recordingStartedAt = 0;
  await page.route("**/api/asr", (route) => {
    uploadElapsedMs = Date.now() - recordingStartedAt;
    return route.fulfill({
      json: {
        asr_result_id: "voice-result-reference-000000000001",
        asr_status: "success",
        asr_text: "自动停止识别",
        retry_count: 0,
        max_retry_per_turn: 3,
      },
    });
  });

  await page.goto("/experiment/e2e-session");
  const voiceRegion = page.getByRole("region", { name: "语音输入" });
  recordingStartedAt = Date.now();
  await voiceRegion.getByRole("button", { name: "开始录音" }).click();

  await expect(voiceRegion.getByText("识别结果：自动停止识别", { exact: true })).toBeVisible();
  expect(uploadElapsedMs).not.toBeNull();
  expect(uploadElapsedMs as number).toBeLessThan(1700);
});
