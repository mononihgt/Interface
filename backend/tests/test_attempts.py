from pathlib import Path

import pytest

from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "attempts.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
    )


def test_create_attempt_sets_current_attempt(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import (
        create_attempt,
        get_current_attempt,
        set_current_attempt,
    )
    from backend.app.repositories.participants import insert_participant_identity

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Attempt User",
                phone="13800000001",
                phone_hash="hash-attempt-user",
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=1,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )

        attempt = get_current_attempt(conn, participant_id=participant_id)
    finally:
        conn.close()

    assert attempt is not None
    assert attempt["id"] == attempt_id
    assert attempt["attempt_no"] == 1
    assert attempt["participant_type"] == "short"
    assert attempt["status"] == "active"
    assert attempt["export_role"] == "normal_short"


def test_short_completed_relogin_does_not_create_new_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import list_attempts_for_participant
    from backend.app.repositories.participants import update_participant_day_status
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            first = login_participant(
                conn,
                name="Completed Short",
                phone="13800000012",
            )
            day_row = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first.attempt_id,),
            ).fetchone()
            update_participant_day_status(
                conn,
                participant_day_id=int(day_row["id"]),
                status="completed",
                completed_at="2026-07-02T10:00:00+08:00",
            )
            conn.execute(
                """
                UPDATE participant_attempts
                SET status = 'completed'
                WHERE id = ?
                """,
                (first.attempt_id,),
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Completed Short",
                phone="13800000012",
            )
        attempts = list_attempts_for_participant(
            conn,
            participant_id=second.participant_id,
        )
    finally:
        conn.close()

    assert second.attempt_id == first.attempt_id
    assert second.participation_state == "completed"
    assert len(attempts) == 1


def test_short_day_one_not_started_relogin_restores_current_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import list_attempts_for_participant
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            first = login_participant(
                conn,
                name="Retry Short Not Started",
                phone="13800000020",
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Retry Short Not Started",
                phone="13800000020",
                data_dir=sqlite_settings.data_dir,
            )

        attempts = list_attempts_for_participant(
            conn,
            participant_id=second.participant_id,
        )
        old_attempt_row = conn.execute(
            """
            SELECT status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE id = ?
            """,
            (first.attempt_id,),
        ).fetchone()
    finally:
        conn.close()

    assert second.attempt_id == first.attempt_id
    assert [row["status"] for row in attempts] == ["active"]
    assert old_attempt_row is not None
    assert old_attempt_row["valid_for_export"] == 1
    assert old_attempt_row["blocked_reason"] is None


def test_incomplete_short_relogin_restores_attempt_pretest_session_and_audio(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import list_attempts_for_participant
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            first = login_participant(
                conn,
                name="Retry Short",
                phone="13800000021",
            )
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
                    submitted_at
                ) VALUES (?, ?, 1, 'final', '{"ok": true}', 0, '2026-07-02T09:00:00+08:00', '2026-07-02T09:00:00+08:00')
                """,
                (first.participant_id, first.attempt_id),
            )
            day_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first.attempt_id,),
            ).fetchone()["id"]
            session_id = conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'retry-session', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (first.participant_id, first.attempt_id, day_id),
            ).lastrowid
            audio_path = (
                sqlite_settings.data_dir / "audio" / "retry-session-turn-1.webm"
            )
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"old audio")
            conn.execute(
                """
                INSERT INTO conversation_turns (
                    session_id,
                    turn_index,
                    user_text,
                    user_input_mode,
                    user_audio_path,
                    user_audio_sha256,
                    asr_status,
                    assistant_text,
                    response_latency_ms,
                    llm_attempts_json,
                    error_planned,
                    error_presented,
                    error_presentation,
                    agent_state_json
                ) VALUES (?, 1, 'hello', 'voice', ?, 'sha-old', 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
                """,
                (session_id, str(audio_path.relative_to(sqlite_settings.data_dir))),
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Retry Short",
                phone="13800000021",
                data_dir=sqlite_settings.data_dir,
            )

        attempts = list_attempts_for_participant(
            conn,
            participant_id=second.participant_id,
        )
        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (first.attempt_id,),
        ).fetchone()[0]
        preserved_pretest = conn.execute(
            """
            SELECT payload_json, source_pretest_response_id
            FROM pretest_responses
            WHERE attempt_id = ? AND status = 'final'
            """,
            (second.attempt_id,),
        ).fetchone()
        old_attempt_row = conn.execute(
            """
            SELECT status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE id = ?
            """,
            (first.attempt_id,),
        ).fetchone()
    finally:
        conn.close()

    assert second.attempt_id == first.attempt_id
    assert [row["status"] for row in attempts] == ["active"]
    assert old_attempt_row is not None
    assert old_attempt_row["valid_for_export"] == 1
    assert old_attempt_row["blocked_reason"] is None
    assert old_session_count == 1
    assert preserved_pretest is not None
    assert preserved_pretest["payload_json"] == '{"ok": true}'
    assert preserved_pretest["source_pretest_response_id"] is None
    assert audio_path.exists()


def test_incomplete_short_relogin_preserves_managed_and_outside_audio_paths(
    tmp_path: Path,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import list_attempts_for_participant
    from backend.app.services.participants import login_participant

    data_dir = tmp_path / "runtime-data"
    db_dir = tmp_path / "db-root"
    outside_audio_path = tmp_path / "outside-audio" / "unsafe-turn.webm"
    settings = Settings(
        app_env="test",
        data_dir=data_dir,
        database_url=f"sqlite:///{db_dir / 'attempts.db'}",
        app_secret_key="test-secret-key",
    )

    conn = get_connection(settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            first = login_participant(
                conn,
                name="Retry Short Cross Root",
                phone="13800000032",
                data_dir=settings.data_dir,
            )
            day_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first.attempt_id,),
            ).fetchone()["id"]
            session_id = conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'retry-cross-root-session', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (first.participant_id, first.attempt_id, day_id),
            ).lastrowid
            managed_audio_path = settings.data_dir / "audio" / "retry-cross-root-session-turn-1.webm"
            managed_audio_path.parent.mkdir(parents=True, exist_ok=True)
            managed_audio_path.write_bytes(b"managed audio")
            outside_audio_path.parent.mkdir(parents=True, exist_ok=True)
            outside_audio_path.write_bytes(b"outside audio")
            conn.executemany(
                """
                INSERT INTO conversation_turns (
                    session_id,
                    turn_index,
                    user_text,
                    user_input_mode,
                    user_audio_path,
                    user_audio_sha256,
                    asr_status,
                    assistant_text,
                    response_latency_ms,
                    llm_attempts_json,
                    error_planned,
                    error_presented,
                    error_presentation,
                    agent_state_json
                ) VALUES (?, ?, ?, 'voice', ?, ?, 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
                """,
                [
                    (
                        session_id,
                        1,
                        "hello",
                        str(managed_audio_path.relative_to(settings.data_dir)),
                        "sha-managed",
                    ),
                    (
                        session_id,
                        2,
                        "unsafe",
                        str(outside_audio_path.resolve()),
                        "sha-outside",
                    ),
                ],
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Retry Short Cross Root",
                phone="13800000032",
                data_dir=settings.data_dir,
            )

        attempts = list_attempts_for_participant(
            conn,
            participant_id=second.participant_id,
        )
        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (first.attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert second.attempt_id == first.attempt_id
    assert old_session_count == 1
    assert len(attempts) == 1
    assert managed_audio_path.exists()
    assert outside_audio_path.exists()


def test_incomplete_short_relogin_preserves_asr_only_audio_without_submitted_turn(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            first = login_participant(
                conn,
                name="Retry Short ASR Only",
                phone="13800000033",
                data_dir=sqlite_settings.data_dir,
            )
            day_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first.attempt_id,),
            ).fetchone()["id"]
            session_id = conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'retry-asr-only-session', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (first.participant_id, first.attempt_id, day_id),
            ).lastrowid
            audio_path = sqlite_settings.data_dir / "audio" / "retry-asr-only-session-turn-1.webm"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"asr only audio")
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
                ) VALUES (?, 1, 1, ?, 'sha-asr-only', 'mock', 'success', 'hello', 12)
                """,
                (session_id, str(audio_path.relative_to(sqlite_settings.data_dir))),
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Retry Short ASR Only",
                phone="13800000033",
                data_dir=sqlite_settings.data_dir,
            )

        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (first.attempt_id,),
        ).fetchone()[0]
        old_asr_attempt_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.attempt_id = ?
            """,
            (first.attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert second.attempt_id == first.attempt_id
    assert old_session_count == 1
    assert old_asr_attempt_count == 1
    assert audio_path.exists()


def test_incomplete_relogin_requires_explicit_data_dir_before_cleanup(
    tmp_path: Path,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import get_current_attempt
    from backend.app.repositories.participants import get_participant_by_id
    from backend.app.services.attempts import abandon_incomplete_attempt_and_reassign
    from backend.app.services.participants import login_participant

    data_dir = tmp_path / "runtime-data"
    db_dir = tmp_path / "db-root"
    settings = Settings(
        app_env="test",
        data_dir=data_dir,
        database_url=f"sqlite:///{db_dir / 'attempts.db'}",
        app_secret_key="test-secret-key",
    )

    conn = get_connection(settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant = login_participant(
                conn,
                name="Missing Data Dir",
                phone="13800000044",
                data_dir=settings.data_dir,
            )
            day_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (participant.attempt_id,),
            ).fetchone()["id"]
            session_id = conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'missing-data-dir-session', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (participant.participant_id, participant.attempt_id, day_id),
            ).lastrowid
            managed_audio_path = settings.data_dir / "audio" / "missing-data-dir-session-turn-1.webm"
            managed_audio_path.parent.mkdir(parents=True, exist_ok=True)
            managed_audio_path.write_bytes(b"managed audio")
            conn.execute(
                """
                INSERT INTO conversation_turns (
                    session_id,
                    turn_index,
                    user_text,
                    user_input_mode,
                    user_audio_path,
                    user_audio_sha256,
                    asr_status,
                    assistant_text,
                    response_latency_ms,
                    llm_attempts_json,
                    error_planned,
                    error_presented,
                    error_presentation,
                    agent_state_json
                ) VALUES (?, 1, 'hello', 'voice', ?, 'sha-managed', 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
                """,
                (session_id, str(managed_audio_path.relative_to(settings.data_dir))),
            )

        participant_row = get_participant_by_id(conn, participant_id=participant.participant_id)
        attempt_row = get_current_attempt(conn, participant_id=participant.participant_id)
        assert participant_row is not None
        assert attempt_row is not None

        with transaction(conn):
            with pytest.raises(ValueError, match="data_dir"):
                abandon_incomplete_attempt_and_reassign(
                    conn,
                    participant_row=participant_row,
                    attempt_row=attempt_row,
                )

        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (participant.attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert old_session_count == 1
    assert managed_audio_path.exists()


def test_delete_incomplete_sessions_requires_durable_audio_intent(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.attempts import delete_incomplete_formal_sessions_for_attempt
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant = login_participant(
                conn,
                name="Rollback Audio Cleanup",
                phone="13800000045",
                data_dir=sqlite_settings.data_dir,
            )
            day_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (participant.attempt_id,),
            ).fetchone()["id"]
            session_id = conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'rollback-audio-session', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (participant.participant_id, participant.attempt_id, day_id),
            ).lastrowid
            audio_path = sqlite_settings.data_dir / "audio" / "rollback-audio-session-turn-1.webm"
            audio_path.parent.mkdir(parents=True, exist_ok=True)
            audio_path.write_bytes(b"rollback audio")
            conn.execute(
                """
                INSERT INTO conversation_turns (
                    session_id,
                    turn_index,
                    user_text,
                    user_input_mode,
                    user_audio_path,
                    user_audio_sha256,
                    asr_status,
                    assistant_text,
                    response_latency_ms,
                    llm_attempts_json,
                    error_planned,
                    error_presented,
                    error_presentation,
                    agent_state_json
                ) VALUES (?, 1, 'hello', 'voice', ?, 'sha-rollback', 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
                """,
                (session_id, str(audio_path.relative_to(sqlite_settings.data_dir))),
            )

        with pytest.raises(RuntimeError, match="durable cleanup operations"):
            with transaction(conn):
                delete_incomplete_formal_sessions_for_attempt(
                    conn,
                    attempt_id=participant.attempt_id,
                    data_dir=sqlite_settings.data_dir,
                )

        session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (participant.attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert session_count == 1
    assert audio_path.exists()


def test_long_day_one_incomplete_relogin_restores_current_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import (
        create_attempt,
        get_current_attempt,
        list_attempts_for_participant,
        set_current_attempt,
    )
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Long Retry Day One",
                phone="13800000031",
                phone_hash="hash-long-retry-day-one",
            )
            first_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=first_attempt_id,
            )
            for day_index, calendar_date in ((1, "2026-07-02"), (2, "2026-07-03"), (3, "2026-07-04")):
                conn.execute(
                    """
                    INSERT INTO participant_days (
                        participant_id,
                        day_index,
                        calendar_date,
                        status,
                        attempt_id
                    ) VALUES (?, ?, ?, 'not_started', ?)
                    """,
                    (participant_id, day_index, calendar_date, first_attempt_id),
                )
            day_one_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first_attempt_id,),
            ).fetchone()["id"]
            conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'long-day-one-retry', 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-02T10:00:00+08:00', '{}', 0)
                """,
                (participant_id, first_attempt_id, day_one_id),
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Long Retry Day One",
                phone="13800000031",
                data_dir=sqlite_settings.data_dir,
            )

        attempts = list_attempts_for_participant(conn, participant_id=participant_id)
        old_attempt = conn.execute(
            """
            SELECT status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE id = ?
            """,
            (first_attempt_id,),
        ).fetchone()
        current_attempt = get_current_attempt(conn, participant_id=participant_id)
    finally:
        conn.close()

    assert second.attempt_id == first_attempt_id
    assert [row["status"] for row in attempts] == ["active"]
    assert old_attempt is not None
    assert old_attempt["status"] == "active"
    assert old_attempt["valid_for_export"] == 1
    assert old_attempt["blocked_reason"] is None
    assert current_attempt is not None
    assert current_attempt["id"] == second.attempt_id
    assert current_attempt["attempt_no"] == 1


def test_long_day_one_not_started_relogin_restores_current_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import (
        create_attempt,
        get_current_attempt,
        list_attempts_for_participant,
        set_current_attempt,
    )
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Long Retry Not Started",
                phone="13800000033",
                phone_hash="hash-long-retry-not-started",
            )
            first_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="human",
                subcondition="qa",
                topic_key="advice",
                error_type_id="factual_minor",
                target_days=3,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=first_attempt_id,
            )
            for day_index, calendar_date in ((1, "2026-07-02"), (2, "2026-07-03"), (3, "2026-07-04")):
                conn.execute(
                    """
                    INSERT INTO participant_days (
                        participant_id,
                        day_index,
                        calendar_date,
                        status,
                        attempt_id
                    ) VALUES (?, ?, ?, 'not_started', ?)
                    """,
                    (participant_id, day_index, calendar_date, first_attempt_id),
                )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Long Retry Not Started",
                phone="13800000033",
                data_dir=sqlite_settings.data_dir,
            )

        attempts = list_attempts_for_participant(conn, participant_id=participant_id)
        old_attempt = conn.execute(
            """
            SELECT status, valid_for_export, blocked_reason
            FROM participant_attempts
            WHERE id = ?
            """,
            (first_attempt_id,),
        ).fetchone()
        current_attempt = get_current_attempt(conn, participant_id=participant_id)
    finally:
        conn.close()

    assert second.attempt_id == first_attempt_id
    assert [row["status"] for row in attempts] == ["active"]
    assert old_attempt is not None
    assert old_attempt["status"] == "active"
    assert old_attempt["valid_for_export"] == 1
    assert old_attempt["blocked_reason"] is None
    assert current_attempt is not None
    assert current_attempt["id"] == second.attempt_id
    assert current_attempt["attempt_no"] == 1


def test_long_completed_day_one_relogin_keeps_existing_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import (
        create_attempt,
        list_attempts_for_participant,
        set_current_attempt,
    )
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.repositories.participants import update_participant_day_status
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Long Completed Day One",
                phone="13800000032",
                phone_hash="hash-long-completed-day-one",
            )
            first_attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="long",
                condition="tool",
                subcondition="planning",
                topic_key="goalPlan",
                error_type_id="logic_minor",
                target_days=3,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=first_attempt_id,
            )
            for day_index, calendar_date in ((1, "2026-07-02"), (2, "2026-07-03"), (3, "2026-07-04")):
                conn.execute(
                    """
                    INSERT INTO participant_days (
                        participant_id,
                        day_index,
                        calendar_date,
                        status,
                        attempt_id
                    ) VALUES (?, ?, ?, 'not_started', ?)
                    """,
                    (participant_id, day_index, calendar_date, first_attempt_id),
                )
            day_one_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first_attempt_id,),
            ).fetchone()["id"]
            update_participant_day_status(
                conn,
                participant_day_id=int(day_one_id),
                status="completed",
                completed_at="2026-07-02T10:00:00+08:00",
            )
            day_two_id = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 2
                """,
                (first_attempt_id,),
            ).fetchone()["id"]
            conn.execute(
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
                    client_info_json,
                    is_test
                ) VALUES (?, ?, ?, 'long-day-two-incomplete', 'tool', 'planning', 'goalPlan', 'scenario-2', 'graph-v1', 'logic_minor', 3, 'started', '2026-07-03T10:00:00+08:00', '{}', 0)
                """,
                (participant_id, first_attempt_id, day_two_id),
            )

        with transaction(conn):
            second = login_participant(
                conn,
                name="Long Completed Day One",
                phone="13800000032",
            )

        attempts = list_attempts_for_participant(conn, participant_id=participant_id)
        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (first_attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert second.attempt_id == first_attempt_id
    assert [row["status"] for row in attempts] == ["active"]
    assert old_session_count == 1
