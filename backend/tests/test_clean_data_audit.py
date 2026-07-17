from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

import pytest

from backend.app.db import get_connection, run_migrations
from backend.app.repositories.admin import AdminRepository
from backend.app.repositories.attempts import create_attempt
from backend.app.services.clean_data import (
    CleanDataAuditResult,
    audit_participant_clean_data,
    persist_clean_data_audit,
    read_audio_evidence,
)
from backend.app.services.sessions import TEST_CHANNEL_PHONE_HASH
from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "clean-data.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt="task13-salt",
        admin_password_hash="hash",
    )


@pytest.fixture
def conn(sqlite_settings: Settings) -> sqlite3.Connection:
    connection = get_connection(sqlite_settings)
    run_migrations(connection)
    yield connection
    connection.close()


def _insert_participant(
    conn: sqlite3.Connection,
    *,
    participant_type: str = "short",
    current_status: str = "completed",
    created_at: str = "2026-07-02T09:00:00+08:00",
    name: str | None = None,
    phone: str = "13800138000",
) -> int:
    target_days = 1 if participant_type == "short" else 3
    cursor = conn.execute(
        """
        INSERT INTO participants (
            name,
            phone,
            phone_hash,
            participant_type,
            condition,
            subcondition,
            topic_key,
            error_type_id,
            target_days,
            current_status,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'topic-qa', 'factual_minor', ?, ?, ?, ?)
        """,
        (
            name or f"Participant-{participant_type}",
            phone,
            f"hash-{participant_type}-{created_at}",
            participant_type,
            target_days,
            current_status,
            created_at,
            created_at,
        ),
    )
    participant_id = int(cursor.lastrowid)
    attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type=participant_type,
        condition="human",
        subcondition="qa",
        topic_key="topic-qa",
        error_type_id="factual_minor",
        target_days=target_days,
        status="completed" if current_status == "completed" else "active",
        valid_for_export=current_status != "withdrawn",
    )
    conn.execute(
        "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
        (attempt_id, participant_id),
    )
    return participant_id


def _insert_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None = None,
    day_index: int = 1,
    status: str = "final",
    created_at: str = "2026-07-02T09:05:00+08:00",
) -> None:
    resolved_attempt_id = attempt_id
    if resolved_attempt_id is None:
        row = conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        resolved_attempt_id = int(row["current_attempt_id"])
    conn.execute(
        """
        INSERT INTO pretest_responses (
            participant_id,
            attempt_id,
            day_index,
            status,
            payload_json,
            autosave_count,
            last_saved_at,
            submitted_at,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            day_index,
            status,
            json.dumps({"ai_familiarity": 4}, ensure_ascii=False, sort_keys=True),
            created_at,
            created_at,
            created_at,
            created_at,
        ),
    )


def _insert_formal_day_with_session(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    participant_id: int,
    attempt_id: int | None = None,
    day_index: int,
    session_uuid: str,
    session_status: str = "completed",
    day_status: str = "completed",
    is_test: bool = False,
    turn_count: int = 5,
    missing_rating_turn_indexes: set[int] | None = None,
    missing_audio_turn_indexes: set[int] | None = None,
    non_voice_turn_indexes: set[int] | None = None,
    asr_status: str = "success",
    error_type_id: str = "factual_minor",
    external_evidence: bool = True,
    external_evidence_scoped: bool = True,
) -> int:
    missing_rating_turn_indexes = missing_rating_turn_indexes or set()
    missing_audio_turn_indexes = missing_audio_turn_indexes or set()
    non_voice_turn_indexes = non_voice_turn_indexes or set()
    created_at = f"2026-07-0{day_index}T10:00:00+08:00"
    resolved_attempt_id = attempt_id
    if resolved_attempt_id is None:
        row = conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        resolved_attempt_id = int(row["current_attempt_id"])
    day_cursor = conn.execute(
        """
        INSERT INTO participant_days (
            participant_id,
            attempt_id,
            day_index,
            calendar_date,
            status,
            started_at,
            completed_at,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            day_index,
            f"2026-07-0{day_index}",
            day_status,
            created_at,
            created_at if day_status == "completed" else None,
            created_at,
            created_at,
        ),
    )
    participant_day_id = int(day_cursor.lastrowid)
    session_cursor = conn.execute(
        """
        INSERT INTO experiment_sessions (
            participant_id,
            attempt_id,
            participant_day_id,
            session_uuid,
            condition,
            subcondition,
            topic_key,
            scenario_id,
            agent_graph_version,
            error_type_id,
            planned_error_turn,
            status,
            started_at,
            completed_at,
            client_info_json,
            is_test,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'topic-qa', 'scenario-1', 'graph-v1', ?, 2, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            participant_day_id,
            session_uuid,
            error_type_id,
            session_status,
            created_at,
            created_at if session_status == "completed" else None,
            json.dumps({"platform": "desktop"}, ensure_ascii=False, sort_keys=True),
            1 if is_test else 0,
            created_at,
            created_at,
        ),
    )
    session_id = int(session_cursor.lastrowid)

    for turn_index in range(1, turn_count + 1):
        has_audio = turn_index not in missing_audio_turn_indexes
        audio_bytes = f"audio-{session_uuid}-{turn_index}".encode()
        audio_relative_path = f"audio/{session_uuid}-{turn_index}.wav"
        audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
        if has_audio:
            audio_path = settings.data_dir / audio_relative_path
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(audio_bytes)
        turn_cursor = conn.execute(
            """
            INSERT INTO conversation_turns (
                session_id,
                turn_index,
                user_text,
                user_input_mode,
                user_audio_path,
                user_audio_sha256,
                asr_provider,
                asr_status,
                asr_text,
                asr_latency_ms,
                assistant_text,
                response_latency_ms,
                llm_provider,
                llm_model,
                llm_route,
                llm_attempts_json,
                error_planned,
                error_type_id,
                error_presented,
                error_presentation,
                error_evaluator_provider,
                error_evaluator_model,
                error_evaluator_result_json,
                agent_state_json,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'tencent', ?, ?, 1200, ?, 800, 'yi-zhan', 'gpt-5.1', 'chat', ?, 0, NULL, 0, 'none', NULL, NULL, NULL, ?, ?)
            """,
            (
                session_id,
                turn_index,
                f"user-{session_uuid}-{turn_index}",
                "text_test_only" if turn_index in non_voice_turn_indexes else "voice",
                audio_relative_path if has_audio else None,
                audio_sha256 if has_audio else None,
                asr_status,
                f"asr-{session_uuid}-{turn_index}" if asr_status == "success" else "",
                f"assistant-{session_uuid}-{turn_index}",
                json.dumps([{"provider": "yi-zhan"}], ensure_ascii=False),
                json.dumps({"turn_index": turn_index}, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        turn_id = int(turn_cursor.lastrowid)
        if has_audio and asr_status == "success":
            conn.execute(
                """
                INSERT INTO asr_attempts (
                    session_id,
                    turn_index,
                    attempt_no,
                    user_audio_path,
                    user_audio_sha256,
                    asr_provider,
                    asr_status,
                    asr_text,
                    asr_latency_ms
                ) VALUES (?, ?, 1, ?, ?, 'tencent', 'success', ?, 1200)
                """,
                (
                    session_id,
                    turn_index,
                    audio_relative_path,
                    audio_sha256,
                    f"asr-{session_uuid}-{turn_index}",
                ),
            )
        if turn_index not in missing_rating_turn_indexes:
            conn.execute(
                """
                INSERT INTO turn_ratings (
                    turn_id,
                    stance_score,
                    trust_score,
                    submitted_at,
                    client_elapsed_ms
                ) VALUES (?, 4, 6, ?, 900)
                """,
                (turn_id, created_at),
            )

        if external_evidence:
            scope = (
                (session_id, turn_index, 1 if is_test else 0)
                if external_evidence_scoped
                else (None, None, None)
            )
            routes = ["asr"]
            if not (error_type_id == "system_failure" and turn_index == 2):
                routes.append("chat")
            if error_type_id != "system_failure" and turn_index == 2:
                routes.append("evaluator")
            for route in routes:
                conn.execute(
                    """
                    INSERT INTO api_call_logs (
                        request_id,
                        session_id,
                        turn_index,
                        is_test,
                        route,
                        provider,
                        model,
                        status,
                        cooldown_applied
                    ) VALUES (?, ?, ?, ?, ?, 'provider', 'model', 'success', 0)
                    """,
                    (
                        f"{session_uuid}-turn-{turn_index}-{route}",
                        *scope,
                        route,
                    ),
                )

    return session_id


def _weather_source_payload() -> dict[str, object]:
    return {
        "provider": "openmeteo",
        "query": "杭州",
        "fetched_at": "2026-07-12T11:02:00Z",
        "location": {
            "name": "杭州",
            "admin1": "浙江",
            "admin2": "杭州市",
            "country": "中国",
            "country_code": "CN",
            "latitude": 30.29365,
            "longitude": 120.16142,
            "timezone": "Asia/Shanghai",
        },
        "current": {
            "time": "2026-07-12T19:00",
            "temperature_c": 28.2,
            "relative_humidity_percent": 84,
            "apparent_temperature_c": 32.2,
            "wind_speed_mps": 5.35,
            "weather_code": 3,
        },
        "daily": [
            {
                "date": f"2026-07-{12 + offset:02d}",
                "weather_code": 80,
                "temperature_max_c": 31,
                "temperature_min_c": 25,
                "precipitation_probability_percent": 70,
                "wind_speed_max_mps": 8,
            }
            for offset in range(7)
        ],
    }


def _configure_weather_audit_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    topic_key: str,
    weather_tool: dict[str, object],
    llm_provider: str = "openmeteo",
    llm_model: str = "weather-snapshot-v1",
) -> None:
    conn.execute(
        """
        UPDATE experiment_sessions
        SET condition = 'tool',
            subcondition = 'qa',
            topic_key = ?
        WHERE id = ?
        """,
        (topic_key, session_id),
    )
    conn.execute(
        "DELETE FROM api_call_logs WHERE session_id = ? AND route = 'chat'",
        (session_id,),
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET llm_provider = ?,
            llm_model = ?,
            llm_route = 'weather',
            llm_attempts_json = '[]',
            agent_state_json = ?
        WHERE session_id = ?
        """,
        (
            llm_provider,
            llm_model,
            json.dumps({"weather_tool": weather_tool}, ensure_ascii=False),
            session_id,
        ),
    )


def test_clean_data_audit_accepts_complete_short_participant(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-short-complete",
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "eligible"
    assert result.reasons == []


def test_clean_data_audit_excludes_failed_manipulation(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-failed-manipulation",
    )
    conn.execute(
        "UPDATE experiment_sessions SET manipulation_status = 'failed' WHERE id = ?",
        (session_id,),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "error_not_presented" in result.reasons


@pytest.mark.parametrize("evidence_kind", ["absent", "unscoped"])
def test_clean_data_audit_marks_completed_legacy_session_for_review_when_positive_external_evidence_is_missing(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    evidence_kind: str,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid=f"legacy-{evidence_kind}-external-evidence",
        external_evidence=evidence_kind != "absent",
        external_evidence_scoped=evidence_kind != "unscoped",
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "review_needed"
    assert result.reasons == ["external_api_evidence_missing"]


@pytest.mark.parametrize("topic_key", ["weather", "factual_lookup"])
def test_clean_data_audit_accepts_weather_provenance_without_chat_log(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    topic_key: str,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid=f"weather-{topic_key}-clean-data",
    )
    _configure_weather_audit_session(
        conn,
        session_id=session_id,
        topic_key=topic_key,
        weather_tool={"status": "success", "source": _weather_source_payload()},
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "eligible"
    assert result.reasons == []
    assert conn.execute(
        "SELECT COUNT(*) FROM api_call_logs WHERE session_id = ? AND route = 'chat'",
        (session_id,),
    ).fetchone()[0] == 0


def test_clean_data_audit_accepts_ai_generated_planned_weather_error(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="weather-ai-planned-error",
    )
    _configure_weather_audit_session(
        conn,
        session_id=session_id,
        topic_key="weather",
        weather_tool={"status": "success", "source": _weather_source_payload()},
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET llm_provider = 'yi-zhan',
            llm_model = 'gpt-5.1',
            llm_route = 'chat',
            error_planned = 1,
            error_type_id = 'factual_minor',
            error_presented = 1,
            error_presentation = 'assistant_text'
        WHERE session_id = ? AND turn_index = 2
        """,
        (session_id,),
    )
    conn.execute(
        """
        INSERT INTO api_call_logs (
            request_id, session_id, turn_index, is_test, route,
            provider, model, status, cooldown_applied
        ) VALUES (?, ?, 2, 0, 'chat', 'yi-zhan', 'gpt-5.1', 'success', 0)
        """,
        ("weather-ai-planned-error-turn-2-chat", session_id),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "eligible"
    assert result.reasons == []


@pytest.mark.parametrize(
    "mutate_source",
    [
        pytest.param(lambda source: source.pop("provider"), id="provider"),
        pytest.param(lambda source: source.pop("query"), id="query"),
        pytest.param(
            lambda source: source["location"].pop("latitude"),
            id="coordinates",
        ),
        pytest.param(lambda source: source.pop("fetched_at"), id="fetched-at"),
        pytest.param(
            lambda source: source.__setitem__("fetched_at", 0),
            id="fetched-at-type",
        ),
        pytest.param(
            lambda source: source["current"].pop("temperature_c"),
            id="current",
        ),
        pytest.param(lambda source: source.pop("daily"), id="daily"),
    ],
)
def test_clean_data_audit_rejects_incomplete_weather_provenance(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    mutate_source,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="weather-invalid-provenance",
    )
    source = _weather_source_payload()
    mutate_source(source)
    _configure_weather_audit_session(
        conn,
        session_id=session_id,
        topic_key="weather",
        weather_tool={"status": "success", "source": source},
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert result.reasons == ["weather_provenance_invalid"]


@pytest.mark.parametrize(
    ("weather_tool", "provider", "model", "expected_reason"),
    [
        pytest.param(
            {"status": "clarification", "error_code": "location_required"},
            "local-system",
            "weather-location-clarification-v1",
            "weather_clarification",
            id="clarification",
        ),
        pytest.param(
            {"status": "failed", "error_code": "timeout", "query": "杭州"},
            "openmeteo",
            "weather-service-v1",
            "weather_service_failure",
            id="failure",
        ),
    ],
)
def test_clean_data_audit_has_stable_weather_non_success_reasons(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    weather_tool: dict[str, object],
    provider: str,
    model: str,
    expected_reason: str,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid=f"weather-{expected_reason}",
    )
    _configure_weather_audit_session(
        conn,
        session_id=session_id,
        topic_key="weather",
        weather_tool=weather_tool,
        llm_provider=provider,
        llm_model=model,
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert result.reasons == [expected_reason]


def test_clean_data_audit_requires_only_actual_system_failure_call_flow(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="planned-system-failure-positive-evidence",
        error_type_id="system_failure",
    )

    bypass_rows = conn.execute(
        """
        SELECT route
        FROM api_call_logs
        WHERE session_id = ? AND turn_index = 2
        ORDER BY route
        """,
        (session_id,),
    ).fetchall()
    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert [row["route"] for row in bypass_rows] == ["asr"]
    assert result.status == "eligible"
    assert result.reasons == []


def test_clean_export_excludes_review_needed_missing_external_evidence(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    from backend.app.services.export import build_clean_data_export_payload

    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id)
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="legacy-clean-export-exclusion",
        external_evidence=False,
    )
    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )
    persist_clean_data_audit(conn, participant_id=participant_id, result=result)

    payload = build_clean_data_export_payload(conn)

    assert result.status == "review_needed"
    assert payload["participants.csv"] == []


def test_clean_data_audit_excludes_missing_audio_path(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-missing-audio",
        missing_audio_turn_indexes={3},
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "audio_missing" in result.reasons


def test_clean_data_audit_excludes_completed_formal_session_with_text_test_turn(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-text-test-only-turn",
        non_voice_turn_indexes={2},
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "non_voice_formal_turn" in result.reasons


def test_clean_data_audit_excludes_risk_flagged_participant(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-risk-flag",
    )
    conn.execute(
        """
        INSERT INTO session_risk_flags (session_id, flag, detail_json)
        VALUES (?, 'api_failure', ?)
        """,
        (session_id, json.dumps({"turn_index": 2}, ensure_ascii=False, sort_keys=True)),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "external_api_failure" in result.reasons


def test_clean_data_audit_excludes_missing_rating(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-missing-rating",
        missing_rating_turn_indexes={5},
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "missing_rating" in result.reasons


def test_clean_data_audit_excludes_long_participant_with_incomplete_days(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="long")
    _insert_pretest_response(conn, participant_id=participant_id, day_index=1, status="final")
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-long-day-1",
    )
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=2,
        session_uuid="session-long-day-2",
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "excluded"
    assert "incomplete_formal_days" in result.reasons


def test_clean_data_audit_excludes_missing_audio_file(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-file-missing",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 3",
        (session_id,),
    ).fetchone()
    (sqlite_settings.data_dir / str(row["user_audio_path"])).unlink()

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_missing" in result.reasons


def test_clean_data_audit_excludes_audio_path_outside_configured_root(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-path-invalid",
    )
    outside_path = tmp_path.parent / "outside-audio.wav"
    outside_path.write_bytes(b"outside audio")
    conn.execute(
        "UPDATE conversation_turns SET user_audio_path = ? WHERE session_id = ? AND turn_index = 2",
        (str(outside_path), session_id),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_requires_audio_to_be_a_regular_file(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-not-regular",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 2",
        (session_id,),
    ).fetchone()
    audio_path = sqlite_settings.data_dir / str(row["user_audio_path"])
    audio_path.unlink()
    audio_path.mkdir()

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_rejects_audio_symlinks_within_configured_root(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-symlink",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 2",
        (session_id,),
    ).fetchone()
    audio_path = sqlite_settings.data_dir / str(row["user_audio_path"])
    target_path = audio_path.with_name("symlink-target.wav")
    audio_path.rename(target_path)
    audio_path.symlink_to(target_path.name)

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_rejects_symlinked_parent_directory(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-parent-symlink",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 2",
        (session_id,),
    ).fetchone()
    original_path = sqlite_settings.data_dir / str(row["user_audio_path"])
    real_parent = sqlite_settings.data_dir / "real-audio-parent"
    real_parent.mkdir()
    moved_path = real_parent / original_path.name
    original_path.rename(moved_path)
    linked_parent = sqlite_settings.data_dir / "linked-audio-parent"
    linked_parent.symlink_to(real_parent.name, target_is_directory=True)
    linked_relative_path = f"{linked_parent.name}/{moved_path.name}"
    conn.execute(
        "UPDATE conversation_turns SET user_audio_path = ? WHERE session_id = ? AND turn_index = 2",
        (linked_relative_path, session_id),
    )
    conn.execute(
        "UPDATE asr_attempts SET user_audio_path = ? WHERE session_id = ? AND turn_index = 2",
        (linked_relative_path, session_id),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_maps_embedded_nul_to_invalid_path(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-nul",
    )
    conn.execute(
        "UPDATE conversation_turns SET user_audio_path = ? WHERE session_id = ? AND turn_index = 2",
        ("audio/bad\0path.wav", session_id),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_rejects_leaf_swapped_to_symlink_before_open(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-leaf-swap",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 2",
        (session_id,),
    ).fetchone()
    audio_path = sqlite_settings.data_dir / str(row["user_audio_path"])
    outside_path = tmp_path.parent / "outside-leaf-swap.wav"
    outside_path.write_bytes(audio_path.read_bytes())
    original_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is not None and str(path) == audio_path.name:
            audio_path.unlink()
            audio_path.symlink_to(outside_path)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert swapped is True
    assert "audio_path_invalid" in result.reasons


def test_clean_data_audit_maps_disappearance_before_open_to_audio_missing(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-disappears",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 3",
        (session_id,),
    ).fetchone()
    audio_path = sqlite_settings.data_dir / str(row["user_audio_path"])
    original_open = os.open
    removed = False

    def disappearing_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal removed
        if not removed and dir_fd is not None and str(path) == audio_path.name:
            audio_path.unlink()
            removed = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", disappearing_open)

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert removed is True
    assert "audio_missing" in result.reasons


def test_clean_data_audit_enforces_maximum_while_file_grows_during_read(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constrained_settings = sqlite_settings.model_copy(
        update={"asr_max_upload_bytes": 128}
    )
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=constrained_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-grows",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 4",
        (session_id,),
    ).fetchone()
    audio_path = constrained_settings.data_dir / str(row["user_audio_path"])
    original_read = os.read
    grew = False

    def growing_read(file_descriptor: int, byte_count: int) -> bytes:
        nonlocal grew
        if not grew:
            with audio_path.open("ab") as audio_file:
                audio_file.write(b"x" * 256)
            grew = True
        return original_read(file_descriptor, byte_count)

    monkeypatch.setattr(os, "read", growing_read)

    result = audit_participant_clean_data(
        conn,
        settings=constrained_settings,
        participant_id=participant_id,
    )

    assert grew is True
    assert "audio_size_invalid" in result.reasons


def test_audio_leaf_swap_to_fifo_completes_without_blocking(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("FIFO and O_NONBLOCK support are required for this regression.")

    audio_bytes = b"fifo swap evidence"
    relative_path = "audio/fifo-swap.wav"
    audio_path = sqlite_settings.data_dir / relative_path
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(audio_bytes)
    original_open = os.open
    swapped = False

    def fifo_swapping_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if not swapped and dir_fd is not None and str(path) == audio_path.name:
            audio_path.unlink()
            os.mkfifo(audio_path)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fifo_swapping_open)
    results = []

    def read_evidence() -> None:
        results.append(
            read_audio_evidence(
                settings=sqlite_settings,
                audio_path_value=relative_path,
                stored_sha256=hashlib.sha256(audio_bytes).hexdigest(),
            )
        )

    reader_thread = threading.Thread(target=read_evidence, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout=0.25)
    completed_without_writer = not reader_thread.is_alive()
    if reader_thread.is_alive():
        writer_descriptor = original_open(
            audio_path,
            os.O_WRONLY | os.O_NONBLOCK,
        )
        os.close(writer_descriptor)
        reader_thread.join(timeout=1)

    assert swapped is True
    assert completed_without_writer is True
    assert len(results) == 1
    assert results[0].reason == "audio_path_invalid"


def test_clean_data_audit_excludes_changed_audio_bytes(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-audio-hash-mismatch",
    )
    row = conn.execute(
        "SELECT user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 4",
        (session_id,),
    ).fetchone()
    (sqlite_settings.data_dir / str(row["user_audio_path"])).write_bytes(b"changed")

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "audio_hash_mismatch" in result.reasons


@pytest.mark.parametrize("audio_bytes", [b"", b"oversized"])
def test_clean_data_audit_excludes_invalid_audio_size(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    audio_bytes: bytes,
) -> None:
    constrained_settings = sqlite_settings.model_copy(
        update={"asr_max_upload_bytes": 4}
    )
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=constrained_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid=f"session-audio-size-{len(audio_bytes)}",
    )
    row = conn.execute(
        "SELECT id, user_audio_path FROM conversation_turns WHERE session_id = ? AND turn_index = 1",
        (session_id,),
    ).fetchone()
    audio_path = constrained_settings.data_dir / str(row["user_audio_path"])
    audio_path.write_bytes(audio_bytes)
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    conn.execute(
        "UPDATE conversation_turns SET user_audio_sha256 = ? WHERE id = ?",
        (audio_sha256, int(row["id"])),
    )
    conn.execute(
        "UPDATE asr_attempts SET user_audio_sha256 = ? WHERE session_id = ? AND turn_index = 1",
        (audio_sha256, session_id),
    )

    result = audit_participant_clean_data(
        conn,
        settings=constrained_settings,
        participant_id=participant_id,
    )

    assert "audio_size_invalid" in result.reasons


def test_clean_data_audit_requires_matching_successful_asr_database_evidence(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-asr-evidence-missing",
    )
    conn.execute(
        "DELETE FROM asr_attempts WHERE session_id = ? AND turn_index = 5",
        (session_id,),
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "asr_evidence_missing" in result.reasons


@pytest.mark.parametrize("route", ["chat", "evaluator", "asr"])
def test_clean_data_audit_inspects_every_formal_external_attempt(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    route: str,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    session_id = _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid=f"session-{route}-timeout-success",
    )
    for status in ("timeout", "success"):
        conn.execute(
            """
            INSERT INTO api_call_logs (
                request_id,
                session_id,
                turn_index,
                is_test,
                route,
                provider,
                model,
                status,
                cooldown_applied
            ) VALUES (?, ?, 2, 0, ?, 'provider', 'model', ?, 0)
            """,
            (f"session-{route}-timeout-success-turn-2", session_id, route, status),
        )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert "external_api_failure" in result.reasons


def test_clean_data_audit_does_not_treat_planned_system_failure_as_external_failure(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn)
    _insert_pretest_response(conn, participant_id=participant_id)
    _insert_formal_day_with_session(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
        day_index=1,
        session_uuid="session-planned-system-failure",
        error_type_id="system_failure",
    )

    result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )

    assert result.status == "eligible"
    assert "external_api_failure" not in result.reasons


def test_persist_clean_data_audit_upserts_current_attempt_result_only(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    eligible_result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=participant_id,
    )
    persist_clean_data_audit(conn, participant_id=participant_id, result=eligible_result)
    updated_result = type(eligible_result)(status="excluded", reasons=["participant_not_found"])
    persist_clean_data_audit(conn, participant_id=participant_id, result=updated_result)

    row = conn.execute(
        """
        SELECT status, reasons_json
        FROM clean_data_audits
        WHERE participant_id = ?
        """,
        (participant_id,),
    ).fetchone()

    assert row["status"] == "excluded"
    assert json.loads(row["reasons_json"]) == ["participant_not_found"]


def test_persist_clean_data_audit_keeps_separate_rows_per_attempt(
    conn: sqlite3.Connection,
) -> None:
    participant_id = _insert_participant(conn, participant_type="long")
    first_attempt_id = int(
        conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
    )
    persist_clean_data_audit(
        conn,
        participant_id=participant_id,
        result=CleanDataAuditResult(status="excluded", reasons=["incomplete_formal_days"]),
    )

    second_attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type="short",
        condition="human",
        subcondition="qa",
        topic_key="topic-qa",
        error_type_id="factual_minor",
        target_days=1,
        status="completed",
        valid_for_export=True,
        source_attempt_id=first_attempt_id,
        export_role="converted_short",
    )
    conn.execute(
        "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
        (second_attempt_id, participant_id),
    )
    persist_clean_data_audit(
        conn,
        participant_id=participant_id,
        result=CleanDataAuditResult(status="eligible", reasons=[]),
    )

    rows = conn.execute(
        """
        SELECT attempt_id, status, reasons_json
        FROM clean_data_audits
        WHERE participant_id = ?
        ORDER BY attempt_id
        """,
        (participant_id,),
    ).fetchall()

    assert [row["attempt_id"] for row in rows] == [first_attempt_id, second_attempt_id]
    assert rows[0]["status"] == "excluded"
    assert json.loads(rows[0]["reasons_json"]) == ["incomplete_formal_days"]
    assert rows[1]["status"] == "eligible"
    assert json.loads(rows[1]["reasons_json"]) == []


def test_persist_clean_data_audit_tracks_current_attempt_id_and_admin_listing_shape(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id = _insert_participant(conn, participant_type="short")
    current_attempt_id = int(
        conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
    )
    persist_clean_data_audit(
        conn,
        participant_id=participant_id,
        result=CleanDataAuditResult(status="eligible", reasons=[]),
    )

    audit_row = conn.execute(
        """
        SELECT attempt_id
        FROM clean_data_audits
        WHERE participant_id = ?
        """,
        (participant_id,),
    ).fetchone()
    repository = AdminRepository(conn, settings=sqlite_settings)
    listing = repository.list_clean_data_audits(status="eligible")

    assert audit_row["attempt_id"] == current_attempt_id
    assert listing["items"][0]["attempt_id"] == current_attempt_id


def test_list_clean_data_audits_parses_reasons_and_filters_status(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    eligible_participant_id = _insert_participant(conn, participant_type="short")
    excluded_participant_id = _insert_participant(
        conn,
        participant_type="long",
        created_at="2026-07-02T09:30:00+08:00",
    )

    eligible_result = CleanDataAuditResult(status="eligible", reasons=[])
    excluded_result = audit_participant_clean_data(
        conn,
        settings=sqlite_settings,
        participant_id=excluded_participant_id,
    )
    persist_clean_data_audit(
        conn,
        participant_id=eligible_participant_id,
        result=eligible_result,
    )
    persist_clean_data_audit(
        conn,
        participant_id=excluded_participant_id,
        result=excluded_result,
    )

    repository = AdminRepository(conn, settings=sqlite_settings)
    all_rows = repository.list_clean_data_audits()
    eligible_rows = repository.list_clean_data_audits(status="eligible")
    excluded_rows = repository.list_clean_data_audits(status="excluded")

    assert all_rows["count"] == 2
    assert len(all_rows["items"]) == 2
    assert isinstance(all_rows["items"][0]["reasons"], list)
    assert "incomplete_formal_days" in all_rows["items"][0]["reasons"]
    assert all_rows["items"][0]["participant_type"] == "long"
    assert all_rows["items"][1]["reasons"] == []
    assert all_rows["items"][1]["name"] == "Participant-short"
    assert eligible_rows["count"] == 1
    assert eligible_rows["items"][0]["status"] == "eligible"
    assert excluded_rows["count"] == 1
    assert excluded_rows["items"][0]["status"] == "excluded"


def test_recompute_clean_data_audits_skips_internal_test_participants(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    internal_participant_id = _insert_participant(
        conn,
        participant_type="short",
        name="Internal Test Participant",
        phone="13800138001",
    )
    conn.execute(
        "UPDATE participants SET phone_hash = ?, phone = ? WHERE id = ?",
        (
            TEST_CHANNEL_PHONE_HASH,
            "00000000000",
            internal_participant_id,
        ),
    )
    normal_participant_id = _insert_participant(
        conn,
        participant_type="short",
        created_at="2026-07-02T10:00:00+08:00",
        name="Normal Clean Participant",
        phone="13800138002",
    )

    repository = AdminRepository(conn, settings=sqlite_settings)
    result = repository.recompute_clean_data_audits(admin_user="admin")

    assert result["summary"]["scanned"] == 1
    assert result["summary"]["persisted"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["participant_id"] == normal_participant_id

    audit_rows = conn.execute(
        "SELECT participant_id FROM clean_data_audits ORDER BY participant_id"
    ).fetchall()
    assert [row["participant_id"] for row in audit_rows] == [normal_participant_id]


def test_recompute_clean_data_audits_releases_write_lock_during_evidence_scan(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from backend.app.repositories import admin as admin_repository

    setup_conn = get_connection(sqlite_settings)
    try:
        run_migrations(setup_conn)
        participant_id = _insert_participant(setup_conn, participant_type="short")
    finally:
        setup_conn.close()

    scan_started = Event()
    release_scan = Event()

    def _blocked_audit(conn, *, settings, participant_id):
        scan_started.set()
        assert release_scan.wait(timeout=5)
        return CleanDataAuditResult(
            status="review_needed",
            reasons=["external_api_evidence_missing"],
        )

    monkeypatch.setattr(
        admin_repository,
        "audit_participant_clean_data",
        _blocked_audit,
    )

    def recompute():
        conn = get_connection(sqlite_settings)
        try:
            return AdminRepository(conn, settings=sqlite_settings).recompute_clean_data_audits(
                admin_user="admin"
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(recompute)
        assert scan_started.wait(timeout=5)
        writer = get_connection(sqlite_settings)
        try:
            writer.execute("PRAGMA busy_timeout = 0")
            writer.execute(
                "UPDATE participants SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (participant_id,),
            )
        finally:
            writer.close()
            release_scan.set()
        result = future.result(timeout=5)

    assert result["summary"]["scanned"] == 1
    assert result["summary"]["persisted"] == 1
