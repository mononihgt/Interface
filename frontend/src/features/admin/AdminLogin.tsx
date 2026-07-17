import { LogIn } from "lucide-react";
import { useState } from "react";

import { apiClient, ApiError } from "../../api/client";

interface AdminLoginProps {
  onAuthenticated: (adminUser: string) => void;
}

export function AdminLogin({ onAuthenticated }: AdminLoginProps) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const submitLogin = async () => {
    setIsSubmitting(true);
    setErrorMessage(null);
    try {
      const result = await apiClient.adminLogin({ username, password });
      onAuthenticated(result.admin_user);
    } catch (error) {
      setErrorMessage(error instanceof ApiError ? error.detail : "登录失败。");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <main className="admin-login-page">
      <form
        className="admin-login-panel"
        onSubmit={(event) => {
          event.preventDefault();
          void submitLogin();
        }}
      >
        <div>
          <p className="admin-kicker">Admin</p>
          <h1>实验 Dashboard 登录</h1>
        </div>
        <label className="admin-field">
          <span>用户名</span>
          <input
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
          />
        </label>
        <label className="admin-field">
          <span>密码</span>
          <input
            value={password}
            type="password"
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {errorMessage ? (
          <p className="admin-inline-error">{errorMessage}</p>
        ) : null}
        <button
          className="admin-primary-button"
          type="submit"
          disabled={isSubmitting || !username.trim() || !password}
        >
          <LogIn size={16} />
          <span>{isSubmitting ? "登录中" : "登录"}</span>
        </button>
      </form>
    </main>
  );
}
