import { expect, test, type Page } from "@playwright/test";

async function mockAdminSession(page: Page) {
  await page.route("**/api/admin/session", (route) =>
    route.fulfill({ json: { authenticated: true, admin_user: "admin" } }),
  );
}

async function mockSystemSection(page: Page) {
  await page.route("**/api/admin/system-metrics", (route) =>
    route.fulfill({
      json: {
        service: { status: "running", label: "running" },
        database: { size_bytes: 1024, path: "app.db" },
        data_directory: { disk_usage: { used_bytes: 2048, free_bytes: 4096 } },
        audio_directory: { files: 0, size_bytes: 0 },
        exports_directory: { files: 0, size_bytes: 0 },
        experiment: {
          today_started: 0,
          today_completed: 0,
          risk_sessions: 0,
          api_failures: 0,
          asr_failures: 0,
        },
      },
    }),
  );
  await page.route("**/api/admin/provider-model-usage", (route) =>
    route.fulfill({
      json: {
        generated_at: "2026-07-16 17:55:00",
        deepseek_configuration: {
          status: "configured",
          model: "deepseek-test",
          timeout_seconds: 15,
        },
        windows: [
          { window: "all_time", provider_model_rows: [] },
          { window: "last_24h", provider_model_rows: [] },
        ],
        notes: [],
      },
    }),
  );
  await page.route("**/api/admin/api-health", (route) =>
    route.fulfill({
      json: {
        routes: [],
        cooldowns: [],
        failure_reasons: [],
        evaluator_success_rate: {},
        asr_success_rate: {},
        manual_test_runs: [],
        notes: [],
      },
    }),
  );
  await page.route("**/api/admin/system-logs", (route) =>
    route.fulfill({
      json: {
        backend_status_counts: {},
        api_log_counts: [],
        asr_status_counts: {},
        database_size_bytes: 1024,
        disk_usage: { total_bytes: 8192, used_bytes: 2048, free_bytes: 6144 },
        audio_directory: { path: "audio", files: 0, size_bytes: 0 },
        exports_directory: "exports",
        sanitized_package_path: null,
        notes: [],
      },
    }),
  );
}

async function mockExportSection(page: Page) {
  await page.route("**/api/admin/export-jobs", (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route("**/api/admin/clean-data-audits**", (route) =>
    route.fulfill({
      json: {
        status: "all",
        count: 1,
        last_updated_at: "2026-07-16 17:52:24",
        summary: {
          scanned: 207,
          persisted: 207,
          status_counts: { eligible: 113, excluded: 94, review_needed: 0 },
        },
        items: [
          {
            participant_id: 1,
            attempt_id: 1,
            name: "Audit Participant",
            phone_hash: "hash",
            participant_type: "short",
            condition: "human",
            subcondition: "qa",
            topic_key: "advice",
            error_type_id: "factual_minor",
            status: "eligible",
            reasons: [],
            reviewer_note: null,
            reviewed_by: null,
            reviewed_at: null,
            computed_at: "2026-07-16 17:52:24",
          },
        ],
      },
    }),
  );
}

test("clean data audit panel labels its refresh action and timestamp as updates", async ({ page }) => {
  await mockAdminSession(page);
  await mockSystemSection(page);
  await mockExportSection(page);

  await page.goto("/admin");
  await page.getByRole("button", { name: "数据导出" }).click();

  await expect(page.getByRole("heading", { name: "完整数据审核" })).toBeVisible();
  await expect(page.getByText("更新时间：2026-07-16 17:52:24")).toBeVisible();
  await expect(page.getByRole("button", { name: "更新" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "更新时间" })).toBeVisible();
  await expect(page.getByRole("button", { name: "重算" })).toHaveCount(0);
  await expect(page.getByRole("columnheader", { name: "计算" })).toHaveCount(0);
});
