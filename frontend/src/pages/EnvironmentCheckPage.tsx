import { useEffect, useState } from "react";

import {
  DesktopGate,
  getDesktopGateState,
  type DesktopGateState,
} from "../components/DesktopGate";

interface EnvironmentCheckPageProps {
  onPassed: (state: DesktopGateState) => void;
}

export function EnvironmentCheckPage({ onPassed }: EnvironmentCheckPageProps) {
  const [state, setState] = useState<DesktopGateState | null>(null);

  useEffect(() => {
    void getDesktopGateState().then(setState);
  }, []);

  const passed = Boolean(state?.isFormalReady);

  return (
    <main className="flow-page">
      <section className="flow-card flow-card--gate">
        <h1 className="flow-title">实验环境检测</h1>
        <p className="flow-message">请先完成实验环境检测，检测通过后再进入问卷和正式实验。</p>
        <DesktopGate onChange={setState} />
        <div className="flow-actions">
          <button
            className="primary-button"
            type="button"
            disabled={!passed || !state}
            onClick={() => {
              if (state) {
                onPassed(state);
              }
            }}
          >
            继续
          </button>
        </div>
      </section>
    </main>
  );
}
