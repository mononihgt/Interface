import { CheckCircle2 } from "lucide-react";

import type { ParticipantView } from "../api/types";
import { interfaceCopy } from "../experiment/interfaceCopy";
import { LoginForm, type LoginBeforeSubmitResult } from "../features/login/LoginForm";

interface WelcomePageProps {
  participant: ParticipantView | null;
  onLoginSuccess: (participant: ParticipantView) => void;
  onBeforeLogin?: () => Promise<LoginBeforeSubmitResult>;
  onGoPretest: () => void;
  onStartFormal: () => void;
  onGoTest: () => void;
  formalStartDisabled: boolean;
}

export function WelcomePage({
  participant,
  onLoginSuccess,
  onBeforeLogin,
  onGoPretest,
  onStartFormal,
  onGoTest,
  formalStartDisabled,
}: WelcomePageProps) {
  const isLong = participant?.participant_type === "long";
  const dayIndex = participant?.current_day.day_index ?? 1;
  const isCompleted = participant?.participation_state === "completed";
  const isBlocked =
    participant?.participation_state === "blocked" ||
    participant?.participation_state === "not_scheduled_today";
  const blockedMessage =
    participant?.participation_message ??
    (participant?.participation_state === "blocked" && isLong
      ? interfaceCopy.welcome.blockedMissedLongTerm
      : "当前无法继续实验，请联系研究人员。");
  const shouldGoPretest =
    Boolean(participant) &&
    dayIndex === 1 &&
    participant?.pretest_status.has_final !== true;
  const startDisabled = shouldGoPretest ? false : formalStartDisabled;

  if (!participant) {
    return (
      <main className="flow-page">
        <LoginForm
          onSuccess={onLoginSuccess}
          onBeforeLogin={onBeforeLogin}
          onGoTest={onGoTest}
          onGoAdmin={() => {
            window.location.href = "/admin";
          }}
        />
      </main>
    );
  }

  return (
    <main className="flow-page">
      <section className="flow-card welcome-flow-card">
        <div className="flow-logo" aria-hidden="true">
          <CheckCircle2 size={34} />
        </div>
        <h1 className="flow-title">{interfaceCopy.welcome.title}</h1>
        <p className="flow-message">
          {interfaceCopy.welcome.message(participant.name)}
          {isLong ? (
            <span
              dangerouslySetInnerHTML={{
                __html: interfaceCopy.welcome.longTermMessage(dayIndex),
              }}
            />
          ) : null}
        </p>
        {isCompleted ? (
          <p className="status-inline">您已完成本次实验，感谢您的参与。</p>
        ) : isBlocked ? (
          <p className="status-inline status-inline--error">{blockedMessage}</p>
        ) : (
          <button
            className="primary-button"
            type="button"
            onClick={shouldGoPretest ? onGoPretest : onStartFormal}
            disabled={startDisabled}
          >
            {interfaceCopy.welcome.start}
          </button>
        )}
        <div className="flow-footer">浙江大学 · 人机交互研究</div>
      </section>
    </main>
  );
}
