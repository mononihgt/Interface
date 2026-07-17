import { useEffect, useMemo, useRef, useState } from "react";

import { apiClient, requiresNewOperationId } from "../../api/client";
import type {
  AsrView,
  RatingSubmitRequest,
  SessionView,
  TurnSubmitRequest,
  TurnView,
} from "../../api/types";
import { interfaceCopy } from "../../experiment/interfaceCopy";
import { TestVoiceTurnInput } from "../test/TestVoiceTurnInput";
import { ChatTranscript } from "./ChatTranscript";
import type { PendingUserMessage } from "./ChatTranscript";
import {
  finishClientResponseTiming,
  markClientTimingInterrupted,
  startClientResponseTiming,
  submitClientTimingWithRetry,
  type ActiveClientResponseTiming,
} from "./clientResponseTiming";
import { ExecutionPanel } from "./ExecutionPanel";
import { TaskCard } from "./TaskCard";
import { VoiceTurnInput } from "./VoiceTurnInput";

interface ExperimentShellProps {
  session: SessionView;
  onSessionChange: (session: SessionView) => void;
  onComplete: () => Promise<void>;
  onBackToTest?: () => void;
}

interface PendingRenderTiming {
  turnId: number;
  timing: ActiveClientResponseTiming;
}

export function ExperimentShell({
  session,
  onSessionChange,
  onComplete,
  onBackToTest,
}: ExperimentShellProps) {
  const [isSubmittingTurn, setIsSubmittingTurn] = useState(false);
  const [testText, setTestText] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [pendingUserMessage, setPendingUserMessage] =
    useState<PendingUserMessage | null>(null);
  const [pendingRenderTiming, setPendingRenderTiming] =
    useState<PendingRenderTiming | null>(null);
  const testTextOperationIdRef = useRef<string | null>(null);
  const activeResponseTimingRef = useRef<ActiveClientResponseTiming | null>(null);

  const awaitingRatingTurn = useMemo(
    () => [...session.turns].reverse().find((turn) => !turn.rating),
    [session.turns],
  );
  const isExecution = session.presentation_mode === "execution";
  const artifactPayload = session.artifact_payload;
  const activeTurnIndex = session.expected_turn_index ?? session.turns.length + 1;

  useEffect(() => {
    const handleVisibilityChange = () => {
      const activeTiming = activeResponseTimingRef.current;
      if (activeTiming && document.visibilityState !== "visible") {
        markClientTimingInterrupted(activeTiming);
      }
    };
    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  useEffect(() => {
    if (!pendingRenderTiming) {
      return;
    }
    const turnElement = document.querySelector(
      `[data-turn-id="${pendingRenderTiming.turnId}"] .message--assistant`,
    );
    if (!turnElement) {
      return;
    }
    if (isExecution) {
      const executionPanel = document.querySelector(
        "#toolActionPanel [data-execution-phase]",
      );
      const phase = executionPanel?.getAttribute("data-execution-phase");
      if (!phase || !["awaiting", "success", "failure"].includes(phase)) {
        return;
      }
    }

    let secondFrame: number | null = null;
    const firstFrame = requestAnimationFrame(() => {
      secondFrame = requestAnimationFrame(() => {
        const payload = finishClientResponseTiming(pendingRenderTiming.timing);
        if (activeResponseTimingRef.current === pendingRenderTiming.timing) {
          activeResponseTimingRef.current = null;
        }
        setPendingRenderTiming(null);
        void submitClientTimingWithRetry(() =>
          apiClient.submitClientTiming(pendingRenderTiming.turnId, payload),
        );
      });
    });
    return () => {
      cancelAnimationFrame(firstFrame);
      if (secondFrame !== null) {
        cancelAnimationFrame(secondFrame);
      }
    };
  }, [isExecution, pendingRenderTiming, session]);

  const submitPendingTurn = async (
    payload: TurnSubmitRequest,
    pendingMessage: PendingUserMessage,
    fallbackError: string,
  ) => {
    setIsSubmittingTurn(true);
    setSubmitError(null);
    setPendingUserMessage(pendingMessage);
    const responseTiming = startClientResponseTiming(
      pendingMessage.operationId,
    );
    if (document.visibilityState !== "visible") {
      markClientTimingInterrupted(responseTiming);
    }
    activeResponseTimingRef.current = responseTiming;

    try {
      let submittedTurn: TurnView;
      try {
        submittedTurn = await apiClient.submitTurn(payload);
      } catch (error) {
        if (activeResponseTimingRef.current === responseTiming) {
          activeResponseTimingRef.current = null;
        }
        setPendingUserMessage(null);
        setSubmitError(error instanceof Error ? error.message : fallbackError);
        throw error;
      }

      onSessionChange({
        ...session,
        turns: [
          ...session.turns.filter((turn) => turn.turn_id !== submittedTurn.turn_id),
          submittedTurn,
        ].sort((left, right) => left.turn_index - right.turn_index),
      });
      setPendingUserMessage(null);
      if (!isExecution) {
        setPendingRenderTiming({
          turnId: submittedTurn.turn_id,
          timing: responseTiming,
        });
      }

      try {
        const canonicalSession = await apiClient.getSession(session.session_id);
        onSessionChange(canonicalSession);
        if (isExecution) {
          setPendingRenderTiming({
            turnId: submittedTurn.turn_id,
            timing: responseTiming,
          });
        }
      } catch (error) {
        if (isExecution && activeResponseTimingRef.current === responseTiming) {
          activeResponseTimingRef.current = null;
        }
        setSubmitError(error instanceof Error ? error.message : "会话状态刷新失败。");
      }
    } finally {
      setIsSubmittingTurn(false);
    }
  };

  const submitTurnFromAsr = async (recognizedAsr: AsrView, operationId: string) => {
    if (!recognizedAsr.asr_text) {
      return;
    }

    const payload: TurnSubmitRequest = {
      session_id: session.session_id,
      operation_id: operationId,
      turn_index: activeTurnIndex,
      input_mode: "voice",
      asr_result_id: recognizedAsr.asr_result_id,
    };

    await submitPendingTurn(
      payload,
      {
        operationId,
        turnIndex: activeTurnIndex,
        text: recognizedAsr.asr_text,
      },
      "语音轮次提交失败。",
    );
  };

  const submitTestTextTurn = async () => {
    const trimmed = testText.trim();
    if (!trimmed) {
      return;
    }

    const operationId = testTextOperationIdRef.current ?? crypto.randomUUID();
    testTextOperationIdRef.current = operationId;
    setTestText("");

    try {
      await submitPendingTurn(
        {
          session_id: session.session_id,
          operation_id: operationId,
          turn_index: activeTurnIndex,
          input_mode: "text_test_only",
          user_text: trimmed,
        },
        {
          operationId,
          turnIndex: activeTurnIndex,
          text: trimmed,
        },
        "文本轮次提交失败。",
      );
      testTextOperationIdRef.current = null;
    } catch (error) {
      setTestText(trimmed);
      if (requiresNewOperationId(error)) {
        testTextOperationIdRef.current = null;
      }
    }
  };

  const handleCompletedSession = async (completedSession: SessionView) => {
    if (completedSession.is_test) {
      onSessionChange(completedSession);
    } else {
      await onComplete();
    }
  };

  const submitRating = async (turn: TurnView, payload: RatingSubmitRequest) => {
    try {
      const result = await apiClient.submitRating(turn.turn_id, payload);
      if ("status" in result) {
        await handleCompletedSession(result);
        return;
      }

      const nextSession = await apiClient.getSession(session.session_id);
      onSessionChange(nextSession);
    } catch (error) {
      if (turn.turn_index !== 5 || session.turns.length !== 5) {
        throw error;
      }

      try {
        const recoveredSession = await apiClient.completeSession(session.session_id);
        await handleCompletedSession(recoveredSession);
      } catch {
        throw error;
      }
    }
  };

  const submitVoiceTurn = async (recognizedAsr: AsrView, operationId: string) => {
    await submitTurnFromAsr(recognizedAsr, operationId);
  };

  return (
    <div
      className={`container${isExecution ? " execution-panel-active" : ""}`}
      id="chatContainer"
    >
      <div className="topic-fixed" id="topicFixed">
        {onBackToTest ? (
          <div className="experiment-top-actions">
            <button className="secondary-button" type="button" onClick={onBackToTest}>
              {interfaceCopy.experiment.returnToTest}
            </button>
          </div>
        ) : null}
        <TaskCard
          topicKey={session.topic_key}
          title={session.topic_title}
          description={session.topic_description}
        />
      </div>

      <div className="chat-workspace" id="chatWorkspace">
        <main className="chat-main">
          <ChatTranscript
            session={session}
            awaitingRatingTurn={awaitingRatingTurn}
            pendingUserMessage={pendingUserMessage}
            onSubmitRating={submitRating}
          />
          {!awaitingRatingTurn && session.turns.length < 5 ? (
            session.is_test ? (
              <>
                <section className="text-input-area" aria-label="测试文本输入">
                  <div className="text-input-container">
                    <input
                      className="text-input"
                      value={testText}
                      onChange={(event) => {
                        testTextOperationIdRef.current = null;
                        setTestText(event.target.value);
                      }}
                      placeholder={interfaceCopy.experiment.textInputPlaceholder}
                      disabled={isSubmittingTurn || session.status !== "started"}
                    />
                    <button
                      className="send-button"
                      type="button"
                      onClick={() => void submitTestTextTurn()}
                      disabled={
                        isSubmittingTurn || session.status !== "started" || !testText.trim()
                      }
                    >
                      {interfaceCopy.experiment.send}
                    </button>
                  </div>
                </section>
                <TestVoiceTurnInput
                  sessionId={session.session_id}
                  turnIndex={activeTurnIndex}
                  disabled={isSubmittingTurn || session.status !== "started"}
                  isSubmitting={isSubmittingTurn}
                  onSubmit={submitVoiceTurn}
                />
              </>
            ) : (
              <VoiceTurnInput
                sessionId={session.session_id}
                turnIndex={activeTurnIndex}
                disabled={isSubmittingTurn || session.status !== "started"}
                isSubmitting={isSubmittingTurn}
                onSubmit={submitVoiceTurn}
              />
            )
          ) : null}
          {submitError ? <p className="status-inline status-inline--error">{submitError}</p> : null}
        </main>

        <aside className="tool-action-panel" id="toolActionPanel" aria-label="助手任务执行状态">
          {isExecution ? (
            <ExecutionPanel
              topicTitle={session.topic_title}
              artifactKind={session.artifact_kind}
              artifactStatus={session.artifact_status}
              payload={artifactPayload}
              isSubmitting={isSubmittingTurn}
              ratingPhase={Boolean(awaitingRatingTurn)}
            />
          ) : null}
        </aside>
      </div>
    </div>
  );
}
