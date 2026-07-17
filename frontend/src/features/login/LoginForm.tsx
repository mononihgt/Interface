import { useState } from "react";
import { CheckCircle2 } from "lucide-react";

import { apiClient } from "../../api/client";
import type { ParticipantView } from "../../api/types";
import { interfaceCopy } from "../../experiment/interfaceCopy";
import { validateLoginFields } from "./loginValidation";
import { formatWelcomeLoginError } from "./welcomeErrorMessages";

export interface LoginBeforeSubmitResult {
  passed: boolean;
  message?: string | null;
}

interface LoginFormProps {
  onSuccess: (participant: ParticipantView) => void;
  onBeforeLogin?: () => Promise<LoginBeforeSubmitResult>;
  onGoTest?: () => void;
  onGoAdmin?: () => void;
}

export function LoginForm({ onSuccess, onBeforeLogin, onGoTest, onGoAdmin }: LoginFormProps) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setErrorMessage(null);
    const validationMessage = validateLoginFields({ name, phone });
    if (validationMessage) {
      setErrorMessage(validationMessage);
      return;
    }
    setIsSubmitting(true);

    try {
      if (onBeforeLogin) {
        const gate = await onBeforeLogin();
        if (!gate.passed) {
          const message = gate.message ?? "当前环境不满足正式实验要求。";
          console.info(message);
          setErrorMessage(message);
          return;
        }
      }

      const participant = await apiClient.login({
        name,
        phone,
      });
      if (
        participant.participation_state === "blocked" ||
        participant.participation_state === "not_scheduled_today"
      ) {
        const message =
          participant.participation_message ?? "当前无法继续实验，请联系研究人员。";
        console.info(message);
        setErrorMessage(participant.participation_message ?? message);
        return;
      }
      onSuccess(participant);
    } catch (error) {
      setErrorMessage(formatWelcomeLoginError(error));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <form className="flow-card login-flow-card" onSubmit={submit} noValidate>
      <div className="flow-logo" aria-hidden="true">
        <CheckCircle2 size={34} />
      </div>
      <h1 className="flow-title">{interfaceCopy.login.title}</h1>
      <p className="flow-subtitle">{interfaceCopy.login.subtitle}</p>

      <label className="field">
        <span className="sr-only">姓名</span>
        <input
          value={name}
          onChange={(event) => setName(event.target.value)}
          aria-required="true"
          placeholder={interfaceCopy.login.namePlaceholder}
        />
      </label>

      <label className="field">
        <span className="sr-only">支付宝绑定手机号</span>
        <input
          value={phone}
          onChange={(event) => setPhone(event.target.value.replace(/\D+/g, "").slice(0, 11))}
          inputMode="tel"
          autoComplete="tel"
          aria-required="true"
          placeholder={interfaceCopy.login.phonePlaceholder}
        />
      </label>

      {errorMessage ? <p className="status-inline status-inline--error">{errorMessage}</p> : null}

      <button className="primary-button" type="submit" disabled={isSubmitting}>
        <span>{isSubmitting ? interfaceCopy.login.submitting : interfaceCopy.login.submit}</span>
      </button>

      {onGoTest || onGoAdmin ? (
        <div className="login-entry-row" aria-label="研究人员入口">
          {onGoTest ? (
            <button
              className="small-ghost-button"
              type="button"
              onClick={onGoTest}
              aria-label="测试入口"
            >
              {interfaceCopy.login.testEntry}
            </button>
          ) : null}
          {onGoAdmin ? (
            <button
              className="small-ghost-button"
              type="button"
              onClick={onGoAdmin}
              aria-label="管理入口"
            >
              {interfaceCopy.login.adminEntry}
            </button>
          ) : null}
        </div>
      ) : null}

      <div className="flow-footer">{interfaceCopy.login.footer}</div>
    </form>
  );
}
