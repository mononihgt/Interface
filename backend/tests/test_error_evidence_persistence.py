from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.db import get_connection, run_migrations
from backend.app.repositories.sessions import insert_session
from backend.app.repositories.turns import insert_turn
from backend.app.settings import Settings
from backend.app.services.export import CSV_FIELDNAMES


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'error-evidence.db'}",
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()
    return settings


def _seed_session(conn, *, session_uuid: str) -> int:
    participant_id = conn.execute(
        """
        INSERT INTO participants (
            name, phone, phone_hash, participant_type, condition, subcondition,
            topic_key, error_type_id, target_days, current_status
        ) VALUES ('测试', '13800000001', ?, 'short', 'human', 'qa', 'advice',
                  'factual_minor', 1, 'active')
        """,
        (f"hash-{session_uuid}",),
    ).lastrowid
    day_id = conn.execute(
        """
        INSERT INTO participant_days (
            participant_id, day_index, calendar_date, status
        ) VALUES (?, 1, '2026-07-12', 'in_experiment')
        """,
        (participant_id,),
    ).lastrowid
    return insert_session(
        conn,
        participant_id=participant_id,
        participant_day_id=day_id,
        attempt_id=None,
        session_uuid=session_uuid,
        condition="human",
        subcondition="qa",
        topic_key="advice",
        scenario_id="human_qa_advice_v2",
        agent_graph_version="qa_graph_v2",
        error_type_id="factual_minor",
        planned_error_turn=2,
        status="started",
        started_at="2026-07-12T00:00:00Z",
        client_info_json="{}",
        is_test=True,
    )


def test_migration_014_adds_manipulation_and_turn_evidence_columns(sqlite_settings):
    conn = get_connection(sqlite_settings)
    try:
        session_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(experiment_sessions)")
        }
        turn_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(conversation_turns)")
        }
    finally:
        conn.close()

    assert "manipulation_status" in session_columns
    assert {
        "error_mutation_json",
        "error_semantic_attempt_count",
        "error_failure_reason",
        "error_attempts_json",
    } <= turn_columns


def test_new_session_defaults_to_pending_manipulation(sqlite_settings):
    conn = get_connection(sqlite_settings)
    try:
        session_id = _seed_session(conn, session_uuid="pending-manipulation-session")
        row = conn.execute(
            "SELECT manipulation_status FROM experiment_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row["manipulation_status"] == "pending"


def test_turn_repository_persists_sanitized_semantic_evidence(sqlite_settings):
    conn = get_connection(sqlite_settings)
    try:
        session_id = _seed_session(conn, session_uuid="turn-evidence-session")
        attempts = [
            {
                "attempt_no": 1,
                "failure_reason": None,
                "mutation_applied": True,
                "evaluator_status": "success",
                "evaluator_parse_attempts": 1,
                "structured_parse_attempts": 2,
                "provider": "provider-a",
                "model": "model-a",
                "route": "chat",
                "provider_status": "success",
                "route_attempt_count": 2,
            }
        ]
        mutation = {
            "errorTypeId": "factual_minor",
            "targetKind": "user_context_fact",
            "targetPath": "assistant_text",
            "originalValue": "original",
            "mutatedValue": "mutated",
            "applied": True,
        }
        turn_id = insert_turn(
            conn,
            session_id=session_id,
            turn_index=1,
            user_text="test",
            user_input_mode="text_test_only",
            user_audio_path=None,
            user_audio_sha256=None,
            asr_provider=None,
            asr_status="not_used",
            asr_text=None,
            asr_latency_ms=None,
            assistant_text="response",
            response_latency_ms=1,
            llm_provider="provider-a",
            llm_model="model-a",
            llm_route="chat",
            llm_attempts_json="[]",
            error_planned=True,
            error_type_id="factual_minor",
            error_presented=True,
            error_presentation="assistant_text",
            error_evaluator_provider="provider-a",
            error_evaluator_model="model-a",
            error_evaluator_result_json="{}",
            error_mutation_json=json.dumps(mutation),
            error_semantic_attempt_count=1,
            error_failure_reason=None,
            error_attempts_json=json.dumps(attempts),
            agent_state_json="{}",
        )
        row = conn.execute(
            "SELECT * FROM conversation_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()
    finally:
        conn.close()

    assert json.loads(row["error_mutation_json"])["targetKind"] == "user_context_fact"
    assert row["error_semantic_attempt_count"] == 1
    assert json.loads(row["error_attempts_json"]) == attempts


def test_raw_export_schema_includes_manipulation_and_semantic_evidence():
    assert "manipulation_status" in CSV_FIELDNAMES["sessions.csv"]
    assert {
        "error_mutation_json",
        "error_semantic_attempt_count",
        "error_failure_reason",
        "error_attempts_json",
    } <= set(CSV_FIELDNAMES["turns.csv"])
