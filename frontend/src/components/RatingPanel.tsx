import { useMemo, useState } from "react";

import type { TurnView } from "../api/types";
import { interfaceCopy } from "../experiment/interfaceCopy";

interface RatingPanelProps {
  turn: TurnView;
  disabled?: boolean;
  onSubmit: (payload: { stance_score: number; trust_score: number; client_elapsed_ms: number }) => Promise<void>;
}

export function RatingPanel({ turn, disabled = false, onSubmit }: RatingPanelProps) {
  const [stance, setStance] = useState<number | null>(turn.rating?.stance_score ?? null);
  const [trust, setTrust] = useState<number | null>(turn.rating?.trust_score ?? null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [startedAt] = useState(() => Date.now());

  const isComplete = useMemo(() => stance !== null && trust !== null, [stance, trust]);

  const submit = async () => {
    if (!isComplete || disabled || turn.rating) {
      return;
    }

    setIsSubmitting(true);
    setErrorMessage(null);

    try {
      await onSubmit({
        stance_score: stance!,
        trust_score: trust!,
        client_elapsed_ms: Date.now() - startedAt,
      });
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : "评分提交失败。");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <section className="panel rating-panel">
      <div className="panel-heading">
        <div>
          <h2>{interfaceCopy.rating.title}</h2>
        </div>
      </div>

      <div className="rating-grid">
        <div className="rating-group">
          <span>{interfaceCopy.rating.stanceQuestion}</span>
          <div className="score-row" role="radiogroup" aria-label="印象评分">
            {[1, 2, 3, 4, 5].map((value) => (
              <button
                className={`score-button${stance === value ? " is-selected" : ""}`}
                type="button"
                key={value}
                onClick={() => setStance(value)}
                disabled={disabled || Boolean(turn.rating)}
              >
                {value}
              </button>
            ))}
          </div>
        </div>
        <div className="rating-group">
          <span>{interfaceCopy.rating.trustQuestion}</span>
          <div className="score-row" role="radiogroup" aria-label="信任评分">
            {[1, 2, 3, 4, 5, 6, 7].map((value) => (
              <button
                className={`score-button${trust === value ? " is-selected" : ""}`}
                type="button"
                key={value}
                onClick={() => setTrust(value)}
                disabled={disabled || Boolean(turn.rating)}
              >
                {value}
              </button>
            ))}
          </div>
        </div>
      </div>

      {turn.rating ? <p className="muted">该轮已完成评分。</p> : null}
      {errorMessage ? <p className="status-inline status-inline--error">{errorMessage}</p> : null}

      <button className="primary-button" type="button" onClick={() => void submit()} disabled={!isComplete || disabled || isSubmitting || Boolean(turn.rating)}>
        <span>{isSubmitting ? interfaceCopy.rating.submitting : interfaceCopy.rating.submit}</span>
      </button>
    </section>
  );
}
