import { useState } from "react";
import { CheckCircle2 } from "lucide-react";

import { apiClient, ApiError } from "../api/client";

interface TestLoginPageProps {
  onAuthenticated: () => void;
  onBack: () => void;
}

export function TestLoginPage({
  onAuthenticated,
  onBack,
}: TestLoginPageProps) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setErrorMessage(null);
    setIsSubmitting(true);
    try {
      await apiClient.adminLogin({ username, password });
      onAuthenticated();
    } catch (error) {
      setErrorMessage(error instanceof ApiError ? error.detail : "认证失败。");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="flow-page">
      <form className="flow-card flow-card--test test-login-card" onSubmit={submit}>
        <div className="flow-logo" aria-hidden="true">
          <CheckCircle2 size={34} />
        </div>
        <h1 className="flow-title">研究人员测试通道</h1>
        <p className="flow-message">测试通道使用与后台管理相同的用户名和密码。</p>
        <label className="field">
          <span>用户名</span>
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
          />
        </label>
        <label className="field">
          <span>密码</span>
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>
        {errorMessage ? (
          <p className="status-inline status-inline--error">{errorMessage}</p>
        ) : null}
        <div className="flow-actions flow-actions--stack">
          <button className="primary-button" type="submit" disabled={isSubmitting}>
            {isSubmitting ? "进入中" : "进入测试通道"}
          </button>
          <button className="small-ghost-button" type="button" onClick={onBack}>
            返回入口
          </button>
        </div>
      </form>
    </main>
  );
}
