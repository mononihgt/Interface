import { expect, test, type Page } from "@playwright/test";

function buildCompletePretestPayload() {
  const scales: Record<string, number | string> = {};
  for (let index = 1; index <= 26; index += 1) {
    scales[`q${index}`] = 3;
  }
  for (let index = 27; index <= 47; index += 1) {
    scales[`q${index}`] = 50;
  }
  for (let index = 27; index <= 46; index += 1) {
    scales[`confidence_q${index}`] = 75;
  }
  scales.q48 = "B";
  scales.q49 = "C";

  const sliderTouchState: Record<string, boolean> = {};
  for (let index = 27; index <= 47; index += 1) {
    sliderTouchState[`q${index}`] = true;
  }
  for (let index = 27; index <= 46; index += 1) {
    sliderTouchState[`confidence_q${index}`] = true;
  }

  return {
    demographics: {
      birthDate: "2000-01-01",
      gender: "男",
      idNumber: "ID1234567",
    },
    scales,
    slider_touch_state: sliderTouchState,
    page_progress: {
      section: "scales",
      current_step: "scales",
      completed_steps: ["intro", "demographics"],
    },
    client_timestamp: "2026-07-15T09:30:00+08:00",
  };
}

async function mockPretestParticipant(page: Page, initialPayload: Record<string, unknown>) {
  await page.route("**/api/me", (route) =>
    route.fulfill({
      json: {
        participant_id: 1,
        attempt_id: 1,
        attempt_no: 1,
        name: "Pretest Slider",
        masked_phone: "199****0000",
        participant_type: "short",
        target_days: 1,
        current_status: "pretest",
        participation_state: "needs_pretest",
        participation_message: null,
        current_day: {
          day_index: 1,
          calendar_date: "2026-07-15",
          status: "pretest",
          can_start_experiment: false,
        },
        pretest_status: {
          status: "draft",
          autosave_count: 1,
          has_draft: true,
          has_final: false,
          last_saved_at: "2026-07-15T09:30:00+08:00",
          submitted_at: null,
        },
      },
    }),
  );
  await page.route("**/api/pretest/current", (route) =>
    route.fulfill({
      json: {
        day_index: 1,
        status: "draft",
        autosave_count: 1,
        payload: initialPayload,
        last_saved_at: "2026-07-15T09:30:00+08:00",
        submitted_at: null,
        can_start_experiment: false,
      },
    }),
  );
  await page.route("**/api/pretest/draft", (route) =>
    route.fulfill({
      json: {
        day_index: 1,
        status: "draft",
        autosave_count: 2,
        payload: route.request().postDataJSON(),
        last_saved_at: "2026-07-15T09:31:00+08:00",
        submitted_at: null,
        can_start_experiment: false,
      },
    }),
  );
}

test("clicking a restored default slider marks it touched before final submit", async ({ page }) => {
  const restoredPayload = buildCompletePretestPayload();
  delete (restoredPayload.slider_touch_state as Record<string, boolean>).q27;
  let finalPayload: Record<string, unknown> | null = null;

  await mockPretestParticipant(page, restoredPayload);
  await page.route("**/api/pretest/final", async (route) => {
    finalPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        day_index: 1,
        status: "final",
        autosave_count: 1,
        payload: finalPayload,
        last_saved_at: "2026-07-15T09:32:00+08:00",
        submitted_at: "2026-07-15T09:32:00+08:00",
        can_start_experiment: true,
      },
    });
  });

  await page.goto("/pretest");
  await expect(page.getByText("已恢复上次保存的问卷。")).toBeVisible();

  const q27 = page.locator('[data-pretest-item-id="q27"] input[type="range"]');
  await q27.dispatchEvent("pointerdown");
  await page.getByRole("button", { name: "进入实验" }).click();

  await expect.poll(() => finalPayload).not.toBeNull();
  expect(
    ((finalPayload?.slider_touch_state as Record<string, boolean> | undefined) ?? {}).q27,
  ).toBe(true);
});
