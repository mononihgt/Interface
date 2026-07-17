import type { RecruitmentStatusView } from "../../api/types";

interface RecruitmentUpdateClient {
  setRecruitment: (open: boolean) => Promise<RecruitmentStatusView>;
  getRecruitmentStatus: () => Promise<RecruitmentStatusView>;
}

export interface RecruitmentUpdateResult {
  status: RecruitmentStatusView | null;
  errorMessage: string | null;
}

export async function reconcileRecruitmentUpdate(
  open: boolean,
  client: RecruitmentUpdateClient,
): Promise<RecruitmentUpdateResult> {
  try {
    await client.setRecruitment(open);
  } catch {}

  try {
    const status = await client.getRecruitmentStatus();
    return {
      status,
      errorMessage:
        status.accepting_new_participants !== open
          ? "正式招募状态更新失败，请稍后重试。"
          : null,
    };
  } catch {
    return {
      status: null,
      errorMessage: "暂时无法确认正式招募状态，请刷新后重试。",
    };
  }
}
