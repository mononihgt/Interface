import type { ClientTimingSubmitRequest } from "../../api/types";

export interface ActiveClientResponseTiming {
  operationId: string;
  startedAtMs: number;
  clientMessageSentAt: string;
  interrupted: boolean;
}

export function startClientResponseTiming(
  operationId: string,
  performanceNow: () => number = () => performance.now(),
  wallClockNow: () => Date = () => new Date(),
): ActiveClientResponseTiming {
  return {
    operationId,
    startedAtMs: performanceNow(),
    clientMessageSentAt: wallClockNow().toISOString(),
    interrupted: false,
  };
}

export function markClientTimingInterrupted(timing: ActiveClientResponseTiming): void {
  timing.interrupted = true;
}

export function finishClientResponseTiming(
  timing: ActiveClientResponseTiming,
  performanceNow: () => number = () => performance.now(),
  wallClockNow: () => Date = () => new Date(),
): Readonly<ClientTimingSubmitRequest> {
  return Object.freeze({
    client_message_sent_at: timing.clientMessageSentAt,
    assistant_render_completed_at: wallClockNow().toISOString(),
    client_response_latency_ms: Math.max(
      0,
      Math.round(performanceNow() - timing.startedAtMs),
    ),
    client_timing_interrupted: timing.interrupted,
  });
}

export async function submitClientTimingWithRetry(
  submit: () => Promise<unknown>,
): Promise<void> {
  try {
    await submit();
  } catch {
    try {
      await submit();
    } catch {
      return;
    }
  }
}
