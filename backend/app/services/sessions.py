from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import hashlib
from pathlib import Path
import random
import secrets
import re
import sqlite3
from time import perf_counter
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, status

from backend.app.agents.candidates import (
    ResponseCandidate,
    project_participant_artifact,
    validate_candidate_artifact,
)
from backend.app.agents.error_evaluator import ErrorEvaluator
from backend.app.agents.error_protocol import (
    build_generation_messages,
    contains_error_policy_disclosure,
)
from backend.app.agents.error_loop import (
    ErrorPresentationCoordinator,
    SemanticAttemptResult,
    SemanticLoopTimeout,
)
from backend.app.agents.error_injection import (
    generation_fallback_prevents_error_presentation,
)
from backend.app.agents.chat import build_chat_graph
from backend.app.agents.decision import build_decision_graph
from backend.app.agents.execution import build_execution_graph
from backend.app.agents.execution_workspace import (
    build_execution_workspace_messages,
    build_local_execution_artifact,
    normalize_execution_payload,
    resolve_execution_result,
)
from backend.app.agents.graph_base import (
    ConversationMessage,
    GraphInput,
    GraphRunResult,
    build_graph_state,
)
from backend.app.agents.planning import build_planning_graph
from backend.app.agents.qa import build_qa_graph
from backend.app.agents.structured import (
    ERROR_SEVERITY_BY_TYPE,
    ErrorMutation,
    StructuredAgentResult,
    normalize_semantic_failure_code,
    schema_for_artifact,
)
from backend.app.models.api import (
    AsrView,
    ClientInfo,
    RatingSubmitRequest,
    RatingView,
    SessionStartRequest,
    SessionView,
    TurnRatingView,
    TurnSubmitRequest,
    TurnView,
)
from backend.app.repositories.participants import (
    create_participant_days,
    get_participant_by_name_phone,
    get_participant_by_id,
    get_participant_day_by_index,
    update_participant_day_status,
    insert_participant,
)
from backend.app.repositories.attempts import (
    get_attempt_by_id,
    get_current_attempt,
)
from backend.app.repositories.pretests import get_latest_pretest_response
from backend.app.repositories.artifacts import (
    get_latest_artifact_status_for_session,
    get_visible_artifact_for_turn,
    insert_failed_task_artifact,
    insert_task_artifact,
    list_visible_completed_artifacts_for_session,
    list_recent_weather_agent_states,
)
from backend.app.repositories.sessions import (
    get_latest_session_for_participant_day,
    get_session_by_uuid_for_participant,
    get_session_by_uuid_for_participant_attempt,
    insert_session,
    update_manipulation_status,
    update_session_status,
)
from backend.app.repositories.turns import (
    count_failed_asr_attempts,
    get_asr_attempt_by_id,
    get_rating_for_turn,
    get_matching_successful_asr_attempt,
    get_successful_asr_attempt_by_result_ref,
    get_turn_by_id,
    insert_asr_attempt,
    insert_rating,
    insert_turn,
    list_turns_for_session,
)
from backend.app.scenarios.registry import Scenario, ScenarioRegistry
from backend.app.services.asr_tencent import (
    AsrClient,
    AsrResult,
    read_bounded_audio_file,
)
from backend.app.services.api_health import ApiHealthService
from backend.app.services.clean_data import refresh_participant_clean_data_audit
from backend.app.services.file_naming import canonical_audio_relative_path
from backend.app.services.history_context import build_formal_history_context
from backend.app.services.participant_days import (
    complete_participant_day,
    resolve_actionable_participant_day,
    resolve_current_participant_day,
)
from backend.app.services.providers import (
    ProviderAttempt,
    ProviderMessage,
    ProviderResponse,
    ProviderRoutesExhausted,
    ProviderRouter,
)
from backend.app.services.records import (
    SYSTEM_FAILURE_TEXT,
    from_json,
    safe_input_metadata,
    timestamp_now,
    to_json,
)
from backend.app.services.weather import (
    WEATHER_LOCATION_NOT_FOUND_TEXT,
    WEATHER_LOCATION_REQUIRED_TEXT,
    WEATHER_UNAVAILABLE_TEXT,
    WeatherService,
    WeatherServiceError,
    WeatherSnapshot,
    extract_weather_location,
    render_weather_card,
    render_weather_text,
)
from backend.app.settings import Settings, get_settings
from backend.app.time_utils import current_shanghai_date


MAX_TURNS = 5
SUPPORTED_BROWSERS = {"chrome", "edge"}
SAFE_FILENAME_CHARS = re.compile(r"[^a-zA-Z0-9._-]+")
TEST_CHANNEL_NAME = "__test_channel__"
TEST_CHANNEL_PHONE = "00000000000"
TEST_CHANNEL_PHONE_HASH = "test-channel"
MISSING_RATING_COMPLETE_DETAIL = (
    "All submitted turns must be rated before completion."
)
EXECUTION_ARTIFACT_KINDS = {
    "table": "schedule_table",
    "copy_versions": "copy_editor",
}


def _validated_stored_artifact_payload(
    artifact_row: sqlite3.Row,
    *,
    expected_artifact_type: str,
) -> dict[str, Any] | None:
    if (
        str(artifact_row["artifact_type"]) != expected_artifact_type
        or str(artifact_row["status"]) != "completed"
        or not bool(artifact_row["visible_to_participant"])
    ):
        return None
    try:
        payload = from_json(artifact_row["payload_json"], None)
        candidate = ResponseCandidate(
            assistant_text="Stored artifact validation.",
            artifact_type=expected_artifact_type,
            artifact_payload=payload,
        )
        validate_candidate_artifact(candidate)
        projected_payload = project_participant_artifact(
            artifact_type=expected_artifact_type,
            payload=payload,
            assistant_text=candidate.assistant_text,
        )
    except (TypeError, ValueError):
        return None
    return projected_payload


@dataclass(frozen=True)
class PreparedAsrSubmission:
    participant_id: int
    attempt_id: int | None
    session_row: dict[str, Any]
    turn_index: int
    filename: str
    content_type: str | None
    audio_path: Path
    max_upload_bytes: int
    relative_audio_path: str
    audio_sha256: str


@dataclass(frozen=True)
class FinalizedAsrSubmission:
    view: AsrView
    asr_attempt_id: int
    session_status: str


@dataclass(frozen=True)
class PreparedTurnSubmission:
    participant_id: int
    attempt_id: int | None
    session_row: dict[str, Any]
    turn_index: int
    request_input_mode: str
    user_text: str
    user_audio_path: str | None
    user_audio_sha256: str | None
    asr_provider: str | None
    asr_status: str
    asr_text: str | None
    asr_latency_ms: int | None
    provider_messages: list[ProviderMessage]
    recent_history: list[ConversationMessage]
    evaluation_history: list[ConversationMessage]


@dataclass(frozen=True)
class ExecutedTurnSubmission:
    provider_result: ProviderResponse | StructuredAgentResult
    provider_response: ProviderResponse
    turn_result: GraphRunResult


@dataclass(frozen=True)
class WeatherTurnExecution:
    provider_response: ProviderResponse
    weather_tool: dict[str, Any]


@dataclass(frozen=True)
class _SemanticCandidateExecution:
    provider_result: ProviderResponse | StructuredAgentResult
    provider_response: ProviderResponse
    turn_result: GraphRunResult
    weather_tool: dict[str, Any] | None


class SessionStateMachine:
    ACTIVE_STATUS = "started"
    TERMINAL_STATUSES = {"completed", "abandoned", "invalid", "interrupted"}

    @classmethod
    def ensure_started(cls, session_row: sqlite3.Row) -> None:
        if session_row["status"] != cls.ACTIVE_STATUS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Session is not active: {session_row['status']}.",
            )


def _build_rating_view(turn_row: sqlite3.Row) -> TurnRatingView | None:
    if turn_row["rating_id"] is None:
        return None
    return TurnRatingView(
        turn_id=int(turn_row["id"]),
        stance_score=int(turn_row["stance_score"]),
        trust_score=int(turn_row["trust_score"]),
        submitted_at=str(turn_row["rating_submitted_at"]),
        client_elapsed_ms=(
            int(turn_row["client_elapsed_ms"])
            if turn_row["client_elapsed_ms"] is not None
            else None
        ),
    )


def _build_turn_view(
    conn: sqlite3.Connection,
    turn_row: sqlite3.Row,
    *,
    session_is_test: bool,
) -> TurnView:
    artifact_row = get_visible_artifact_for_turn(conn, turn_id=int(turn_row["id"]))
    artifact_type = None
    artifact_payload = None
    if artifact_row is not None:
        try:
            artifact_payload = from_json(artifact_row["payload_json"], None)
        except ValueError:
            artifact_payload = None
        if artifact_payload is not None:
            artifact_type = str(artifact_row["artifact_type"])

    graph_trace = None
    provider_attempts = None
    evaluator_result = None
    if session_is_test:
        trace_payload = from_json(turn_row["agent_state_json"], {})
        if isinstance(trace_payload, dict):
            graph_trace = dict(trace_payload)
            graph_trace["semantic_evidence"] = {
                "attempt_count": int(turn_row["error_semantic_attempt_count"]),
                "failure_reason": turn_row["error_failure_reason"],
                "attempts": from_json(turn_row["error_attempts_json"], []),
            }
            evaluator_result = from_json(
                turn_row["error_evaluator_result_json"],
                None,
            )
            if evaluator_result is not None:
                graph_trace["evaluator_result"] = evaluator_result
        attempts_payload = from_json(turn_row["llm_attempts_json"], [])
        if isinstance(attempts_payload, list):
            provider_attempts = attempts_payload

    return TurnView(
        turn_id=int(turn_row["id"]),
        session_id=str(turn_row["session_uuid"]) if "session_uuid" in turn_row.keys() else "",
        turn_index=int(turn_row["turn_index"]),
        user_text=str(turn_row["user_text"] or ""),
        user_input_mode=str(turn_row["user_input_mode"]),
        assistant_text=str(turn_row["assistant_text"] or ""),
        error_planned=bool(turn_row["error_planned"]),
        error_presented=bool(turn_row["error_presented"]),
        error_presentation=str(turn_row["error_presentation"]),
        session_is_test=session_is_test,
        artifact_type=artifact_type,
        artifact_payload=artifact_payload,
        graph_trace=graph_trace,
        provider_attempts=provider_attempts,
        evaluator_result=evaluator_result,
        rating=_build_rating_view(turn_row),
    )


def _expected_turn_index(turn_rows: list[sqlite3.Row], *, session_status: str) -> int | None:
    if session_status in SessionStateMachine.TERMINAL_STATUSES:
        return None
    if not turn_rows:
        return 1

    last_turn = turn_rows[-1]
    if last_turn["rating_id"] is None:
        return int(last_turn["turn_index"])

    next_turn = int(last_turn["turn_index"]) + 1
    if next_turn > MAX_TURNS:
        return None
    return next_turn


def _build_session_view(
    conn: sqlite3.Connection,
    *,
    session_row: sqlite3.Row,
) -> SessionView:
    turn_rows = list_turns_for_session(conn, session_id=int(session_row["id"]))
    turn_views = [
        _build_turn_view(
            conn,
            turn_row,
            session_is_test=bool(session_row["is_test"]),
        )
        for turn_row in turn_rows
    ]
    latest_turn = turn_views[-1] if turn_views else None
    scenario = ScenarioRegistry.load_default().resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
    )
    presentation_mode = (
        "execution" if scenario.subcondition == "execution" else "conversation"
    )
    artifact_kind = (
        EXECUTION_ARTIFACT_KINDS.get(scenario.artifact_type)
        if presentation_mode == "execution"
        else None
    )
    artifact_status = "none"
    artifact_type = latest_turn.artifact_type if latest_turn is not None else None
    artifact_payload = latest_turn.artifact_payload if latest_turn is not None else None
    if presentation_mode == "execution":
        latest_outcome = get_latest_artifact_status_for_session(
            conn,
            session_id=int(session_row["id"]),
        )
        completed_rows = list_visible_completed_artifacts_for_session(
            conn,
            session_id=int(session_row["id"]),
            artifact_type=scenario.artifact_type,
        )
        if latest_outcome is None:
            artifact_status = "none"
        elif str(latest_outcome["status"]) == "draft":
            artifact_status = "awaiting_input"
        elif str(latest_outcome["status"]) == "completed":
            latest_outcome_payload = _validated_stored_artifact_payload(
                latest_outcome,
                expected_artifact_type=str(scenario.artifact_type),
            )
            artifact_status = (
                "completed" if latest_outcome_payload is not None else "failed"
            )
        else:
            artifact_status = "failed"
        artifact_type = None
        artifact_payload = None
        for completed_row in completed_rows:
            validated_payload = _validated_stored_artifact_payload(
                completed_row,
                expected_artifact_type=str(scenario.artifact_type),
            )
            if validated_payload is not None:
                artifact_type = str(completed_row["artifact_type"])
                artifact_payload = validated_payload
                break
    return SessionView(
        session_id=str(session_row["session_uuid"]),
        day_index=int(session_row["day_index"]),
        status=str(session_row["status"]),
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
        error_type_id=str(session_row["error_type_id"]),
        planned_error_turn=int(session_row["planned_error_turn"]),
        started_at=str(session_row["started_at"]),
        completed_at=session_row["completed_at"],
        is_test=bool(session_row["is_test"]),
        client_info=ClientInfo.model_validate(
            from_json(session_row["client_info_json"], {})
        ),
        expected_turn_index=_expected_turn_index(
            turn_rows,
            session_status=str(session_row["status"]),
        ),
        presentation_mode=presentation_mode,
        artifact_kind=artifact_kind,
        artifact_status=artifact_status,
        artifact_type=artifact_type,
        artifact_payload=artifact_payload,
        graph_trace=latest_turn.graph_trace if latest_turn is not None else None,
        provider_attempts=latest_turn.provider_attempts if latest_turn is not None else None,
        evaluator_result=latest_turn.evaluator_result if latest_turn is not None else None,
        turns=turn_views,
    )


def _resolve_test_session_configuration(
    *,
    participant_row: sqlite3.Row,
    request: SessionStartRequest,
) -> tuple[str, str, str, str, int, str, str]:
    condition = request.condition or str(participant_row["condition"])
    subcondition = request.subcondition or str(participant_row["subcondition"])
    topic_key = request.topic_key or str(participant_row["topic_key"])
    registry = ScenarioRegistry.load_default()
    try:
        scenario = registry.require(
            condition=condition,
            subcondition=subcondition,
            topic_key=topic_key,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid topic_key for selected condition/subcondition: "
                f"{condition}/{subcondition}/{topic_key}."
            ),
        ) from exc
    topic_key = scenario.topic_key
    error_type_id = request.error_type_id or str(participant_row["error_type_id"])
    planned_error_turn = request.planned_error_turn or _generate_planned_error_turn()
    return (
        condition,
        subcondition,
        topic_key,
        error_type_id,
        planned_error_turn,
        scenario.scenario_id,
        scenario.graph,
    )


def _validate_formal_client_info(
    client_info: ClientInfo,
    *,
    settings: Settings,
) -> None:
    if client_info.device_type == "mobile":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="mobile_device")
    if client_info.device_type == "tablet":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tablet_device")
    if client_info.viewport_width < settings.formal_min_viewport_width:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="small_viewport")
    if not client_info.is_secure_context:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="insecure_context")
    if client_info.browser_name.strip().lower() not in SUPPORTED_BROWSERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unsupported_browser",
        )
    if not client_info.microphone_available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing_microphone",
        )
    if client_info.microphone_permission != "granted":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="microphone_permission_denied",
        )


def _require_participant(conn: sqlite3.Connection, *, participant_id: int) -> sqlite3.Row:
    participant_row = get_participant_by_id(conn, participant_id=participant_id)
    if participant_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Participant not found.",
        )
    return participant_row


def _require_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    session_uuid: str,
    attempt_id: int | None = None,
) -> sqlite3.Row:
    if attempt_id is None:
        session_row = get_session_by_uuid_for_participant(
            conn,
            session_uuid=session_uuid,
            participant_id=participant_id,
        )
    else:
        session_row = get_session_by_uuid_for_participant_attempt(
            conn,
            session_uuid=session_uuid,
            participant_id=participant_id,
            attempt_id=attempt_id,
        )
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )
    return session_row


def _ensure_test_channel_participant(
    conn: sqlite3.Connection,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    participant_row = get_participant_by_name_phone(
        conn,
        name=TEST_CHANNEL_NAME,
        phone=TEST_CHANNEL_PHONE,
    )
    if participant_row is None:
        participant_id = insert_participant(
            conn,
            name=TEST_CHANNEL_NAME,
            phone=TEST_CHANNEL_PHONE,
            phone_hash=TEST_CHANNEL_PHONE_HASH,
            participant_type="short",
            condition="human",
            subcondition="qa",
            topic_key="advice",
            error_type_id="factual_minor",
            target_days=1,
        )
        create_participant_days(
            conn,
            participant_id=participant_id,
            target_days=1,
            start_date=date.fromisoformat(current_shanghai_date()),
        )
        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        if participant_row is None:
            raise LookupError("Failed to reload test channel participant after insert.")

    participant_day = get_participant_day_by_index(
        conn,
        participant_id=int(participant_row["id"]),
        day_index=1,
    )
    if participant_day is None:
        raise LookupError("Test channel participant day is missing.")

    today = current_shanghai_date()
    if participant_day["calendar_date"] != today:
        conn.execute(
            """
            UPDATE participant_days
            SET
                calendar_date = ?,
                status = 'not_started',
                started_at = NULL,
                completed_at = NULL,
                missed_reason = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (today, int(participant_day["id"])),
        )
        participant_day = get_participant_day_by_index(
            conn,
            participant_id=int(participant_row["id"]),
            day_index=1,
        )
        if participant_day is None:
            raise LookupError("Failed to reload test channel participant day.")

    return participant_row, participant_day


def _ensure_formal_pretest_gate(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    attempt_id: int | None = None,
) -> None:
    if day_index != 1:
        return
    final_row = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="final",
        attempt_id=attempt_id,
    )
    if final_row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Day 1 pretest final submission is required.",
        )


def start_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    request: SessionStartRequest,
    settings: Settings,
) -> SessionView:
    participant_row = _require_participant(conn, participant_id=participant_id)
    is_test = bool(request.is_test)
    current_attempt: sqlite3.Row | None = None
    session_attempt_id: int | None = None

    if is_test:
        resolved_day = resolve_current_participant_day(
            conn,
            participant_id=participant_id,
        )
    else:
        current_attempt = get_current_attempt(conn, participant_id=participant_id)
        if current_attempt is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Participant has no current attempt.",
            )
        current_attempt_id = int(current_attempt["id"])
        if current_attempt_id != attempt_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid session.",
            )
        session_attempt_id = current_attempt_id
        resolved_day = resolve_actionable_participant_day(
            conn,
            participant_id=participant_id,
            attempt_row=current_attempt,
        )
    participant_day = (
        resolved_day.require_scheduled_today()
        if is_test
        else resolved_day.require_actionable_today()
    )

    if not is_test:
        _validate_formal_client_info(request.client_info, settings=settings)
        _ensure_formal_pretest_gate(
            conn,
            participant_id=participant_id,
            day_index=int(participant_day["day_index"]),
            attempt_id=session_attempt_id,
        )

    existing_session = get_latest_session_for_participant_day(
        conn,
        participant_day_id=int(participant_day["id"]),
        is_test=is_test,
    )
    if existing_session is not None and not is_test:
        if existing_session["status"] == "started":
            return _build_session_view(conn, session_row=existing_session)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A session already exists for this participant day.",
        )

    started_at = timestamp_now()
    session_uuid = str(uuid4())
    if is_test:
        (
            condition,
            subcondition,
            topic_key,
            error_type_id,
            planned_error_turn,
            scenario_id,
            agent_graph_version,
        ) = _resolve_test_session_configuration(
            participant_row=participant_row,
            request=request,
        )
    else:
        if current_attempt is None:
            raise LookupError("Formal session requires a current attempt.")
        planned_error_turn = _generate_planned_error_turn()
        condition = str(current_attempt["condition"])
        subcondition = str(current_attempt["subcondition"])
        topic_key = str(current_attempt["topic_key"])
        error_type_id = str(current_attempt["error_type_id"])
        scenario = ScenarioRegistry.load_default().require(
            condition=condition,
            subcondition=subcondition,
            topic_key=topic_key,
        )
        scenario_id = scenario.scenario_id
        agent_graph_version = scenario.graph
    session_id = insert_session(
        conn,
        participant_id=participant_id,
        participant_day_id=int(participant_day["id"]),
        attempt_id=session_attempt_id,
        session_uuid=session_uuid,
        condition=condition,
        subcondition=subcondition,
        topic_key=topic_key,
        scenario_id=scenario_id,
        agent_graph_version=agent_graph_version,
        error_type_id=error_type_id,
        planned_error_turn=planned_error_turn,
        status="started",
        started_at=started_at,
        client_info_json=to_json(request.client_info.model_dump(mode="json")),
        is_test=is_test,
    )
    if not is_test:
        update_participant_day_status(
            conn,
            participant_day_id=int(participant_day["id"]),
            status="in_experiment",
            started_at=started_at,
        )
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=session_uuid,
        attempt_id=session_attempt_id,
    )
    if session_row["id"] != session_id:
        raise LookupError("Failed to reload session after insert.")
    return _build_session_view(conn, session_row=session_row)


def start_test_session_without_participant(
    conn: sqlite3.Connection,
    *,
    request: SessionStartRequest,
    settings: Settings,
) -> SessionView:
    del settings
    participant_row, participant_day = _ensure_test_channel_participant(conn)
    started_at = timestamp_now()
    session_uuid = str(uuid4())
    (
        condition,
        subcondition,
        topic_key,
        error_type_id,
        planned_error_turn,
        scenario_id,
        agent_graph_version,
    ) = _resolve_test_session_configuration(
        participant_row=participant_row,
        request=request,
    )
    session_id = insert_session(
        conn,
        participant_id=int(participant_row["id"]),
        participant_day_id=int(participant_day["id"]),
        attempt_id=None,
        session_uuid=session_uuid,
        condition=condition,
        subcondition=subcondition,
        topic_key=topic_key,
        scenario_id=scenario_id,
        agent_graph_version=agent_graph_version,
        error_type_id=error_type_id,
        planned_error_turn=planned_error_turn,
        status="started",
        started_at=started_at,
        client_info_json=to_json(request.client_info.model_dump(mode="json")),
        is_test=True,
    )
    session_row = _require_session(
        conn,
        participant_id=int(participant_row["id"]),
        session_uuid=session_uuid,
    )
    if session_row["id"] != session_id:
        raise LookupError("Failed to reload test session after insert.")
    return _build_session_view(conn, session_row=session_row)


def get_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    session_uuid: str,
    attempt_id: int | None = None,
) -> SessionView:
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=session_uuid,
        attempt_id=attempt_id,
    )
    return _build_session_view(conn, session_row=session_row)


def prepare_asr_submission(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    session_uuid: str,
    filename: str,
    content_type: str | None,
    staged_audio_path: Path,
    audio_sha256: str,
    settings: Settings,
) -> PreparedAsrSubmission:
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=session_uuid,
        attempt_id=attempt_id,
    )
    SessionStateMachine.ensure_started(session_row)
    turn_index = _require_expected_turn_index(conn, session_id=int(session_row["id"]))

    relative_audio_path = _persist_audio_file(
        conn=conn,
        settings=settings,
        participant_id=participant_id,
        session_row=session_row,
        turn_index=turn_index,
        filename=filename,
        staged_audio_path=staged_audio_path,
    )
    return PreparedAsrSubmission(
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_row=_session_row_to_mapping(session_row),
        turn_index=turn_index,
        filename=filename,
        content_type=content_type,
        audio_path=settings.data_dir / relative_audio_path,
        max_upload_bytes=settings.asr_max_upload_bytes,
        relative_audio_path=relative_audio_path,
        audio_sha256=audio_sha256,
    )


def run_asr_submission(
    prepared: PreparedAsrSubmission,
    *,
    asr_client: AsrClient,
) -> AsrResult:
    audio_bytes = read_bounded_audio_file(
        prepared.audio_path,
        max_bytes=prepared.max_upload_bytes,
    )
    try:
        return asr_client.transcribe(
            audio_bytes=audio_bytes,
            filename=prepared.filename,
            content_type=prepared.content_type,
            request_id=(
                f"{prepared.session_row['session_uuid']}-asr-turn-"
                f"{prepared.turn_index}"
            ),
        )
    except (TimeoutError, httpx.TimeoutException):
        return AsrResult(
            status="timeout",
            provider="tencent",
            text=None,
            latency_ms=None,
        )
    except Exception:
        return AsrResult(
            status="failed",
            provider="tencent",
            text=None,
            latency_ms=None,
        )


def finalize_asr_submission(
    conn: sqlite3.Connection,
    *,
    prepared: PreparedAsrSubmission,
    asr_result: AsrResult,
    settings: Settings,
    health_service: ApiHealthService,
) -> FinalizedAsrSubmission:
    session_row = _require_session(
        conn,
        participant_id=prepared.participant_id,
        session_uuid=str(prepared.session_row["session_uuid"]),
        attempt_id=prepared.attempt_id,
    )
    SessionStateMachine.ensure_started(session_row)
    current_turn_index = _require_expected_turn_index(
        conn,
        session_id=int(session_row["id"]),
    )
    if current_turn_index != prepared.turn_index:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ASR operation is stale for the current session turn.",
        )

    asr_attempt_id = insert_asr_attempt(
        conn,
        session_id=int(session_row["id"]),
        turn_index=prepared.turn_index,
        user_audio_path=prepared.relative_audio_path,
        user_audio_sha256=prepared.audio_sha256,
        asr_provider=asr_result.provider,
        asr_status=asr_result.status,
        asr_text=asr_result.text,
        asr_latency_ms=asr_result.latency_ms,
        result_ref=secrets.token_urlsafe(32),
    )
    asr_attempt_row = get_asr_attempt_by_id(conn, asr_attempt_id=asr_attempt_id)
    if asr_attempt_row is None or asr_attempt_row["result_ref"] is None:
        raise RuntimeError("Failed to persist the ASR result reference.")

    _log_asr_attempt(
        health_service=health_service,
        request_id=(
            f"{session_row['session_uuid']}-asr-turn-{prepared.turn_index}"
        ),
        asr_result=asr_result,
    )

    retry_count = 0
    session_status = str(session_row["status"])
    if asr_result.status in {"failed", "timeout"}:
        retry_count = count_failed_asr_attempts(
            conn,
            session_id=int(session_row["id"]),
            turn_index=prepared.turn_index,
        )
        health_service.add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="asr_failed",
            detail={
                "turn_index": prepared.turn_index,
                "asr_status": asr_result.status,
                "retry_count": retry_count,
            },
        )
        if (
            not bool(session_row["is_test"])
            and retry_count >= settings.asr_max_retry_per_turn
        ):
            update_session_status(
                conn,
                session_id=int(session_row["id"]),
                status="interrupted",
                completed_at=None,
            )
            health_service.add_session_risk_flag(
                session_id=int(session_row["id"]),
                flag="asr_repeated_failure",
                detail={
                    "turn_index": prepared.turn_index,
                    "retry_count": retry_count,
                },
            )
            session_status = "interrupted"

    return FinalizedAsrSubmission(
        view=AsrView(
            asr_result_id=str(asr_attempt_row["result_ref"]),
            asr_status=asr_result.status,
            asr_text=asr_result.text,
            retry_count=retry_count,
            max_retry_per_turn=settings.asr_max_retry_per_turn,
        ),
        asr_attempt_id=asr_attempt_id,
        session_status=session_status,
    )


def submit_asr(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    session_uuid: str,
    filename: str,
    content_type: str | None,
    audio_bytes: bytes,
    asr_client: AsrClient,
    settings: Settings,
    health_service: ApiHealthService | None = None,
) -> AsrView:
    media_type = (content_type or "application/octet-stream").split(";", 1)[0].lower()
    allowed_media_types = {
        item.strip().lower()
        for item in settings.asr_allowed_media_types.split(",")
        if item.strip()
    }
    if media_type not in allowed_media_types:
        raise ValueError(f"Unsupported audio media type: {media_type}.")
    if not audio_bytes:
        raise ValueError("Audio upload must not be empty.")
    if len(audio_bytes) > settings.asr_max_upload_bytes:
        raise ValueError(
            f"Audio upload exceeds the {settings.asr_max_upload_bytes} byte limit."
        )

    staging_dir = settings.data_dir / ".asr-uploads"
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_audio_path = staging_dir / f"{uuid4().hex}.upload"
    staged_audio_path.write_bytes(audio_bytes)
    prepared = None
    try:
        prepared = prepare_asr_submission(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
            session_uuid=session_uuid,
            filename=filename,
            content_type=content_type,
            staged_audio_path=staged_audio_path,
            audio_sha256=hashlib.sha256(audio_bytes).hexdigest(),
            settings=settings,
        )
        asr_result = run_asr_submission(prepared, asr_client=asr_client)
        return finalize_asr_submission(
            conn,
            prepared=prepared,
            asr_result=asr_result,
            settings=settings,
            health_service=health_service or ApiHealthService(conn),
        ).view
    except Exception:
        if prepared is not None:
            prepared.audio_path.unlink(missing_ok=True)
        raise
    finally:
        staged_audio_path.unlink(missing_ok=True)


def prepare_turn_submission(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    request: TurnSubmitRequest,
) -> PreparedTurnSubmission:
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=request.session_id,
        attempt_id=attempt_id,
    )
    SessionStateMachine.ensure_started(session_row)
    turn_index = _require_expected_turn_index(conn, session_id=int(session_row["id"]))
    if not bool(session_row["is_test"]) and request.input_mode != "voice":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formal sessions require voice input.",
        )

    user_text = request.user_text or ""
    user_audio_path = None
    user_audio_sha256 = None
    asr_provider = None
    asr_status = "not_used"
    asr_text = None
    asr_latency_ms = None
    if request.input_mode == "voice":
        asr_attempt_row = _require_matching_asr_success(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
            session_id=int(session_row["id"]),
            turn_index=turn_index,
            request=request,
        )
        user_text = str(asr_attempt_row["asr_text"] or "")
        user_audio_path = str(asr_attempt_row["user_audio_path"])
        user_audio_sha256 = str(asr_attempt_row["user_audio_sha256"])
        asr_provider = (
            str(asr_attempt_row["asr_provider"])
            if asr_attempt_row["asr_provider"] is not None
            else None
        )
        asr_status = str(asr_attempt_row["asr_status"])
        asr_text = str(asr_attempt_row["asr_text"] or "")
        asr_latency_ms = (
            int(asr_attempt_row["asr_latency_ms"])
            if asr_attempt_row["asr_latency_ms"] is not None
            else None
        )
    elif not user_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Test text submissions require user text.",
        )

    recent_history = build_formal_history_context(
        conn,
        attempt_id=attempt_id,
        current_session_id=int(session_row["id"]),
        is_test=bool(session_row["is_test"]),
    )
    evaluation_history = _recent_history_for_session(
        conn,
        session_id=int(session_row["id"]),
    )
    scenario = ScenarioRegistry.load_default().resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
    )
    provider_messages = [
        ProviderMessage(role="system", content=scenario.provider_system_prompt)
    ]
    provider_messages.extend(
        ProviderMessage(role=message.role, content=message.text)
        for message in recent_history
    )
    provider_messages.append(ProviderMessage(role="user", content=user_text))

    return PreparedTurnSubmission(
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_row=_session_row_to_mapping(session_row),
        turn_index=turn_index,
        request_input_mode=request.input_mode,
        user_text=user_text,
        user_audio_path=user_audio_path,
        user_audio_sha256=user_audio_sha256,
        asr_provider=asr_provider,
        asr_status=asr_status,
        asr_text=asr_text,
        asr_latency_ms=asr_latency_ms,
        provider_messages=provider_messages,
        recent_history=recent_history,
        evaluation_history=evaluation_history,
    )


def run_turn_submission(
    conn: sqlite3.Connection,
    *,
    prepared: PreparedTurnSubmission,
    settings: Settings,
    health_service: ApiHealthService,
) -> ExecutedTurnSubmission:
    session_row = prepared.session_row
    scenario = ScenarioRegistry.load_default().resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
    )
    error_planned = prepared.turn_index == int(session_row["planned_error_turn"])
    error_type_id = str(session_row["error_type_id"])
    router = ProviderRouter(settings=settings, health_service=health_service)
    request_id = f"{session_row['session_uuid']}-turn-{prepared.turn_index}"
    cached_weather: WeatherTurnExecution | None = None
    execution_schema = None
    current_execution_artifact: dict[str, Any] | None = None
    local_execution_artifact = None
    if str(session_row["subcondition"]) == "execution":
        execution_schema = schema_for_artifact(
            condition=str(session_row["condition"]),
            subcondition=str(session_row["subcondition"]),
        )
        if scenario.artifact_type:
            completed_artifacts = list_visible_completed_artifacts_for_session(
                conn,
                session_id=int(session_row["id"]),
                artifact_type=scenario.artifact_type,
            )
            if completed_artifacts:
                current_execution_artifact = _validated_stored_artifact_payload(
                    completed_artifacts[0],
                    expected_artifact_type=scenario.artifact_type,
                )
        local_execution_artifact = build_local_execution_artifact(
            condition=str(session_row["condition"]),
            user_text=prepared.user_text,
            current_artifact=current_execution_artifact,
        )

    async def generate_candidate(
        attempt_no: int,
        feedback_code: str | None,
    ) -> tuple[
        ProviderResponse | StructuredAgentResult,
        dict[str, Any] | None,
    ]:
        nonlocal cached_weather
        if error_planned and error_type_id == "system_failure":
            return _planned_system_failure_response(), None
        if scenario.topic_key == "weather":
            if cached_weather is None:
                previous_source = _latest_successful_weather_source(
                    conn,
                    session_id=int(session_row["id"]),
                )
                cached_weather = await _run_weather_turn(
                    settings=settings,
                    user_text=prepared.user_text,
                    previous_source=previous_source,
                )
            if (
                not error_planned
                or cached_weather.weather_tool.get("status") != "success"
            ):
                return cached_weather.provider_response, cached_weather.weather_tool

        authoritative_context = (
            to_json(cached_weather.weather_tool)
            if cached_weather is not None
            else None
        )
        generation_messages = build_generation_messages(
            base_messages=prepared.provider_messages,
            behavior_id=error_type_id if error_planned else "normal",
            feedback_reason=feedback_code if error_planned else None,
            authoritative_context=authoritative_context,
        )

        semantic_request_id = f"{request_id}-semantic-{attempt_no}"
        if str(session_row["subcondition"]) == "execution":
            assert execution_schema is not None
            execution_messages = build_execution_workspace_messages(
                base_messages=generation_messages,
                schema=execution_schema,
                user_text=prepared.user_text,
                current_artifact=current_execution_artifact,
                local_artifact=local_execution_artifact,
                error_type_id=error_type_id if error_planned else None,
                error_presentation=(
                    scenario.mutation_policy.rules[error_type_id].presentation
                    if error_planned
                    else None
                ),
                target_kind=(
                    scenario.mutation_policy.rules[error_type_id].target_kind
                    if error_planned
                    else None
                ),
            )
            structured_kwargs = {
                "request_id": semantic_request_id,
                "messages": execution_messages,
                "is_test": bool(session_row["is_test"]),
                "schema": execution_schema,
                "payload_normalizer": lambda payload: normalize_execution_payload(
                    payload,
                    schema=execution_schema,
                    user_text=prepared.user_text,
                    current_artifact=current_execution_artifact,
                    action_mode=(
                        "revise" if current_execution_artifact is not None else "create"
                    ),
                ),
            }
            if error_planned:
                structured_kwargs["allow_local_fallback"] = False
            generated = await router.generate_structured_agent(
                **structured_kwargs,
            )
            resolved = resolve_execution_result(
                generated,
                schema=execution_schema,
                user_text=prepared.user_text,
                current_artifact=current_execution_artifact,
                local_artifact=local_execution_artifact,
                error_planned=error_planned,
            )
            return resolved, cached_weather.weather_tool if cached_weather else None
        chat_kwargs = {
            "request_id": semantic_request_id,
            "messages": generation_messages,
            "is_test": bool(session_row["is_test"]),
        }
        if error_planned:
            chat_kwargs["allow_local_fallback"] = False
        generated_chat = await router.generate_chat(**chat_kwargs)
        return (
            generated_chat,
            cached_weather.weather_tool if cached_weather else None,
        )

    async def execute_attempt(
        attempt_no: int,
        feedback_code: str | None,
    ) -> SemanticAttemptResult:
        provider_result, weather_tool = await generate_candidate(
            attempt_no,
            feedback_code,
        )
        provider_response = _provider_response(provider_result)
        prompt_native_error_generated = (
            error_planned
            and error_type_id != "system_failure"
            and (
                scenario.topic_key != "weather"
                or weather_tool is not None
                and weather_tool.get("status") == "success"
            )
        )
        error_agent_result = (
            _prompt_native_error_evidence(
                scenario=scenario,
                error_type_id=error_type_id,
                provider_result=provider_result,
            )
            if prompt_native_error_generated
            else None
        )
        structured_parse_attempts = (
            provider_result.parse_attempts
            if isinstance(provider_result, StructuredAgentResult)
            else 0
        )
        structured_failure = (
            provider_result.validation_error
            if isinstance(provider_result, StructuredAgentResult)
            else None
        )
        turn_result = _build_session_turn_result(
            conn=conn,
            session_row=session_row,
            turn_index=prepared.turn_index,
            request_input_mode=prepared.request_input_mode,
            user_text=prepared.user_text,
            provider_result=provider_result,
            settings=settings,
            health_service=health_service,
            recent_history=prepared.recent_history,
            weather_tool=weather_tool,
            error_agent_result=error_agent_result,
            defer_evaluator=True,
        )
        mutation = turn_result.state.error_mutation
        evaluator: dict[str, Any] = {}
        participant_surface = turn_result.state.assistant_text
        if turn_result.state.artifact_payload is not None:
            participant_surface = (
                f"{participant_surface}\n"
                f"{to_json(turn_result.state.artifact_payload)}"
            )
        disclosure_detected = (
            error_planned
            and error_type_id != "system_failure"
            and contains_error_policy_disclosure(participant_surface)
        )
        if (
            error_planned
            and turn_result.state.error_presentation
            not in {"none", "system_failure"}
            and not disclosure_detected
        ):
            evaluator = await _evaluate_injected_error_async(
                settings=settings,
                health_service=health_service,
                session_uuid=str(session_row["session_uuid"]),
                turn_index=prepared.turn_index,
                state=turn_result.state,
                assistant_text=turn_result.state.assistant_text,
                artifact_type=turn_result.state.artifact_type,
                artifact_payload=turn_result.state.artifact_payload,
                session_history=prepared.evaluation_history,
                current_user_text=prepared.user_text,
                weather_context=(
                    to_json(weather_tool)
                    if weather_tool is not None
                    else None
                ),
                provider_response=provider_response,
            )
            retry_feedback = evaluator.pop("feedback_reason", None)
            _apply_deferred_evaluator_result(turn_result, evaluator)
        elif disclosure_detected:
            evaluator = {
                "status": "failed",
                "presented": False,
                "provider": None,
                "model": None,
                "route": "candidate_validation",
                "parse_attempts": 0,
                "reason": "structured_mutation_disclosure",
            }
            retry_feedback = (
                "上一候选违反参与者可见输出边界。下一候选只输出自然、完整的用户回复，"
                "不要添加括号说明、注释或元解释。"
            )
            _apply_deferred_evaluator_result(turn_result, evaluator)
        else:
            retry_feedback = None
        failure_reason = structured_failure
        if failure_reason is None and disclosure_detected:
            failure_reason = "structured_mutation_disclosure"
        if failure_reason is None and turn_result.state.artifact_validation_status == "invalid":
            failure_reason = "artifact_schema_invalid"
        if failure_reason is None and (mutation is None or not mutation.applied):
            failure_reason = (
                mutation.failure_reason
                if mutation is not None
                else "mutation_not_applied"
            )
        if failure_reason is None and error_planned and not bool(evaluator.get("presented")):
            failure_reason = normalize_semantic_failure_code(
                evaluator.get("reason"),
                default="evaluator_not_presented",
            )
        execution = _SemanticCandidateExecution(
            provider_result=provider_result,
            provider_response=provider_response,
            turn_result=turn_result,
            weather_tool=weather_tool,
        )
        return SemanticAttemptResult(
            final_value=execution,
            mutation_applied=bool(mutation and mutation.applied),
            evaluator_presented=(
                bool(evaluator.get("presented")) if error_planned else False
            ),
            failure_reason=failure_reason,
            evaluator_status=str(evaluator.get("status") or "not_run"),
            evaluator_parse_attempts=int(evaluator.get("parse_attempts") or 0),
            structured_parse_attempts=structured_parse_attempts,
            provider=provider_response.provider,
            model=provider_response.model,
            route=provider_response.route,
            provider_status=_provider_status(provider_response),
            route_attempt_count=len(provider_response.attempts),
            retry_feedback=retry_feedback,
        )

    coordinator = ErrorPresentationCoordinator(
        max_semantic_attempts=settings.error_semantic_max_attempts,
        timeout_seconds=settings.error_semantic_timeout_seconds,
    )
    async def run_semantic_loop():
        if error_planned and error_type_id == "system_failure":
            system_attempt = await execute_attempt(1, None)
            return await coordinator.run_async(
                error_planned=True,
                error_type_id=error_type_id,
                attempt_runner=execute_attempt,
                system_result=system_attempt,
            )
        return await coordinator.run_async(
            error_planned=error_planned,
            error_type_id=error_type_id if error_planned else None,
            attempt_runner=execute_attempt,
        )

    try:
        outcome = asyncio.run(run_semantic_loop())
    except ProviderRoutesExhausted as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI service is temporarily unavailable. Please retry this turn.",
        ) from exc
    except SemanticLoopTimeout:
        health_service.discard_pending()
        raise

    if outcome.failure_reason == "structured_mutation_disclosure":
        health_service.discard_pending()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI response could not be safely presented. Please retry this turn.",
        )

    execution = outcome.final_result.final_value
    assert isinstance(execution, _SemanticCandidateExecution)
    _attach_semantic_evidence(
        execution.turn_result,
        outcome=outcome,
        error_planned=error_planned,
        error_type_id=error_type_id,
    )
    return ExecutedTurnSubmission(
        provider_result=execution.provider_result,
        provider_response=execution.provider_response,
        turn_result=execution.turn_result,
    )


def _prompt_native_error_evidence(
    *,
    scenario: Scenario,
    error_type_id: str,
    provider_result: ProviderResponse | StructuredAgentResult,
) -> ErrorMutation:
    rule = scenario.mutation_policy.rules[error_type_id]
    provider_response = _provider_response(provider_result)
    if (
        rule.presentation == "simulated_ui"
        and isinstance(provider_result, StructuredAgentResult)
        and provider_result.value is not None
    ):
        generated_projection: Any = provider_result.value.model_dump(
            mode="json",
            by_alias=True,
            exclude={"assistant_text"},
        )
    elif (
        isinstance(provider_result, StructuredAgentResult)
        and provider_result.value is not None
    ):
        generated_projection = provider_result.value.assistant_text
    else:
        generated_projection = provider_response.text
    return ErrorMutation(
        error_type_id=error_type_id,
        severity=ERROR_SEVERITY_BY_TYPE[error_type_id],
        presentation=rule.presentation,
        target_kind=rule.target_kind,
        target_path=(
            "artifact"
            if rule.presentation == "simulated_ui"
            else "assistant_text"
        ),
        original_value=None,
        mutated_value=generated_projection,
        applied=True,
        centrality=rule.centrality,
        operation="prompt_native_generation",
        magnitude=rule.centrality,
        agent_generated=True,
    )


def submit_turn(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    request: TurnSubmitRequest,
    settings: Settings | None = None,
    health_service: ApiHealthService | None = None,
    expected_turn_index: int | None = None,
    provider_result_override: ProviderResponse | StructuredAgentResult | None = None,
    turn_result_override: GraphRunResult | None = None,
) -> TurnView:
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=request.session_id,
        attempt_id=attempt_id,
    )
    SessionStateMachine.ensure_started(session_row)

    turn_index = _require_expected_turn_index(conn, session_id=int(session_row["id"]))
    if expected_turn_index is not None and turn_index != expected_turn_index:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn operation is stale for the current session state.",
        )

    if not bool(session_row["is_test"]) and request.input_mode != "voice":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formal sessions require voice input.",
        )

    app_settings = settings or get_settings()
    router_health_service = health_service or ApiHealthService(conn)
    user_text = request.user_text or ""
    user_audio_path = None
    user_audio_sha256 = None
    asr_provider = None
    asr_status = "not_used"
    asr_text = None
    asr_latency_ms = None
    if request.input_mode == "voice":
        asr_attempt_row = _require_matching_asr_success(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
            session_id=int(session_row["id"]),
            turn_index=turn_index,
            request=request,
        )
        user_text = str(asr_attempt_row["asr_text"] or "")
        user_audio_path = str(asr_attempt_row["user_audio_path"])
        user_audio_sha256 = str(asr_attempt_row["user_audio_sha256"])
        asr_provider = (
            str(asr_attempt_row["asr_provider"])
            if asr_attempt_row["asr_provider"] is not None
            else None
        )
        asr_status = str(asr_attempt_row["asr_status"])
        asr_text = str(asr_attempt_row["asr_text"] or "")
        asr_latency_ms = (
            int(asr_attempt_row["asr_latency_ms"])
            if asr_attempt_row["asr_latency_ms"] is not None
            else None
        )
    elif not user_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Test text submissions require user text.",
        )
    planned_system_failure = (
        turn_index == int(session_row["planned_error_turn"])
        and str(session_row["error_type_id"]) == "system_failure"
    )
    weather_tool: dict[str, Any] | None = None
    if provider_result_override is not None:
        provider_result = provider_result_override
    elif planned_system_failure:
        provider_result: ProviderResponse | StructuredAgentResult = _planned_system_failure_response()
    else:
        scenario = ScenarioRegistry.load_default().resolve_persisted(
            condition=str(session_row["condition"]),
            subcondition=str(session_row["subcondition"]),
            topic_key=str(session_row["topic_key"]),
        )
        if scenario.topic_key == "weather":
            weather_execution = asyncio.run(
                _run_weather_turn(
                    settings=app_settings,
                    user_text=user_text,
                    previous_source=_latest_successful_weather_source(
                        conn,
                        session_id=int(session_row["id"]),
                    ),
                )
            )
            provider_result = weather_execution.provider_response
            weather_tool = weather_execution.weather_tool
        else:
            router = ProviderRouter(
                settings=app_settings,
                health_service=router_health_service,
            )
            provider_messages = _provider_messages_for_session(
                conn,
                session_row=session_row,
                user_text=user_text,
            )
            if str(session_row["subcondition"]) == "execution":
                provider_result = asyncio.run(
                    router.generate_structured_agent(
                        request_id=f"{session_row['session_uuid']}-turn-{turn_index}",
                        messages=provider_messages,
                        is_test=bool(session_row["is_test"]),
                        schema=schema_for_artifact(
                            condition=str(session_row["condition"]),
                            subcondition=str(session_row["subcondition"]),
                        ),
                    )
                )
            else:
                provider_result = asyncio.run(
                    router.generate_chat(
                        request_id=f"{session_row['session_uuid']}-turn-{turn_index}",
                        messages=provider_messages,
                        is_test=bool(session_row["is_test"]),
                    )
                )
    provider_response = (
        provider_result.response
        if isinstance(provider_result, StructuredAgentResult)
        else provider_result
    )
    turn_result = turn_result_override or _build_session_turn_result(
        conn=conn,
        session_row=session_row,
        turn_index=turn_index,
        request_input_mode=request.input_mode,
        user_text=user_text,
        provider_result=provider_result,
        settings=app_settings,
        health_service=router_health_service,
        weather_tool=weather_tool,
    )
    record = turn_result.turn_record
    turn_id = insert_turn(
        conn,
        session_id=int(session_row["id"]),
        turn_index=turn_index,
        user_text=user_text,
        user_input_mode=request.input_mode,
        user_audio_path=user_audio_path,
        user_audio_sha256=user_audio_sha256,
        asr_provider=asr_provider,
        asr_status=asr_status,
        asr_text=asr_text,
        asr_latency_ms=asr_latency_ms,
        assistant_text=record["assistant_text"],
        response_latency_ms=record["response_latency_ms"],
        llm_provider=record["llm_provider"],
        llm_model=record["llm_model"],
        llm_route=record["llm_route"],
        llm_attempts_json=record["llm_attempts_json"],
        error_planned=record["error_planned"],
        error_type_id=record["error_type_id"],
        error_presented=record["error_presented"],
        error_presentation=record["error_presentation"],
        error_evaluator_provider=record.get("error_evaluator_provider"),
        error_evaluator_model=record.get("error_evaluator_model"),
        error_evaluator_result_json=record.get("error_evaluator_result_json"),
        error_mutation_json=record.get("error_mutation_json"),
        error_semantic_attempt_count=int(record.get("error_semantic_attempt_count", 0)),
        error_failure_reason=record.get("error_failure_reason"),
        error_attempts_json=record.get("error_attempts_json"),
        agent_state_json=record["agent_state_json"],
    )
    if record.get("manipulation_status") in {"presented", "failed"}:
        update_manipulation_status(
            conn,
            session_id=int(session_row["id"]),
            manipulation_status=str(record["manipulation_status"]),
        )
    _persist_turn_artifact(
        conn=conn,
        session_row=session_row,
        turn_id=turn_id,
        provider_result=provider_result,
        turn_result=turn_result,
    )
    turn_row = get_turn_by_id(conn, turn_id=turn_id)
    if turn_row is None:
        raise LookupError("Failed to reload turn after insert.")
    if record["error_planned"] and not record["error_presented"]:
        router_health_service.add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="error_not_presented",
            detail={
                "turn_index": turn_index,
                "error_type_id": record["error_type_id"],
                "error_presentation": record["error_presentation"],
                "semantic_failure_code": record.get("error_failure_reason"),
                "semantic_attempt_count": int(
                    record.get("error_semantic_attempt_count", 0)
                ),
            },
        )
    if provider_response.used_local_fallback:
        router_health_service.add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="api_failure",
            detail={"turn_index": turn_index},
        )
        router_health_service.add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="local_fallback",
            detail={"turn_index": turn_index},
        )
    if turn_result.state.artifact_validation_status == "invalid":
        router_health_service.add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="artifact_schema_error",
            detail={
                "turn_index": turn_index,
                "validation_error": turn_result.state.artifact_validation_error,
            },
        )
    return _build_turn_view(
        conn,
        turn_row,
        session_is_test=bool(session_row["is_test"]),
    )


def _build_turn_record(
    *,
    session_uuid: str,
    turn_index: int,
    condition: str,
    subcondition: str,
    topic_key: str,
    planned_error_turn: int,
    error_type_id: str,
    user_input_mode: str,
    user_text: str,
    provider_response: ProviderResponse,
) -> dict[str, object]:
    error_planned = turn_index == planned_error_turn
    if error_planned and error_type_id == "system_failure":
        assistant_text = SYSTEM_FAILURE_TEXT
        error_presentation = "system_failure"
        error_presented = True
    elif error_planned and generation_fallback_prevents_error_presentation(
        provider_response=provider_response
    ):
        assistant_text = provider_response.text
        error_presentation = "none"
        error_presented = False
    else:
        assistant_text = provider_response.text
        error_presentation = "assistant_text" if error_planned else "none"
        error_presented = error_planned

    return {
        "assistant_text": assistant_text,
        "response_latency_ms": next(
            (
                attempt.latency_ms
                for attempt in reversed(provider_response.attempts)
                if attempt.status == "success" and attempt.latency_ms is not None
            ),
            0,
        ),
        "llm_provider": provider_response.provider,
        "llm_model": provider_response.model,
        "llm_route": provider_response.route,
        "llm_attempts_json": to_json(
            [
                {
                    "route": attempt.route,
                    "provider": attempt.provider,
                    "model": attempt.model,
                    "status": attempt.status,
                    "http_status": attempt.http_status,
                    "cooldown_applied": attempt.cooldown_applied,
                }
                for attempt in provider_response.attempts
            ]
        ),
        "error_planned": error_planned,
        "error_type_id": error_type_id if error_planned else None,
        "error_presented": error_presented,
        "error_presentation": error_presentation,
        "agent_state_json": to_json(
            {
                "session_id": session_uuid,
                "turn_index": turn_index,
                "condition": condition,
                "subcondition": subcondition,
                "topic_key": topic_key,
                "planned_error_turn": planned_error_turn,
                **safe_input_metadata(
                    user_input_mode=user_input_mode,
                    user_text=user_text,
                ),
                "provider_route": provider_response.route,
            }
        ),
    }


def _build_session_turn_result(
    *,
    conn: sqlite3.Connection,
    session_row: sqlite3.Row,
    turn_index: int,
    request_input_mode: str,
    user_text: str,
    provider_result: ProviderResponse | StructuredAgentResult,
    settings: Settings,
    health_service: ApiHealthService,
    recent_history: list[ConversationMessage] | None = None,
    weather_tool: dict[str, Any] | None = None,
    error_agent_result: ErrorMutation | None = None,
    disable_error: bool = False,
    defer_evaluator: bool = False,
) -> GraphRunResult:
    subcondition = str(session_row["subcondition"])
    provider_response = (
        provider_result.response
        if isinstance(provider_result, StructuredAgentResult)
        else provider_result
    )
    registry = ScenarioRegistry.load_default()
    scenario = registry.resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=subcondition,
        topic_key=str(session_row["topic_key"]),
    )
    state = build_graph_state(
        session_row=_session_row_to_mapping(session_row),
        turn_index=turn_index,
        graph_input=GraphInput(
            user_text=user_text,
            input_mode=request_input_mode,
        ),
        recent_history=(
            recent_history
            if recent_history is not None
            else _recent_history_for_session(conn, session_id=int(session_row["id"]))
        ),
        scenario=scenario,
        graph_version=str(session_row["agent_graph_version"]),
        weather_tool=weather_tool,
    )
    state.error_agent_result = error_agent_result
    if disable_error:
        state.planned_error_turn = None
    graph = _build_controlled_graph(
        subcondition=subcondition,
        provider_result=provider_result,
        evaluator_runner=(
            lambda _graph_state, _assistant_text, _artifact_type, _artifact_payload: {
                "status": "pending",
                "presented": True,
                "provider": None,
                "model": None,
                "route": "evaluator",
                "parse_attempts": 0,
                "reason": "evaluator_presented",
            }
            if defer_evaluator
            else lambda graph_state, assistant_text, artifact_type, artifact_payload: _evaluate_injected_error(
                settings=settings,
                health_service=health_service,
                session_uuid=str(session_row["session_uuid"]),
                turn_index=turn_index,
                state=graph_state,
                assistant_text=assistant_text,
                artifact_type=artifact_type,
                artifact_payload=artifact_payload,
                provider_response=provider_response,
            )
        ),
    )
    return graph.run(state)


def _provider_response(
    provider_result: ProviderResponse | StructuredAgentResult,
) -> ProviderResponse:
    return (
        provider_result.response
        if isinstance(provider_result, StructuredAgentResult)
        else provider_result
    )


def _provider_status(provider_response: ProviderResponse) -> str:
    if provider_response.attempts:
        return provider_response.attempts[-1].status
    if provider_response.route == "system_failure":
        return "system_failure"
    return "success"


def _attach_semantic_evidence(
    turn_result: GraphRunResult,
    *,
    outcome,
    error_planned: bool,
    error_type_id: str,
) -> None:
    final_execution = outcome.final_result.final_value
    mutation = (
        final_execution.turn_result.state.error_mutation
        if isinstance(final_execution, _SemanticCandidateExecution)
        else None
    )
    record = turn_result.turn_record
    record["error_planned"] = error_planned
    record["error_type_id"] = error_type_id if error_planned else None
    if outcome.manipulation_status == "failed":
        turn_result.state.error_presented = False
        turn_result.state.error_presentation = "none"
        turn_result.client_response["error_presentation"] = "none"
        record["error_presented"] = False
        record["error_presentation"] = "none"
        agent_state = from_json(record.get("agent_state_json"), {})
        if isinstance(agent_state, dict):
            agent_state["error_presented"] = False
            agent_state["error_presentation"] = "none"
            record["agent_state_json"] = to_json(agent_state)
    if mutation is None:
        mutation_evidence = None
    elif outcome.manipulation_status == "failed":
        mutation_evidence = {
            "errorTypeId": mutation.error_type_id,
            "severity": mutation.severity,
            "presentation": "none",
            "targetKind": mutation.target_kind,
            "targetPath": mutation.target_path,
            "applied": False,
            "failureReason": normalize_semantic_failure_code(
                outcome.failure_reason,
                default="semantic_attempts_exhausted",
            ),
            "centrality": mutation.centrality,
            "operation": mutation.operation,
            "magnitude": mutation.magnitude,
            "agentGenerated": mutation.agent_generated,
        }
    else:
        mutation_evidence = mutation.model_dump(mode="json", by_alias=True)
    record["error_mutation_json"] = (
        to_json(mutation_evidence) if mutation_evidence is not None else None
    )
    record["error_semantic_attempt_count"] = outcome.semantic_attempt_count
    record["error_failure_reason"] = outcome.failure_reason
    record["error_attempts_json"] = to_json(
        [attempt.model_dump(mode="json") for attempt in outcome.attempts]
    )
    record["manipulation_status"] = outcome.manipulation_status


def _session_row_to_mapping(session_row: sqlite3.Row) -> dict[str, Any]:
    return {key: session_row[key] for key in session_row.keys()}


def _recent_history_for_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> list[ConversationMessage]:
    history: list[ConversationMessage] = []
    for row in list_turns_for_session(conn, session_id=session_id):
        history.append(
            ConversationMessage(role="user", text=str(row["user_text"] or ""))
        )
        history.append(
            ConversationMessage(role="assistant", text=str(row["assistant_text"] or ""))
        )
    return history


def _provider_messages_for_session(
    conn: sqlite3.Connection,
    *,
    session_row: sqlite3.Row,
    user_text: str,
) -> list[ProviderMessage]:
    scenario = ScenarioRegistry.load_default().resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
    )
    messages = [ProviderMessage(role="system", content=scenario.provider_system_prompt)]
    messages.extend(
        ProviderMessage(role=message.role, content=message.text)
        for message in _recent_history_for_session(
            conn,
            session_id=int(session_row["id"]),
        )
    )
    messages.append(ProviderMessage(role="user", content=user_text))
    return messages


def _latest_successful_weather_source(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> WeatherSnapshot | None:
    for state_json in list_recent_weather_agent_states(conn, session_id=session_id):
        state = from_json(state_json, None)
        if not isinstance(state, dict):
            continue
        weather_tool = state.get("weather_tool")
        if not isinstance(weather_tool, dict) or weather_tool.get("status") != "success":
            continue
        source = weather_tool.get("source")
        if not isinstance(source, dict):
            continue
        try:
            return WeatherSnapshot.model_validate(source)
        except ValueError:
            continue
    return None


async def _run_weather_turn(
    *,
    settings: Settings,
    user_text: str,
    previous_source: WeatherSnapshot | None,
) -> WeatherTurnExecution:
    query = extract_weather_location(user_text)
    if query is None and previous_source is not None:
        query = previous_source.query
    if query is None:
        return WeatherTurnExecution(
            provider_response=_weather_provider_response(
                text=WEATHER_LOCATION_REQUIRED_TEXT,
                provider="local-system",
                model="weather-location-clarification-v1",
                status="success",
            ),
            weather_tool={
                "status": "clarification",
                "error_code": "location_required",
            },
        )

    started_at = perf_counter()
    try:
        snapshot = await WeatherService(settings=settings).lookup(query)
    except WeatherServiceError as exc:
        text = (
            WEATHER_LOCATION_NOT_FOUND_TEXT
            if exc.code == "location_not_found"
            else WEATHER_UNAVAILABLE_TEXT
        )
        status = {
            "timeout": "timeout",
            "transport_error": "http_error",
            "http_error": "http_error",
            "invalid_response": "invalid_response",
            "location_not_found": "invalid_response",
        }[exc.code]
        return WeatherTurnExecution(
            provider_response=_weather_provider_response(
                text=text,
                provider="openmeteo",
                model="weather-service-v1",
                status=status,
                latency_ms=int((perf_counter() - started_at) * 1000),
            ),
            weather_tool={
                "status": "failed",
                "error_code": exc.code,
                "query": query,
            },
        )

    participant_card = render_weather_card(snapshot, user_text)
    return WeatherTurnExecution(
        provider_response=_weather_provider_response(
            text=render_weather_text(snapshot, user_text),
            provider="openmeteo",
            model="weather-snapshot-v1",
            status="success",
            latency_ms=int((perf_counter() - started_at) * 1000),
        ),
        weather_tool={
            "status": "success",
            "source": snapshot.model_dump(mode="json"),
            "participant_card": participant_card,
        },
    )


def _weather_provider_response(
    *,
    text: str,
    provider: str,
    model: str,
    status: str,
    latency_ms: int | None = None,
) -> ProviderResponse:
    return ProviderResponse(
        text=text,
        provider=provider,
        model=model,
        route="weather",
        attempts=[
            ProviderAttempt(
                route="weather",
                provider=provider,
                model=model,
                status=status,
                latency_ms=latency_ms,
                error_code=None if status == "success" else status,
                cooldown_applied=False,
            )
        ],
        used_local_fallback=False,
    )


def _build_controlled_graph(
    *,
    subcondition: str,
    provider_result: ProviderResponse | StructuredAgentResult,
    evaluator_runner=None,
):
    def provider_runner(_state):
        return provider_result

    builders = {
        "qa": build_qa_graph,
        "planning": build_planning_graph,
        "chat": build_chat_graph,
        "decision": build_decision_graph,
        "execution": build_execution_graph,
    }
    builder = builders.get(subcondition)
    if builder is None:
        raise NotImplementedError(f"Controlled graph is not implemented for {subcondition}.")
    return builder(provider_runner=provider_runner, evaluator_runner=evaluator_runner)


def _generate_planned_error_turn() -> int:
    return random.choice((2, 3, 4))


def _evaluate_injected_error(
    *,
    settings: Settings,
    health_service: ApiHealthService,
    session_uuid: str,
    turn_index: int,
    state,
    assistant_text: str,
    artifact_type: str | None,
    artifact_payload: dict[str, Any] | None,
    session_history: list[ConversationMessage] | None = None,
    current_user_text: str = "",
    weather_context: str | None = None,
    provider_response: ProviderResponse | None = None,
) -> dict[str, Any] | None:
    if state.planned_error_turn != state.turn_index:
        return None
    if state.error_type_id == "system_failure":
        return None
    if generation_fallback_prevents_error_presentation(
        provider_response=provider_response,
        provider_name=state.llm_provider,
        provider_status=state.provider_status,
        provider_route=state.llm_route,
    ):
        return {
            "status": "failed",
            "presented": False,
            "provider": state.llm_provider,
            "model": state.llm_model,
            "route": state.llm_route or "chat",
            "reason": "generation_local_fallback",
        }
    if state.error_presentation == "none":
        return {
            "status": "failed",
            "presented": False,
            "provider": None,
            "model": None,
            "route": "evaluator",
            "reason": "error_not_injected",
        }

    if settings.app_env == "test":
        return {
            "status": "success",
            "presented": True,
            "provider": "deepseek",
            "model": settings.deepseek_model,
            "route": "evaluator",
            "parse_attempts": 1,
            "attempts": [
                {
                    "route": "evaluator",
                    "provider": "deepseek",
                    "model": settings.deepseek_model,
                    "used_local_fallback": False,
                }
            ],
            "reason": "test_mode_deterministic_evaluator",
        }

    router = ProviderRouter(settings=settings, health_service=health_service)
    evaluator = ErrorEvaluator(
        runner=lambda messages: asyncio.run(
            router.generate_evaluator(
                request_id=f"{session_uuid}-turn-{turn_index}-evaluator",
                messages=messages,
            )
        )
    )
    return evaluator.evaluate(
        state=state,
        assistant_text=assistant_text,
        artifact_type=artifact_type,
        artifact_payload=artifact_payload,
        session_history=session_history or (),
        current_user_text=current_user_text,
        weather_context=weather_context,
    )


async def _evaluate_injected_error_async(
    *,
    settings: Settings,
    health_service: ApiHealthService,
    session_uuid: str,
    turn_index: int,
    state,
    assistant_text: str,
    artifact_type: str | None,
    artifact_payload: dict[str, Any] | None,
    session_history: list[ConversationMessage] | None = None,
    current_user_text: str = "",
    weather_context: str | None = None,
    provider_response: ProviderResponse | None = None,
) -> dict[str, Any]:
    if settings.app_env == "test":
        result = _evaluate_injected_error(
            settings=settings,
            health_service=health_service,
            session_uuid=session_uuid,
            turn_index=turn_index,
            state=state,
            assistant_text=assistant_text,
            artifact_type=artifact_type,
            artifact_payload=artifact_payload,
            session_history=session_history,
            current_user_text=current_user_text,
            weather_context=weather_context,
            provider_response=provider_response,
        )
        normalized = dict(result or {})
    elif generation_fallback_prevents_error_presentation(
        provider_response=provider_response,
        provider_name=state.llm_provider,
        provider_status=state.provider_status,
        provider_route=state.llm_route,
    ):
        normalized = {
            "status": "failed",
            "presented": False,
            "provider": state.llm_provider,
            "model": state.llm_model,
            "route": state.llm_route or "chat",
            "reason": "generation_local_fallback",
        }
    else:
        router = ProviderRouter(settings=settings, health_service=health_service)
        evaluator = ErrorEvaluator(
            runner=lambda _messages: (_ for _ in ()).throw(
                RuntimeError("sync evaluator runner is unavailable")
            )
        )
        normalized = await evaluator.evaluate_async(
            runner=lambda messages: router.generate_evaluator(
                request_id=f"{session_uuid}-turn-{turn_index}-evaluator",
                messages=messages,
            ),
            state=state,
            assistant_text=assistant_text,
            artifact_type=artifact_type,
            artifact_payload=artifact_payload,
            session_history=session_history or (),
            current_user_text=current_user_text,
            weather_context=weather_context,
        )
    normalized["reason"] = normalize_semantic_failure_code(
        normalized.get("reason"),
        default=(
            "evaluator_presented"
            if bool(normalized.get("presented"))
            else "evaluator_not_presented"
        ),
    )
    return normalized


def _apply_deferred_evaluator_result(
    turn_result: GraphRunResult,
    evaluator: dict[str, Any],
) -> None:
    persisted_evaluator = {
        key: value
        for key, value in evaluator.items()
        if key != "feedback_reason"
    }
    presented = bool(persisted_evaluator.get("presented"))
    turn_result.state.error_presented = presented
    turn_result.state.evaluator_result = dict(persisted_evaluator)
    turn_result.state.error_evaluator_provider = persisted_evaluator.get("provider")
    turn_result.state.error_evaluator_model = persisted_evaluator.get("model")
    record = turn_result.turn_record
    record["error_presented"] = presented
    record["error_evaluator_provider"] = persisted_evaluator.get("provider")
    record["error_evaluator_model"] = persisted_evaluator.get("model")
    record["error_evaluator_result_json"] = to_json(persisted_evaluator)
    agent_state = from_json(record.get("agent_state_json"), {})
    if isinstance(agent_state, dict):
        agent_state["error_presented"] = presented
        record["agent_state_json"] = to_json(agent_state)


def _persist_turn_artifact(
    *,
    conn: sqlite3.Connection,
    session_row: sqlite3.Row,
    turn_id: int,
    provider_result: ProviderResponse | StructuredAgentResult,
    turn_result: GraphRunResult,
) -> None:
    scenario = ScenarioRegistry.load_default().resolve_persisted(
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
    )
    if scenario.artifact_type and turn_result.turn_record["error_presentation"] == "system_failure":
        insert_failed_task_artifact(
            conn,
            turn_id=turn_id,
            artifact_type=scenario.artifact_type,
            payload_json=to_json({"code": "system_failure"}),
        )
        return
    if scenario.artifact_type and isinstance(provider_result, StructuredAgentResult):
        if (
            provider_result.value is None
            or turn_result.state.artifact_validation_status == "invalid"
        ):
            insert_failed_task_artifact(
                conn,
                turn_id=turn_id,
                artifact_type=scenario.artifact_type,
                payload_json=to_json({"code": "artifact_schema_invalid"}),
            )
            return
        structured_status = getattr(provider_result.value, "status", "completed")
        if structured_status in {"pending", "clarify"}:
            insert_task_artifact(
                conn,
                turn_id=turn_id,
                artifact_type=scenario.artifact_type,
                status="draft",
                payload_json=to_json({"code": "structured_result_pending"}),
                visible_to_participant=False,
            )
            return
        if structured_status == "failed":
            insert_failed_task_artifact(
                conn,
                turn_id=turn_id,
                artifact_type=scenario.artifact_type,
                payload_json=to_json({"code": "structured_result_failed"}),
            )
            return
    artifact_type = turn_result.client_response.get("artifact_type")
    artifact_payload = turn_result.client_response.get("artifact_payload")
    if artifact_type and artifact_payload:
        insert_task_artifact(
            conn,
            turn_id=turn_id,
            artifact_type=str(artifact_type),
            status="completed",
            payload_json=to_json(artifact_payload),
            visible_to_participant=True,
        )


def _require_expected_turn_index(conn: sqlite3.Connection, *, session_id: int) -> int:
    turn_rows = list_turns_for_session(conn, session_id=session_id)
    if turn_rows:
        last_turn = turn_rows[-1]
        if last_turn["rating_id"] is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Turn {last_turn['turn_index']} must be rated before the next turn.",
            )
    if len(turn_rows) >= MAX_TURNS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session already has the maximum 5 turns.",
        )
    return len(turn_rows) + 1


def _require_matching_asr_success(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    turn_index: int,
    request: TurnSubmitRequest,
) -> sqlite3.Row:
    if request.asr_result_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Voice submissions require a successful ASR result.",
        )
    attempt_row = get_successful_asr_attempt_by_result_ref(
        conn,
        result_ref=request.asr_result_id,
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_id=session_id,
        turn_index=turn_index,
    )
    if attempt_row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Voice submissions require a matching successful ASR result.",
        )
    return attempt_row


def _persist_audio_file(
    *,
    conn: sqlite3.Connection,
    settings: Settings,
    participant_id: int,
    session_row: sqlite3.Row,
    turn_index: int,
    filename: str,
    staged_audio_path: Path,
) -> str:
    audio_dir = settings.data_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower()
    participant_row = _require_participant(conn, participant_id=participant_id)
    participant_type = str(participant_row["participant_type"])
    if session_row["attempt_id"] is not None:
        attempt_row = get_attempt_by_id(conn, attempt_id=int(session_row["attempt_id"]))
        if attempt_row is not None:
            participant_type = str(attempt_row["participant_type"])
    relative_audio_path = canonical_audio_relative_path(
        name=str(participant_row["name"]),
        phone=str(participant_row["phone"]),
        participant_type=participant_type,
        day_index=int(session_row["day_index"]),
        turn_index=turn_index,
        session_id=str(session_row["session_uuid"]),
        suffix=suffix,
    )
    output_path = settings.data_dir / relative_audio_path
    if output_path.exists():
        base = output_path.with_suffix("")
        suffix = output_path.suffix
        retry_index = 2
        while output_path.exists():
            output_path = base.with_name(f"{base.name}_retry_{retry_index}").with_suffix(
                suffix
            )
            retry_index += 1
        relative_audio_path = str(output_path.relative_to(settings.data_dir))
    staged_audio_path.replace(output_path)
    return relative_audio_path


def _log_asr_attempt(
    *,
    health_service: ApiHealthService,
    request_id: str,
    asr_result: AsrResult,
) -> None:
    from backend.app.services.api_health import LoggedProviderAttempt

    logged_status = asr_result.status
    if logged_status == "failed":
        logged_status = "invalid_response"

    health_service.log_attempt(
        request_id=request_id,
        attempt=LoggedProviderAttempt(
            route="asr",
            provider=asr_result.provider,
            model=None,
            status=logged_status,
            latency_ms=asr_result.latency_ms,
        ),
    )


def _planned_system_failure_response() -> ProviderResponse:
    return ProviderResponse(
        text=SYSTEM_FAILURE_TEXT,
        provider="local-system",
        model="planned-system-failure-v1",
        route="system_failure",
        attempts=[],
        used_local_fallback=False,
    )


def submit_rating(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    turn_id: int,
    request: RatingSubmitRequest,
    attempt_id: int | None = None,
    settings: Settings | None = None,
) -> RatingView | SessionView:
    turn_row = get_turn_by_id(conn, turn_id=turn_id)
    if turn_row is None or int(turn_row["participant_id"]) != participant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Turn not found.",
        )
    if str(turn_row["session_status"]) != "started":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is not active: {turn_row['session_status']}.",
        )
    if get_rating_for_turn(conn, turn_id=turn_id) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Turn rating already submitted.",
        )

    submitted_at = timestamp_now()
    insert_rating(
        conn,
        turn_id=turn_id,
        stance_score=request.stance_score,
        trust_score=request.trust_score,
        submitted_at=submitted_at,
        client_elapsed_ms=request.client_elapsed_ms,
    )
    rating_view = RatingView(
        turn_id=turn_id,
        stance_score=request.stance_score,
        trust_score=request.trust_score,
        submitted_at=submitted_at,
        client_elapsed_ms=request.client_elapsed_ms,
    )
    session_turns = list_turns_for_session(
        conn,
        session_id=int(turn_row["session_id"]),
    )
    if len(session_turns) != MAX_TURNS:
        return rating_view

    return complete_session(
        conn,
        participant_id=participant_id,
        session_uuid=str(turn_row["session_uuid"]),
        attempt_id=attempt_id,
        settings=settings,
    )


def _require_five_rated_turns(
    conn: sqlite3.Connection,
    *,
    session_row: sqlite3.Row,
) -> None:
    turn_rows = list_turns_for_session(conn, session_id=int(session_row["id"]))
    if any(turn_row["rating_id"] is None for turn_row in turn_rows):
        ApiHealthService(conn).add_session_risk_flag(
            session_id=int(session_row["id"]),
            flag="missing_rating",
            detail={
                "missing_turn_indexes": [
                    int(turn_row["turn_index"])
                    for turn_row in turn_rows
                    if turn_row["rating_id"] is None
                ],
            },
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=MISSING_RATING_COMPLETE_DETAIL,
        )
    if len(turn_rows) != MAX_TURNS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session requires exactly 5 rated turns before completion.",
        )


def complete_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    session_uuid: str,
    attempt_id: int | None = None,
    settings: Settings | None = None,
) -> SessionView:
    session_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=session_uuid,
        attempt_id=attempt_id,
    )
    if str(session_row["status"]) == "completed":
        _require_five_rated_turns(conn, session_row=session_row)
        return _build_session_view(conn, session_row=session_row)

    SessionStateMachine.ensure_started(session_row)
    _require_five_rated_turns(conn, session_row=session_row)

    completed_at = timestamp_now()
    update_session_status(
        conn,
        session_id=int(session_row["id"]),
        status="completed",
        completed_at=completed_at,
    )
    if not bool(session_row["is_test"]):
        complete_participant_day(
            conn,
            participant_id=participant_id,
            participant_day_id=int(session_row["participant_day_id"]),
            attempt_id=(
                int(session_row["attempt_id"])
                if session_row["attempt_id"] is not None
                else None
            ),
            completed_at=completed_at,
        )
        if settings is not None:
            refresh_participant_clean_data_audit(
                conn,
                settings=settings,
                participant_id=participant_id,
            )
    completed_row = _require_session(
        conn,
        participant_id=participant_id,
        session_uuid=session_uuid,
        attempt_id=attempt_id,
    )
    return _build_session_view(conn, session_row=completed_row)
