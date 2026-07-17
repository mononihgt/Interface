import type { PretestResponseView } from "../api/types";
import { PretestForm } from "../features/pretest/PretestForm";

interface PretestPageProps {
  onEnterExperiment: (response: PretestResponseView) => void | Promise<void>;
  enterExperimentError?: string | null;
}

export function PretestPage({
  onEnterExperiment,
  enterExperimentError,
}: PretestPageProps) {
  return (
    <main className="flow-page pretest-flow-page">
      <PretestForm
        onEnterExperiment={onEnterExperiment}
        enterExperimentError={enterExperimentError}
      />
    </main>
  );
}
