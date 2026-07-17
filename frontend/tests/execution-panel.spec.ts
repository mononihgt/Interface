import { expect, test, type Page, type TestInfo } from "@playwright/test";

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const viewports = [
  { width: 1366, height: 768 },
  { width: 1180, height: 800 },
  { width: 1024, height: 768 },
];

const scheduleArtifact = {
  actionType: "schedule_table",
  actionMode: "create",
  status: "completed",
  requestedSource: "用户提供的安排",
  columns: ["日期", "时间", "地点", "任务", "备注"],
  rows: [
    {
      date: "7月13日",
      time: "09:00",
      location: "会议室A",
      task: "项目会与较长的项目阶段同步说明",
      note: "携带材料并提前检查会议设备",
    },
  ],
};

const copyArtifact = {
  actionType: "copy_editor",
  actionMode: "create",
  status: "completed",
  versions: [
    { id: "v1", label: "直接礼貌版", text: "今晚会晚到十分钟，请大家先开始。" },
    { id: "v2", label: "柔和版", text: "抱歉，我今晚会晚到十分钟，麻烦大家先开始。" },
  ],
  selected_version: { version_id: "v1", reason: "信息清楚，语气礼貌。" },
  revision_notes: ["保留迟到时长。"],
};

function session(overrides: Record<string, unknown> = {}) {
  return {
    session_id: "e2e-session",
    day_index: 1,
    status: "started",
    topic_title: "信息整理执行",
    started_at: "2026-07-12T10:00:00+08:00",
    is_test: true,
    expected_turn_index: 1,
    presentation_mode: "execution",
    artifact_kind: "schedule_table",
    artifact_status: "completed",
    artifact_type: "table",
    artifact_payload: scheduleArtifact,
    turns: [],
    ...overrides,
  };
}

async function mockExecutionSession(page: Page, transition = false) {
  let turnSubmitted = false;
  await page.route("**/api/runtime-config", (route) =>
    route.fulfill({ json: { asr_max_duration_seconds: 60 } }),
  );
  await page.route("**/api/sessions/e2e-session", async (route) => {
    const nextSession = transition && turnSubmitted
      ? session({
          expected_turn_index: 2,
          artifact_status: "awaiting_input",
          turns: [
            {
              turn_id: 101,
              turn_index: 1,
              user_text: "请把这份安排再补充负责人。",
              user_input_mode: "text_test_only",
              assistant_text: "请告诉我每项任务对应的负责人。",
              rating: null,
            },
          ],
        })
      : session();
    await route.fulfill({ json: nextSession });
  });
  await page.route("**/api/turns", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 300));
    turnSubmitted = true;
    await route.fulfill({
      json: {
        turn_id: 101,
        turn_index: 1,
        user_text: "请把这份安排再补充负责人。",
        user_input_mode: "text_test_only",
        assistant_text: "请告诉我每项任务对应的负责人。",
        rating: null,
      },
    });
  });
}

async function mockStaticSession(page: Page, sessionView: Record<string, unknown>) {
  await page.route("**/api/runtime-config", (route) =>
    route.fulfill({ json: { asr_max_duration_seconds: 60 } }),
  );
  await page.route("**/api/sessions/e2e-session", (route) =>
    route.fulfill({ json: sessionView }),
  );
}

async function mockFormalEnvironment(page: Page) {
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
        }),
      },
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: class MediaRecorder {},
    });
  });
}

async function attachScreenshot(page: Page, testInfo: TestInfo, name: string) {
  const screenshot = await page.screenshot({ fullPage: true });
  expect(screenshot.byteLength).toBeGreaterThan(1_000);
  await testInfo.attach(name, { body: screenshot, contentType: "image/png" });
}

for (const viewport of viewports) {
  test(`${viewport.width}x${viewport.height} keeps execution workspace side by side`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await mockExecutionSession(page);
    await page.goto("/experiment/e2e-session");

    const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
    const panelColumn = page.locator(".tool-action-panel");
    const chat = page.locator(".chat-main");
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("data-execution-phase", "success");

    const panelBox = await panelColumn.boundingBox();
    const chatBox = await chat.boundingBox();
    expect(panelBox).not.toBeNull();
    expect(chatBox).not.toBeNull();
    expect(Math.abs(panelBox!.y - chatBox!.y)).toBeLessThanOrEqual(1);
    expect(chatBox!.x + chatBox!.width).toBeLessThanOrEqual(panelBox!.x + 1);
    expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(viewport.width + 1);

    const horizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth - window.innerWidth,
    );
    expect(horizontalOverflow).toBeLessThanOrEqual(0);
    await attachScreenshot(page, testInfo, `execution-${viewport.width}x${viewport.height}`);
  });

  test(`${viewport.width}x${viewport.height} keeps panel position stable across states`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await mockExecutionSession(page, true);
    await page.goto("/experiment/e2e-session");

    const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
    const panelColumn = page.locator(".tool-action-panel");
    const textInput = page.getByRole("region", { name: "测试文本输入" });
    const initialBox = await panelColumn.boundingBox();
    await textInput.getByPlaceholder("也可以在这里输入文字...").fill("请把这份安排再补充负责人。");
    await textInput.getByRole("button", { name: "发送", exact: true }).click();
    await expect(panel).toHaveAttribute("data-execution-phase", "loading");
    await expect(panel.getByText("助手正在处理本轮内容。")).toBeVisible();
    const loadingBox = await panelColumn.boundingBox();

    await expect(panel).toHaveAttribute("data-execution-phase", "awaiting");
    await expect(panel).toHaveAttribute("data-rating-phase", "active");
    await expect(panel.getByText("请完成本轮评分。")).toBeVisible();
    await expect(panel.getByText("项目会与较长的项目阶段同步说明")).toBeVisible();
    const awaitingBox = await panelColumn.boundingBox();
    const chatBox = await page.locator(".chat-main").boundingBox();
    const ratingBox = await page.locator(".rating-panel").boundingBox();

    expect(chatBox).not.toBeNull();
    expect(ratingBox).not.toBeNull();
    expect(ratingBox!.x).toBeGreaterThanOrEqual(chatBox!.x);
    expect(ratingBox!.x + ratingBox!.width).toBeLessThanOrEqual(
      chatBox!.x + chatBox!.width + 1,
    );
    expect(ratingBox!.x + ratingBox!.width).toBeLessThanOrEqual(awaitingBox!.x + 1);

    for (const box of [loadingBox, awaitingBox]) {
      expect(box).not.toBeNull();
      expect(Math.abs(box!.x - initialBox!.x)).toBeLessThanOrEqual(1);
      expect(Math.abs(box!.y - initialBox!.y)).toBeLessThanOrEqual(1);
      expect(Math.abs(box!.width - initialBox!.width)).toBeLessThanOrEqual(1);
    }
    await attachScreenshot(page, testInfo, `execution-states-${viewport.width}x${viewport.height}`);
  });
}

test("formal execution session mounts an empty workspace before the first turn", async ({ page }) => {
  await page.setViewportSize(viewports[0]);
  await mockFormalEnvironment(page);
  const formalSession = session({
    is_test: false,
    artifact_status: "none",
    artifact_payload: null,
  });
  await mockStaticSession(page, formalSession);
  await page.route("**/api/sessions/start", (route) => route.fulfill({ json: formalSession }));
  await page.route("**/api/me", (route) => route.fulfill({ json: {
    participant_id: 1,
    attempt_id: 1,
    attempt_no: 1,
    name: "测试被试",
    masked_phone: "138****8000",
    participant_type: "short",
    target_days: 1,
    current_status: "active",
    participation_state: "ready_for_experiment",
    current_day: {
      day_index: 1,
      calendar_date: "2026-07-12",
      status: "active",
      can_start_experiment: true,
    },
    pretest_status: {
      status: "completed",
      autosave_count: 1,
      has_draft: true,
      has_final: true,
    },
  } }));
  await page.goto("/experiment/e2e-session");

  const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute("data-execution-phase", "empty");
  await expect(panel.getByText("等待你提供材料后，助手会在这里生成结果。")).toBeVisible();
  await expect(panel.locator("table")).toHaveCount(0);
});

test("execution timing waits for the current right-side result to render", async ({ page }) => {
  const canonicalRefreshStarted = deferred();
  const releaseCanonicalRefresh = deferred();
  let turnSubmitted = false;
  const timingBodies: unknown[] = [];
  const returnedTurn = {
    turn_id: 101,
    turn_index: 1,
    user_text: "生成新的项目安排。",
    user_input_mode: "text_test_only",
    assistant_text: "新的安排已经生成。",
    rating: null,
  };

  await page.route("**/api/runtime-config", (route) =>
    route.fulfill({ json: { asr_max_duration_seconds: 60 } }),
  );
  await page.route("**/api/sessions/e2e-session", async (route) => {
    if (!turnSubmitted) {
      await route.fulfill({
        json: session({ artifact_status: "none", artifact_payload: null }),
      });
      return;
    }
    canonicalRefreshStarted.resolve();
    await releaseCanonicalRefresh.promise;
    await route.fulfill({
      json: session({
        expected_turn_index: 2,
        artifact_status: "completed",
        artifact_payload: scheduleArtifact,
        turns: [returnedTurn],
      }),
    });
  });
  await page.route("**/api/turns", async (route) => {
    turnSubmitted = true;
    await route.fulfill({ json: returnedTurn });
  });
  await page.route("**/api/turns/101/client-timing", async (route) => {
    const body = route.request().postDataJSON();
    timingBodies.push(body);
    await route.fulfill({
      json: {
        turn_id: 101,
        ...body,
        render_timing_received_at: "2026-07-13T02:00:05",
      },
    });
  });

  await page.goto("/experiment/e2e-session");
  const inputRegion = page.getByRole("region", { name: "测试文本输入" });
  await inputRegion.getByPlaceholder("也可以在这里输入文字...").fill("生成新的项目安排。");
  await inputRegion.getByRole("button", { name: "发送", exact: true }).click();
  await canonicalRefreshStarted.promise;

  await expect(page.getByText("新的安排已经生成。", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("complementary", { name: "任务执行状态", exact: true }),
  ).toHaveAttribute("data-execution-phase", "loading");
  expect(timingBodies).toHaveLength(0);

  releaseCanonicalRefresh.resolve();
  await expect(page.getByText("项目会与较长的项目阶段同步说明")).toBeVisible();
  await expect.poll(() => timingBodies.length).toBe(1);
});

for (const stateCase of [
  {
    status: "awaiting_input",
    phase: "awaiting",
    message: "还需要补充信息，请补充后继续。",
  },
  {
    status: "failed",
    phase: "failure",
    message: "本轮未能生成有效结果，请重试。",
  },
]) {
  test(`${stateCase.phase} without history uses accurate Chinese copy`, async ({ page }) => {
    await mockStaticSession(page, session({
      artifact_status: stateCase.status,
      artifact_payload: null,
    }));
    await page.goto("/experiment/e2e-session");

    const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
    await expect(panel).toHaveAttribute("data-execution-phase", stateCase.phase);
    await expect(panel.getByText(stateCase.message)).toBeVisible();
    await expect(panel.getByText("已有结果会保留在下方。", { exact: false })).toHaveCount(0);
  });
}

test("completed malformed payload fails safely without rendering an artifact", async ({ page }) => {
  await mockStaticSession(page, session({
    artifact_payload: {
      actionType: "schedule_table",
      columns: ["日期", "时间", "地点", "任务", "备注"],
      rows: [{ date: "7月13日", time: "09:00" }],
    },
  }));
  await page.goto("/experiment/e2e-session");

  const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
  await expect(panel).toHaveAttribute("data-execution-phase", "failure");
  await expect(panel.getByText("本轮未能生成有效结果，请重试。")).toBeVisible();
  await expect(panel.locator("table")).toHaveCount(0);
  await expect(panel.locator(".artifact-block")).toHaveCount(0);
});

test("copy editor renders only validated copy versions", async ({ page }) => {
  await mockStaticSession(page, session({
    artifact_kind: "copy_editor",
    artifact_type: "copy_versions",
    artifact_payload: copyArtifact,
  }));
  await page.goto("/experiment/e2e-session");

  const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
  await expect(panel).toHaveAttribute("data-execution-phase", "success");
  await expect(panel.locator(".artifact-block")).toHaveCount(2);
  await expect(panel.getByText("今晚会晚到十分钟，请大家先开始。")).toBeVisible();
  await expect(panel.getByText("推荐理由：信息清楚，语气礼貌。")).toBeVisible();
  await expect(panel.locator("table")).toHaveCount(0);
});

test("failure with history keeps the artifact and reports preservation", async ({ page }) => {
  await mockStaticSession(page, session({ artifact_status: "failed" }));
  await page.goto("/experiment/e2e-session");

  const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
  await expect(panel).toHaveAttribute("data-execution-phase", "failure");
  await expect(
    panel.getByText("本轮未能生成有效结果，已有结果会保留在下方。"),
  ).toBeVisible();
  await expect(panel.getByText("项目会与较长的项目阶段同步说明")).toBeVisible();
});

test("long execution content scrolls inside the bounded panel", async ({ page }, testInfo) => {
  await page.setViewportSize(viewports[2]);
  const longRows = Array.from({ length: 35 }, (_, index) => ({
    date: `7月${index + 1}日`,
    time: "09:00",
    location: "会议室A",
    task: `第 ${index + 1} 项任务与完整的长内容说明`,
    note: "携带材料并提前检查会议设备",
  }));
  await mockStaticSession(page, session({
    artifact_payload: { ...scheduleArtifact, rows: longRows },
  }));
  await page.goto("/experiment/e2e-session");

  const panel = page.getByRole("complementary", { name: "任务执行状态", exact: true });
  const artifactScroller = panel.locator(".table-scroll");
  await expect(panel).toHaveAttribute("data-execution-phase", "success");
  await expect(panel.getByText("第 35 项任务与完整的长内容说明")).toBeAttached();
  const scrollState = await artifactScroller.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    overflowY: getComputedStyle(element).overflowY,
  }));
  expect(scrollState.scrollHeight).toBeGreaterThan(scrollState.clientHeight);
  expect(scrollState.overflowY).toBe("auto");
  await artifactScroller.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(panel.getByText("第 35 项任务与完整的长内容说明")).toBeVisible();
  await attachScreenshot(page, testInfo, "execution-long-content-scrolled");
});
