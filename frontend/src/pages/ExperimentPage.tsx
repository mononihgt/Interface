import type { SessionView } from "../api/types";
import { navigate } from "../app/routes";
import { ExperimentShell } from "../features/experiment/ExperimentShell";

interface ExperimentPageProps {
  session: SessionView;
  onSessionChange: (session: SessionView) => void;
  onComplete: () => Promise<void>;
}

export function ExperimentPage({ session, onSessionChange, onComplete }: ExperimentPageProps) {
  const goBackToTest = () => {
    navigate("/test");
  };

  return (
    <ExperimentShell
      session={session}
      onSessionChange={onSessionChange}
      onComplete={onComplete}
      onBackToTest={session.is_test ? goBackToTest : undefined}
    />
  );
}
