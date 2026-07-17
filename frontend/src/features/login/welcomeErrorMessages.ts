import { ApiError } from "../../api/client";

const PHONE_MESSAGE = "请输入有效的中国大陆手机号码。";
const NAME_MESSAGE = "请输入有效的姓名。";
const RECRUITMENT_MESSAGE = "正式实验招募暂未开放，请稍后再试。";

export function formatWelcomeLoginError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "recruitment_closed") return RECRUITMENT_MESSAGE;
    if (error.fieldErrors?.phone) return PHONE_MESSAGE;
    if (error.fieldErrors?.name) return NAME_MESSAGE;
    if (error.status === 408) return "请求超时，请稍后重试。";
    if (error.status >= 500) return "服务暂时不可用，请稍后重试。";
    return "登录信息有误，请检查后重试。";
  }
  if (error instanceof DOMException && error.name === "AbortError") {
    return "请求超时，请稍后重试。";
  }
  if (error instanceof TypeError) {
    return "网络连接失败，请检查网络后重试。";
  }
  return "登录失败，请稍后重试。";
}
