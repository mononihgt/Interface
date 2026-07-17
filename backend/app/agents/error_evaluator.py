from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from backend.app.agents.error_protocol import (
    EvaluationHistoryMessage,
    build_evaluator_messages,
    parse_error_evaluation,
)
from backend.app.services.providers import ProviderMessage, ProviderResponse

if TYPE_CHECKING:
    from backend.app.agents.graph_base import ExperimentGraphState


EvaluatorRunner = Callable[[Sequence[ProviderMessage]], ProviderResponse]
AsyncEvaluatorRunner = Callable[
    [Sequence[ProviderMessage]],
    Awaitable[ProviderResponse],
]


@dataclass
class ErrorEvaluator:
    runner: EvaluatorRunner
    max_parse_attempts: int = 2

    def evaluate(
        self,
        *,
        state: "ExperimentGraphState",
        assistant_text: str,
        artifact_type: str | None,
        artifact_payload: dict[str, Any] | None,
        session_history: Sequence[EvaluationHistoryMessage] = (),
        current_user_text: str = "",
        weather_context: str | None = None,
    ) -> dict[str, Any]:
        messages = build_evaluator_messages(
            error_type_id=str(state.error_type_id),
            session_history=session_history,
            current_user_text=current_user_text,
            assistant_text=assistant_text,
            weather_context=weather_context,
            error_presentation=state.error_presentation,
            artifact_type=artifact_type,
            artifact_payload=artifact_payload,
        )
        attempts: list[dict[str, Any]] = []
        for parse_attempt in range(self.max_parse_attempts):
            response = self.runner(messages)
            attempts.append(self._attempt_evidence(response))
            unavailable = self._unavailable_result(
                response=response,
                attempts=attempts,
                parse_attempt=parse_attempt,
            )
            if unavailable is not None:
                return unavailable
            parsed = parse_error_evaluation(
                response.text,
                expected_error_type=str(state.error_type_id),
            )
            if parsed is not None:
                return self._success_result(
                    response=response,
                    attempts=attempts,
                    parse_attempt=parse_attempt,
                    presented=parsed.passed,
                    feedback_reason=parsed.feedback_reason,
                )
        return self._invalid_result(attempts)

    async def evaluate_async(
        self,
        *,
        runner: AsyncEvaluatorRunner,
        state: "ExperimentGraphState",
        assistant_text: str,
        artifact_type: str | None,
        artifact_payload: dict[str, Any] | None,
        session_history: Sequence[EvaluationHistoryMessage] = (),
        current_user_text: str = "",
        weather_context: str | None = None,
    ) -> dict[str, Any]:
        messages = build_evaluator_messages(
            error_type_id=str(state.error_type_id),
            session_history=session_history,
            current_user_text=current_user_text,
            assistant_text=assistant_text,
            weather_context=weather_context,
            error_presentation=state.error_presentation,
            artifact_type=artifact_type,
            artifact_payload=artifact_payload,
        )
        attempts: list[dict[str, Any]] = []
        for parse_attempt in range(self.max_parse_attempts):
            response = await runner(messages)
            attempts.append(self._attempt_evidence(response))
            unavailable = self._unavailable_result(
                response=response,
                attempts=attempts,
                parse_attempt=parse_attempt,
            )
            if unavailable is not None:
                return unavailable
            parsed = parse_error_evaluation(
                response.text,
                expected_error_type=str(state.error_type_id),
            )
            if parsed is not None:
                return self._success_result(
                    response=response,
                    attempts=attempts,
                    parse_attempt=parse_attempt,
                    presented=parsed.passed,
                    feedback_reason=parsed.feedback_reason,
                )
        return self._invalid_result(attempts)

    @staticmethod
    def _attempt_evidence(response: ProviderResponse) -> dict[str, Any]:
        return {
            "route": response.route,
            "provider": response.provider,
            "model": response.model,
            "used_local_fallback": response.used_local_fallback,
        }

    @staticmethod
    def _unavailable_result(
        *,
        response: ProviderResponse,
        attempts: list[dict[str, Any]],
        parse_attempt: int,
    ) -> dict[str, Any] | None:
        if not response.used_local_fallback:
            return None
        return {
            "status": "failed",
            "presented": False,
            "provider": response.provider,
            "model": response.model,
            "route": response.route,
            "parse_attempts": parse_attempt + 1,
            "attempts": attempts,
            "reason": "evaluator_local_fallback",
            "feedback_reason": None,
        }

    @staticmethod
    def _success_result(
        *,
        response: ProviderResponse,
        attempts: list[dict[str, Any]],
        parse_attempt: int,
        presented: bool,
        feedback_reason: str,
    ) -> dict[str, Any]:
        return {
            "status": "success",
            "presented": presented,
            "provider": response.provider,
            "model": response.model,
            "route": response.route,
            "parse_attempts": parse_attempt + 1,
            "attempts": attempts,
            "reason": "evaluator_presented" if presented else "evaluator_not_presented",
            "feedback_reason": feedback_reason,
        }

    def _invalid_result(self, attempts: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "status": "failed",
            "presented": False,
            "provider": attempts[-1]["provider"] if attempts else None,
            "model": attempts[-1]["model"] if attempts else None,
            "route": attempts[-1]["route"] if attempts else "evaluator",
            "parse_attempts": self.max_parse_attempts,
            "attempts": attempts,
            "reason": "invalid_evaluator_json",
            "feedback_reason": None,
        }
