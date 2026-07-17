from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agents.structured import normalize_semantic_failure_code


class SemanticLoopTimeout(TimeoutError):
    pass


class SemanticAttemptResult(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    final_value: Any
    mutation_applied: bool
    evaluator_presented: bool
    failure_reason: str | None = None
    evaluator_status: str
    evaluator_parse_attempts: int = Field(ge=0)
    structured_parse_attempts: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    route: str | None = None
    provider_status: str | None = None
    route_attempt_count: int = Field(ge=0)
    retry_feedback: str | None = None


class SemanticAttemptAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_no: int = Field(ge=1, le=5)
    failure_reason: str | None = None
    mutation_applied: bool
    evaluator_status: str
    evaluator_parse_attempts: int = Field(ge=0)
    structured_parse_attempts: int = Field(ge=0)
    provider: str | None = None
    model: str | None = None
    route: str | None = None
    provider_status: str | None = None
    route_attempt_count: int = Field(ge=0)


@dataclass(frozen=True)
class ErrorPresentationOutcome:
    final_result: SemanticAttemptResult
    semantic_attempt_count: int
    attempts: list[SemanticAttemptAudit]
    manipulation_status: Literal["unknown", "pending", "presented", "failed"]
    failure_reason: str | None


AttemptRunner = Callable[[int, str | None], SemanticAttemptResult]
AsyncAttemptRunner = Callable[
    [int, str | None],
    Awaitable[SemanticAttemptResult],
]


class ErrorPresentationCoordinator:
    def __init__(
        self,
        *,
        max_semantic_attempts: int = 5,
        timeout_seconds: float = 120.0,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not 1 <= max_semantic_attempts <= 5:
            raise ValueError("max_semantic_attempts_must_be_between_1_and_5")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds_must_be_positive")
        self._max_semantic_attempts = max_semantic_attempts
        self._timeout_seconds = timeout_seconds
        self._clock = clock

    def run(
        self,
        *,
        error_planned: bool,
        error_type_id: str | None,
        attempt_runner: AttemptRunner,
        system_result: SemanticAttemptResult | None = None,
    ) -> ErrorPresentationOutcome:
        if error_planned and error_type_id == "system_failure":
            if system_result is None:
                raise ValueError("system_result_required")
            return ErrorPresentationOutcome(
                final_result=system_result,
                semantic_attempt_count=0,
                attempts=[],
                manipulation_status="presented",
                failure_reason=None,
            )

        started_at = self._clock()
        if not error_planned:
            result = attempt_runner(1, None)
            self._ensure_within_deadline(started_at)
            return ErrorPresentationOutcome(
                final_result=result,
                semantic_attempt_count=0,
                attempts=[],
                manipulation_status="unknown",
                failure_reason=None,
            )

        retry_feedback: str | None = None
        last_failure_reason: str | None = None
        audits: list[SemanticAttemptAudit] = []
        last_result: SemanticAttemptResult | None = None
        for attempt_no in range(1, self._max_semantic_attempts + 1):
            self._ensure_within_deadline(started_at)
            result = attempt_runner(attempt_no, retry_feedback)
            self._ensure_within_deadline(started_at)
            last_result = result
            failure_reason = self._failure_reason(result)
            if failure_reason != result.failure_reason:
                result = result.model_copy(update={"failure_reason": failure_reason})
                last_result = result
            last_failure_reason = failure_reason
            audits.append(self._audit(attempt_no, result))
            if failure_reason is None:
                return ErrorPresentationOutcome(
                    final_result=result,
                    semantic_attempt_count=attempt_no,
                    attempts=audits,
                    manipulation_status="presented",
                    failure_reason=None,
                )
            retry_feedback = result.retry_feedback or failure_reason

        assert last_result is not None
        return ErrorPresentationOutcome(
            final_result=last_result,
            semantic_attempt_count=self._max_semantic_attempts,
            attempts=audits,
            manipulation_status="failed",
            failure_reason=last_failure_reason or "semantic_attempts_exhausted",
        )

    async def run_async(
        self,
        *,
        error_planned: bool,
        error_type_id: str | None,
        attempt_runner: AsyncAttemptRunner,
        system_result: SemanticAttemptResult | None = None,
    ) -> ErrorPresentationOutcome:
        if error_planned and error_type_id == "system_failure":
            if system_result is None:
                raise ValueError("system_result_required")
            return ErrorPresentationOutcome(
                final_result=system_result,
                semantic_attempt_count=0,
                attempts=[],
                manipulation_status="presented",
                failure_reason=None,
            )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                if not error_planned:
                    result = await attempt_runner(1, None)
                    return ErrorPresentationOutcome(
                        final_result=result,
                        semantic_attempt_count=0,
                        attempts=[],
                        manipulation_status="unknown",
                        failure_reason=None,
                    )

                retry_feedback: str | None = None
                last_failure_reason: str | None = None
                audits: list[SemanticAttemptAudit] = []
                last_result: SemanticAttemptResult | None = None
                for attempt_no in range(1, self._max_semantic_attempts + 1):
                    result = await attempt_runner(attempt_no, retry_feedback)
                    last_result = result
                    failure_reason = self._failure_reason(result)
                    if failure_reason != result.failure_reason:
                        result = result.model_copy(
                            update={"failure_reason": failure_reason}
                        )
                        last_result = result
                    last_failure_reason = failure_reason
                    audits.append(self._audit(attempt_no, result))
                    if failure_reason is None:
                        return ErrorPresentationOutcome(
                            final_result=result,
                            semantic_attempt_count=attempt_no,
                            attempts=audits,
                            manipulation_status="presented",
                            failure_reason=None,
                        )
                    retry_feedback = result.retry_feedback or failure_reason

                assert last_result is not None
                return ErrorPresentationOutcome(
                    final_result=last_result,
                    semantic_attempt_count=self._max_semantic_attempts,
                    attempts=audits,
                    manipulation_status="failed",
                    failure_reason=last_failure_reason or "semantic_attempts_exhausted",
                )
        except TimeoutError as exc:
            raise SemanticLoopTimeout("semantic_loop_timeout") from exc

    def _ensure_within_deadline(self, started_at: float) -> None:
        if self._clock() - started_at > self._timeout_seconds:
            raise SemanticLoopTimeout("semantic_loop_timeout")

    @staticmethod
    def _failure_reason(result: SemanticAttemptResult) -> str | None:
        if result.failure_reason:
            return normalize_semantic_failure_code(
                result.failure_reason,
                default="semantic_attempt_failed",
            )
        if not result.mutation_applied:
            return "mutation_not_applied"
        if not result.evaluator_presented:
            return "evaluator_not_presented"
        return None

    @staticmethod
    def _audit(
        attempt_no: int,
        result: SemanticAttemptResult,
    ) -> SemanticAttemptAudit:
        return SemanticAttemptAudit(
            attempt_no=attempt_no,
            failure_reason=result.failure_reason,
            mutation_applied=result.mutation_applied,
            evaluator_status=result.evaluator_status,
            evaluator_parse_attempts=result.evaluator_parse_attempts,
            structured_parse_attempts=result.structured_parse_attempts,
            provider=result.provider,
            model=result.model,
            route=result.route,
            provider_status=result.provider_status,
            route_attempt_count=result.route_attempt_count,
        )
