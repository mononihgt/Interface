import { CheckCircle2, Circle, Radio, Star } from "lucide-react";

import type { SessionView } from "../api/types";

interface TurnTimelineProps {
  session: SessionView;
}

export function TurnTimeline({ session }: TurnTimelineProps) {
  const expectedTurn = session.expected_turn_index ?? session.turns.length + 1;

  return (
    <aside className="panel timeline-panel">
      <div className="panel-heading">
        <div>
          <p className="panel-kicker">Session Rail</p>
          <h2>轮次进度</h2>
        </div>
      </div>

      <ol className="timeline-list">
        {Array.from({ length: 5 }, (_, index) => {
          const turnIndex = index + 1;
          const turn = session.turns.find((item) => item.turn_index === turnIndex);
          const isCurrent = turnIndex === expectedTurn && !turn;
          const isDone = Boolean(turn?.rating);
          const isAwaitingRating = Boolean(turn && !turn.rating);

          return (
            <li className={`timeline-item${isCurrent ? " is-current" : ""}`} key={turnIndex}>
              <div className="timeline-marker" aria-hidden="true">
                {isDone ? (
                  <CheckCircle2 size={16} />
                ) : isAwaitingRating ? (
                  <Star size={16} />
                ) : isCurrent ? (
                  <Radio size={16} />
                ) : (
                  <Circle size={16} />
                )}
              </div>
              <div className="timeline-content">
                <strong>第 {turnIndex} 轮</strong>
                <p>
                  {isDone
                    ? "已完成并评分"
                    : isAwaitingRating
                      ? "等待评分"
                      : isCurrent
                        ? "当前轮"
                        : "未开始"}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
