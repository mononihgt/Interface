from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_connection, run_migrations
from backend.app.repositories.attempts import create_attempt
from backend.app.security import sign_session_payload
from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'client-response-timing.db'}",
        app_secret_key="client-response-timing-test-secret",
    )


@pytest.fixture
def client(sqlite_settings: Settings) -> TestClient:
    from backend.app.main import create_app

    return TestClient(create_app(settings=sqlite_settings))


def _seed_owned_turn(
    settings: Settings,
    client: TestClient,
    *,
    suffix: str,
) -> int:
    conn = get_connection(settings)
    try:
        run_migrations(conn)
        phone_hash = f"client-timing-hash-{suffix}"
        participant_cursor = conn.execute(
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
                current_status
            ) VALUES (?, ?, ?, 'short', 'human', 'qa', 'physics',
                      'factual_minor', 1, 'active')
            """,
            (f"Timing Participant {suffix}", f"test-phone-{suffix}", phone_hash),
        )
        participant_id = int(participant_cursor.lastrowid)
        attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="short",
            condition="human",
            subcondition="qa",
            topic_key="physics",
            error_type_id="factual_minor",
            target_days=1,
            status="active",
            valid_for_export=True,
        )
        conn.execute(
            "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
            (attempt_id, participant_id),
        )
        day_cursor = conn.execute(
            """
            INSERT INTO participant_days (
                participant_id,
                attempt_id,
                day_index,
                calendar_date,
                status,
                started_at
            ) VALUES (?, ?, 1, '2026-07-13', 'in_experiment',
                      '2026-07-13T10:00:00+08:00')
            """,
            (participant_id, attempt_id),
        )
        session_cursor = conn.execute(
            """
            INSERT INTO experiment_sessions (
                participant_id,
                participant_day_id,
                attempt_id,
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
                is_test
            ) VALUES (?, ?, ?, ?, 'human', 'qa', 'physics', 'scenario-physics',
                      'graph-v1', 'factual_minor', 2, 'started',
                      '2026-07-13T10:00:00+08:00', 0)
            """,
            (
                participant_id,
                int(day_cursor.lastrowid),
                attempt_id,
                f"client-timing-session-{suffix}",
            ),
        )
        turn_cursor = conn.execute(
            """
            INSERT INTO conversation_turns (
                session_id,
                turn_index,
                user_text,
                user_input_mode,
                asr_status,
                assistant_text,
                response_latency_ms,
                llm_attempts_json,
                error_planned,
                error_presented,
                error_presentation,
                agent_state_json
            ) VALUES (?, 1, '测试消息', 'text_test_only', 'not_used',
                      '测试回复', 800, '[]', 0, 0, 'none', '{}')
            """,
            (int(session_cursor.lastrowid),),
        )
    finally:
        conn.close()

    client.cookies.set(
        settings.session_cookie_name,
        sign_session_payload(
            {
                "participant_id": participant_id,
                "attempt_id": attempt_id,
                "phone_hash": phone_hash,
            },
            settings.app_secret_key,
        ),
    )
    return int(turn_cursor.lastrowid)


def _timing_payload(*, latency_ms: int = 4230) -> dict[str, object]:
    return {
        "client_message_sent_at": "2026-07-13T10:00:00.000+08:00",
        "assistant_render_completed_at": "2026-07-13T10:00:04.230+08:00",
        "client_response_latency_ms": latency_ms,
        "client_timing_interrupted": False,
    }


def test_client_timing_is_persisted_and_identical_retry_is_idempotent(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    with client:
        turn_id = _seed_owned_turn(sqlite_settings, client, suffix="owner")
        payload = _timing_payload()

        first = client.post(f"/api/turns/{turn_id}/client-timing", json=payload)
        repeated = client.post(f"/api/turns/{turn_id}/client-timing", json=payload)

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert first.json() == repeated.json()
    assert first.json()["turn_id"] == turn_id
    assert first.json()["client_response_latency_ms"] == 4230
    assert first.json()["client_timing_interrupted"] is False
    assert first.json()["render_timing_received_at"]

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT
                client_message_sent_at,
                assistant_render_completed_at,
                client_response_latency_ms,
                client_timing_interrupted,
                render_timing_received_at
            FROM conversation_turns
            WHERE id = ?
            """,
            (turn_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["client_message_sent_at"] == "2026-07-13T10:00:00+08:00"
    assert row["assistant_render_completed_at"] == "2026-07-13T10:00:04.230000+08:00"
    assert row["client_response_latency_ms"] == 4230
    assert row["client_timing_interrupted"] == 0
    assert row["render_timing_received_at"] == first.json()["render_timing_received_at"]


def test_client_timing_conflict_preserves_first_observation(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    with client:
        turn_id = _seed_owned_turn(sqlite_settings, client, suffix="conflict")
        first = client.post(
            f"/api/turns/{turn_id}/client-timing",
            json=_timing_payload(),
        )
        conflicting = client.post(
            f"/api/turns/{turn_id}/client-timing",
            json=_timing_payload(latency_ms=5000),
        )

    assert first.status_code == 200
    assert conflicting.status_code == 409

    conn = get_connection(sqlite_settings)
    try:
        stored_latency = conn.execute(
            "SELECT client_response_latency_ms FROM conversation_turns WHERE id = ?",
            (turn_id,),
        ).fetchone()["client_response_latency_ms"]
    finally:
        conn.close()
    assert stored_latency == 4230


def test_client_timing_rejects_foreign_participant(
    client: TestClient,
    sqlite_settings: Settings,
) -> None:
    with client:
        owner_turn_id = _seed_owned_turn(sqlite_settings, client, suffix="first")
        _seed_owned_turn(sqlite_settings, client, suffix="second")

        response = client.post(
            f"/api/turns/{owner_turn_id}/client-timing",
            json=_timing_payload(),
        )

    assert response.status_code == 401


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_response_latency_ms", -1),
        ("client_response_latency_ms", 3_600_001),
        ("client_message_sent_at", "not-a-timestamp"),
        ("assistant_render_completed_at", "2026-07-13"),
    ],
)
def test_client_timing_validates_payload(
    client: TestClient,
    sqlite_settings: Settings,
    field: str,
    value: object,
) -> None:
    with client:
        turn_id = _seed_owned_turn(
            sqlite_settings,
            client,
            suffix=f"invalid-{field}-{value}",
        )
        payload = _timing_payload()
        payload[field] = value

        response = client.post(
            f"/api/turns/{turn_id}/client-timing",
            json=payload,
        )

    assert response.status_code == 422
