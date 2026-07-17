export interface LoginFields {
  name: string;
  phone: string;
}

export function validateLoginFields({ name, phone }: LoginFields): string | null {
  if (!name.trim()) {
    return "请输入有效的姓名。";
  }
  if (!phone.trim()) {
    return "请输入有效的中国大陆手机号码。";
  }
  return null;
}
