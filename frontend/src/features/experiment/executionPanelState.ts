import type { ArtifactKind, ArtifactStatus } from "../../api/types";
import { safeExecutionArtifact, type SafeExecutionArtifact } from "./safeArtifact";

export type ExecutionPanelPhase =
  | "empty"
  | "loading"
  | "awaiting"
  | "success"
  | "failure";

interface ExecutionPanelStateInput {
  artifactKind: ArtifactKind;
  artifactStatus: ArtifactStatus;
  artifactPayload: unknown;
  isSubmitting: boolean;
  ratingPhase: boolean;
}

export interface ExecutionPanelState {
  phase: ExecutionPanelPhase;
  artifact: SafeExecutionArtifact | null;
  ratingPhase: boolean;
}

export function deriveExecutionPanelState({
  artifactKind,
  artifactStatus,
  artifactPayload,
  isSubmitting,
  ratingPhase,
}: ExecutionPanelStateInput): ExecutionPanelState {
  const artifact = safeExecutionArtifact(artifactKind, artifactPayload);

  if (isSubmitting) {
    return { phase: "loading", artifact, ratingPhase };
  }
  if (artifactStatus === "awaiting_input") {
    return { phase: "awaiting", artifact, ratingPhase };
  }
  if (artifactStatus === "failed") {
    return { phase: "failure", artifact, ratingPhase };
  }
  if (artifactStatus === "completed") {
    return {
      phase: artifact ? "success" : "failure",
      artifact,
      ratingPhase,
    };
  }
  return { phase: "empty", artifact, ratingPhase };
}
