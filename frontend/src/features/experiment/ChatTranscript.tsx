import type { SessionView, TurnView } from "../../api/types";
import { RatingPanel } from "../../components/RatingPanel";
import { interfaceCopy } from "../../experiment/interfaceCopy";

export interface PendingUserMessage {
  operationId: string;
  turnIndex: number;
  text: string;
}

interface ChatTranscriptProps {
  session: SessionView;
  awaitingRatingTurn?: TurnView;
  pendingUserMessage?: PendingUserMessage | null;
  onSubmitRating: (
    turn: TurnView,
    payload: {
      stance_score: number;
      trust_score: number;
      client_elapsed_ms: number;
    },
  ) => Promise<void>;
}

export function ChatTranscript({
  session,
  awaitingRatingTurn,
  pendingUserMessage,
  onSubmitRating,
}: ChatTranscriptProps) {
  return (
    <section className="chat-area" id="chatArea" aria-label="对话记录">
      <article className="message message--assistant">
        <p>{interfaceCopy.experiment.initialAssistantMessage}</p>
      </article>
      {session.turns.map((turn) => (
        <div className="turn-pair" data-turn-id={turn.turn_id} key={turn.turn_id}>
          <article className="message message--user">
            <p>{turn.user_text}</p>
          </article>
          <article className="message message--assistant">
            <p>{turn.assistant_text}</p>
          </article>
          {awaitingRatingTurn?.turn_id === turn.turn_id ? (
            <RatingPanel
              turn={turn}
              onSubmit={(payload) => onSubmitRating(turn, payload)}
            />
          ) : null}
        </div>
      ))}
      {pendingUserMessage ? (
        <div
          className="turn-pair turn-pair--pending"
          data-operation-id={pendingUserMessage.operationId}
          data-turn-index={pendingUserMessage.turnIndex}
        >
          <article className="message message--user">
            <p>{pendingUserMessage.text}</p>
          </article>
          <article
            className="message message--assistant message--pending"
            role="status"
            aria-label="助手正在思考"
          >
            <span className="typing-dots" aria-hidden="true">
              <span />
              <span />
              <span />
            </span>
            <span className="sr-only">助手正在思考</span>
          </article>
        </div>
      ) : null}
    </section>
  );
}
