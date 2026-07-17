from __future__ import annotations

from collections.abc import Callable
import asyncio

import pytest

from backend.app.agents.error_loop import (
    ErrorPresentationCoordinator,
    SemanticAttemptResult,
    SemanticLoopTimeout,
)


def _attempt(
    *,
    presented: bool,
    failure_reason: str | None = None,
    evaluator_parse_attempts: int = 1,
    structured_parse_attempts: int = 1,
    provider: str = "provider-a",
    route_attempt_count: int = 1,
    retry_feedback: str | None = None,
) -> SemanticAttemptResult:
    return SemanticAttemptResult(
        final_value={"safe": True},
        mutation_applied=True,
        evaluator_presented=presented,
        failure_reason=failure_reason,
        evaluator_status="success" if presented else "failed",
        evaluator_parse_attempts=evaluator_parse_attempts,
        structured_parse_attempts=structured_parse_attempts,
        provider=provider,
        model="model-a",
        route="chat",
        provider_status="success",
        route_attempt_count=route_attempt_count,
        retry_feedback=retry_feedback,
    )


def _runner(results: list[SemanticAttemptResult]) -> Callable[[int, str | None], SemanticAttemptResult]:
    def run(attempt_no: int, feedback_code: str | None) -> SemanticAttemptResult:
        del feedback_code
        return results[attempt_no - 1]

    return run


def test_second_semantic_attempt_succeeds():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="factual_major",
        attempt_runner=_runner([
            _attempt(presented=False, failure_reason="evaluator_not_presented"),
            _attempt(presented=True),
        ]),
    )

    assert outcome.semantic_attempt_count == 2
    assert outcome.manipulation_status == "presented"
    assert outcome.failure_reason is None
    assert [item.failure_reason for item in outcome.attempts] == [
        "evaluator_not_presented",
        None,
    ]


def test_fifth_semantic_attempt_succeeds():
    failures = [
        _attempt(presented=False, failure_reason="evaluator_not_presented")
        for _ in range(4)
    ]
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="logic_major",
        attempt_runner=_runner([*failures, _attempt(presented=True)]),
    )

    assert outcome.semantic_attempt_count == 5
    assert outcome.manipulation_status == "presented"


def test_five_failures_return_last_safe_candidate_and_failed_status():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="factual_minor",
        attempt_runner=_runner([
            _attempt(presented=False, failure_reason="schema_validation_error")
            for _ in range(5)
        ]),
    )

    assert outcome.semantic_attempt_count == 5
    assert outcome.manipulation_status == "failed"
    assert outcome.failure_reason == "schema_validation_error"
    assert outcome.final_result.final_value == {"safe": True}


def test_evaluator_parse_retries_do_not_increment_semantic_attempt():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="logic_minor",
        attempt_runner=_runner([
            _attempt(presented=True, evaluator_parse_attempts=3)
        ]),
    )

    assert outcome.semantic_attempt_count == 1
    assert outcome.attempts[0].evaluator_parse_attempts == 3


def test_provider_route_failover_does_not_increment_semantic_attempt():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="factual_major",
        attempt_runner=_runner([
            _attempt(presented=True, provider="provider-b", route_attempt_count=2)
        ]),
    )

    assert outcome.semantic_attempt_count == 1
    assert outcome.attempts[0].route_attempt_count == 2
    assert outcome.attempts[0].provider == "provider-b"


def test_structured_parse_retry_does_not_increment_semantic_attempt():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="factual_minor",
        attempt_runner=_runner([
            _attempt(presented=True, structured_parse_attempts=2)
        ]),
    )

    assert outcome.semantic_attempt_count == 1
    assert outcome.attempts[0].structured_parse_attempts == 2


def test_system_failure_has_zero_semantic_attempts_and_never_calls_runner():
    def unexpected(_attempt_no: int, _feedback_code: str | None) -> SemanticAttemptResult:
        raise AssertionError("system failure must not execute semantic attempts")

    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="system_failure",
        attempt_runner=unexpected,
        system_result=_attempt(presented=True, provider="local-system"),
    )

    assert outcome.semantic_attempt_count == 0
    assert outcome.manipulation_status == "presented"


def test_nonplanned_turn_runs_once_without_semantic_audit():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=False,
        error_type_id=None,
        attempt_runner=_runner([_attempt(presented=False)]),
    )

    assert outcome.semantic_attempt_count == 0
    assert outcome.attempts == []
    assert outcome.failure_reason is None
    assert outcome.manipulation_status == "unknown"


def test_total_timeout_raises_without_returning_partial_result():
    times = iter([0.0, 0.0, 2.0])

    with pytest.raises(SemanticLoopTimeout, match="semantic_loop_timeout"):
        ErrorPresentationCoordinator(timeout_seconds=1.0, clock=lambda: next(times)).run(
            error_planned=True,
            error_type_id="logic_major",
            attempt_runner=_runner([
                _attempt(presented=False, failure_reason="evaluator_not_presented")
            ]),
        )


def test_attempt_audit_excludes_candidate_prompt_and_raw_feedback():
    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="logic_minor",
        attempt_runner=_runner([_attempt(presented=True)]),
    )

    audit = outcome.attempts[0].model_dump(mode="json")
    assert set(audit) == {
        "attempt_no",
        "failure_reason",
        "mutation_applied",
        "evaluator_status",
        "evaluator_parse_attempts",
        "structured_parse_attempts",
        "provider",
        "model",
        "route",
        "provider_status",
        "route_attempt_count",
    }


def test_stable_failure_code_is_forwarded_to_next_semantic_attempt():
    observed_feedback: list[str | None] = []

    def run(attempt_no: int, feedback_code: str | None) -> SemanticAttemptResult:
        observed_feedback.append(feedback_code)
        return _attempt(
            presented=attempt_no == 2,
            failure_reason=(
                None if attempt_no == 2 else "structured_mutation_invalid"
            ),
        )

    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="logic_minor",
        attempt_runner=run,
    )

    assert outcome.semantic_attempt_count == 2
    assert observed_feedback == [None, "structured_mutation_invalid"]


def test_detailed_retry_feedback_is_forwarded_but_excluded_from_audit():
    observed_feedback: list[str | None] = []

    def run(attempt_no: int, feedback: str | None) -> SemanticAttemptResult:
        observed_feedback.append(feedback)
        return _attempt(
            presented=attempt_no == 2,
            failure_reason=None if attempt_no == 2 else "evaluator_not_presented",
            retry_feedback=(
                None if attempt_no == 2 else "候选没有改变用户明确给出的日期。"
            ),
        )

    outcome = ErrorPresentationCoordinator().run(
        error_planned=True,
        error_type_id="factual_minor",
        attempt_runner=run,
    )

    assert observed_feedback == [None, "候选没有改变用户明确给出的日期。"]
    assert "retry_feedback" not in outcome.attempts[0].model_dump(mode="json")


def test_async_total_deadline_cancels_running_semantic_attempt():
    cancelled = asyncio.Event()

    async def run(
        _attempt_no: int,
        _feedback_code: str | None,
    ) -> SemanticAttemptResult:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("attempt should have been cancelled")

    async def exercise() -> None:
        with pytest.raises(SemanticLoopTimeout, match="semantic_loop_timeout"):
            await ErrorPresentationCoordinator(timeout_seconds=0.01).run_async(
                error_planned=True,
                error_type_id="logic_major",
                attempt_runner=run,
            )
        assert cancelled.is_set()

    asyncio.run(exercise())
