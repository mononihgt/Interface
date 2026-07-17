from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import threading

import pytest

from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "cleanup.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="test-secret-key",
    )


def _insert_long_attempt(conn, *, participant_id: int, start_date: str) -> int:
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import create_participant_days

    attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type="long",
        condition="human",
        subcondition="qa",
        topic_key="advice",
        error_type_id="factual_minor",
        target_days=3,
    )
    set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
    create_participant_days(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        target_days=3,
        start_date=date.fromisoformat(start_date),
    )
    return attempt_id


def _participant_day_id(conn, *, attempt_id: int, day_index: int) -> int:
    row = conn.execute(
        """
        SELECT id
        FROM participant_days
        WHERE attempt_id = ? AND day_index = ?
        """,
        (attempt_id, day_index),
    ).fetchone()
    assert row is not None
    return int(row["id"])


def _insert_incomplete_session_with_audio(
    conn,
    *,
    sqlite_settings: Settings,
    participant_id: int,
    attempt_id: int,
    day_index: int,
    session_uuid: str,
) -> Path:
    day_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=day_index)
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
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-03T10:00:00+08:00', '{}', 0)
        """,
        (participant_id, attempt_id, day_id, session_uuid),
    ).lastrowid
    audio_path = sqlite_settings.data_dir / "audio" / f"{session_uuid}-turn-1.webm"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = b"incomplete audio"
    audio_path.write_bytes(audio_bytes)
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
        ) VALUES (?, 1, 'hello', 'voice', ?, ?, 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
        """,
        (
            session_id,
            str(audio_path.relative_to(sqlite_settings.data_dir)),
            hashlib.sha256(audio_bytes).hexdigest(),
        ),
    )
    return audio_path


def _insert_incomplete_session_with_asr_only_audio(
    conn,
    *,
    sqlite_settings: Settings,
    participant_id: int,
    attempt_id: int,
    day_index: int,
    session_uuid: str,
) -> Path:
    day_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=day_index)
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
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'started', '2026-07-03T10:00:00+08:00', '{}', 0)
        """,
        (participant_id, attempt_id, day_id, session_uuid),
    ).lastrowid
    audio_path = sqlite_settings.data_dir / "audio" / f"{session_uuid}-turn-1.webm"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = b"asr only audio"
    audio_path.write_bytes(audio_bytes)
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
        ) VALUES (?, 1, 1, ?, ?, 'mock', 'success', 'hello', 12)
        """,
        (
            session_id,
            str(audio_path.relative_to(sqlite_settings.data_dir)),
            hashlib.sha256(audio_bytes).hexdigest(),
        ),
    )
    return audio_path


def _insert_session_with_audio(
    conn,
    *,
    sqlite_settings: Settings,
    participant_id: int,
    attempt_id: int,
    day_index: int,
    session_uuid: str,
    session_status: str,
    audio_bytes: bytes,
) -> tuple[int, Path]:
    day_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=day_index)
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
            completed_at,
            client_info_json,
            is_test
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, ?, '2026-07-03T10:00:00+08:00', ?, '{}', 0)
        """,
        (
            participant_id,
            attempt_id,
            day_id,
            session_uuid,
            session_status,
            "2026-07-03T10:05:00+08:00" if session_status == "completed" else None,
        ),
    ).lastrowid
    audio_path = sqlite_settings.data_dir / "audio" / f"{session_uuid}-turn-1.webm"
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(audio_bytes)
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
        ) VALUES (?, 1, 'hello', 'voice', ?, ?, 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
        """,
        (
            session_id,
            str(audio_path.relative_to(sqlite_settings.data_dir)),
            hashlib.sha256(audio_bytes).hexdigest(),
        ),
    )
    return int(session_id), audio_path


def _insert_completed_day_one_session_with_long_audio(
    conn,
    *,
    sqlite_settings: Settings,
    participant_id: int,
    attempt_id: int,
    name: str,
    phone: str,
    session_uuid: str,
) -> tuple[int, Path, str]:
    from backend.app.services.file_naming import canonical_audio_relative_path

    day_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=1)
    source_relative_path = canonical_audio_relative_path(
        name=name,
        phone=phone,
        participant_type="long",
        day_index=1,
        turn_index=1,
        session_id=session_uuid,
        suffix=".webm",
    )
    audio_path = sqlite_settings.data_dir / source_relative_path
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = b"completed day one long audio"
    audio_path.write_bytes(audio_bytes)
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
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
            completed_at,
            client_info_json,
            is_test
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'advice', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'completed', '2026-07-02T10:00:00+08:00', '2026-07-02T10:05:00+08:00', '{}', 0)
        """,
        (participant_id, attempt_id, day_id, session_uuid),
    ).lastrowid
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
        ) VALUES (?, 1, 'hello', 'voice', ?, ?, 'success', 'reply', 10, '[]', 0, 0, 'none', '{}')
        """,
        (session_id, source_relative_path, audio_sha256),
    )
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
        ) VALUES (?, 1, 1, ?, ?, 'mock', 'success', 'hello', 12)
        """,
        (session_id, source_relative_path, audio_sha256),
    )
    return int(session_id), audio_path, source_relative_path


def _prepare_completed_audio_cleanup(conn, *, sqlite_settings: Settings, suffix: str):
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import plan_attempt_cleanup
    from backend.app.services.file_naming import canonical_audio_relative_path

    name = f"Recoverable Cleanup {suffix}"
    phone = f"1380000{int(suffix):04d}"
    participant_id = insert_participant_identity(
        conn,
        name=name,
        phone=phone,
        phone_hash=f"hash-recoverable-{suffix}",
    )
    attempt_id = _insert_long_attempt(
        conn,
        participant_id=participant_id,
        start_date="2026-07-02",
    )
    update_participant_day_status(
        conn,
        participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
        status="completed",
        completed_at="2026-07-02T20:00:00+08:00",
    )
    session_uuid = f"recoverable-{suffix}"
    session_id, source_path, source_relative_path = (
        _insert_completed_day_one_session_with_long_audio(
            conn,
            sqlite_settings=sqlite_settings,
            participant_id=participant_id,
            attempt_id=attempt_id,
            name=name,
            phone=phone,
            session_uuid=session_uuid,
        )
    )
    destination_relative_path = canonical_audio_relative_path(
        name=name,
        phone=phone,
        participant_type="short",
        day_index=1,
        turn_index=1,
        session_id=session_uuid,
        suffix=".webm",
    )
    plan = plan_attempt_cleanup(
        conn,
        today="2026-07-04",
        data_dir=sqlite_settings.data_dir,
    )
    return {
        "attempt_id": attempt_id,
        "session_id": session_id,
        "source_path": source_path,
        "source_relative_path": source_relative_path,
        "destination_relative_path": destination_relative_path,
        "plan": plan,
    }


def test_cleanup_missing_source_does_not_commit_a_renamed_path(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="101",
            )
        prepared["source_path"].unlink()

        summary = apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )

        attempt_status = conn.execute(
            "SELECT status FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["status"]
        committed_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_path"]
    finally:
        conn.close()

    assert summary.converted_attempts == 0
    assert prepared["source_relative_path"] in summary.failed_audio_paths
    assert attempt_status == "active"
    assert committed_path == prepared["source_relative_path"]
    assert not (sqlite_settings.data_dir / prepared["destination_relative_path"]).exists()


def test_cleanup_move_failure_keeps_database_and_source_recoverable(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="102",
            )

        monkeypatch.setattr(
            cleanup_attempts,
            "_stage_audio_file",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("stage failed")),
        )
        summary = cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        operation = conn.execute(
            "SELECT state, source_path, staging_path FROM cleanup_operations"
        ).fetchone()
        committed_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_path"]
    finally:
        conn.close()

    assert summary.converted_attempts == 0
    assert prepared["source_relative_path"] in summary.failed_audio_paths
    assert committed_path == prepared["source_relative_path"]
    assert prepared["source_path"].exists()
    assert operation["state"] == "planned"
    assert not (sqlite_settings.data_dir / operation["staging_path"]).exists()


def test_cleanup_database_failure_restores_staged_audio(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="103",
            )

        monkeypatch.setattr(
            cleanup_attempts,
            "_commit_converted_attempt",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db failed")),
        )
        with pytest.raises(RuntimeError, match="db failed"):
            cleanup_attempts.apply_attempt_cleanup(
                conn,
                plan=prepared["plan"],
                data_dir=sqlite_settings.data_dir,
            )
        operation = conn.execute(
            "SELECT state, staging_path FROM cleanup_operations"
        ).fetchone()
        attempt_status = conn.execute(
            "SELECT status FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["status"]
    finally:
        conn.close()

    assert attempt_status == "active"
    assert prepared["source_path"].read_bytes() == b"completed day one long audio"
    assert operation["state"] == "rolled_back"
    assert not (sqlite_settings.data_dir / operation["staging_path"]).exists()


def test_cleanup_finalize_failure_is_reconciled_from_durable_staging(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="104",
            )

        real_finalize = cleanup_attempts._finalize_audio_file
        monkeypatch.setattr(
            cleanup_attempts,
            "_finalize_audio_file",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("finalize failed")),
        )
        summary = cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        operation_before = conn.execute(
            "SELECT state, staging_path, destination_path FROM cleanup_operations"
        ).fetchone()
        staging_exists_before = (
            sqlite_settings.data_dir / operation_before["staging_path"]
        ).exists()
        committed_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_path"]

        monkeypatch.setattr(cleanup_attempts, "_finalize_audio_file", real_finalize)
        cleanup_attempts.reconcile_cleanup_operations(
            conn,
            data_dir=sqlite_settings.data_dir,
        )
        operation_after = conn.execute(
            "SELECT state FROM cleanup_operations"
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert prepared["destination_relative_path"] in summary.failed_audio_paths
    assert committed_path == prepared["destination_relative_path"]
    assert operation_before["state"] == "database_committed"
    assert staging_exists_before is True
    assert (sqlite_settings.data_dir / operation_before["staging_path"]).exists() is False
    assert operation_after["state"] == "completed"
    assert (sqlite_settings.data_dir / prepared["destination_relative_path"]).read_bytes() == (
        b"completed day one long audio"
    )


def test_cleanup_rejects_completed_audio_that_no_longer_matches_persisted_hash(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="105",
            )
        persisted_hash = conn.execute(
            "SELECT user_audio_sha256 FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_sha256"]
        prepared["source_path"].write_bytes(b"tampered audio")

        summary = apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        turn_row = conn.execute(
            """
            SELECT user_audio_path, user_audio_sha256
            FROM conversation_turns
            WHERE session_id = ?
            """,
            (prepared["session_id"],),
        ).fetchone()
        attempt_status = conn.execute(
            "SELECT status FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["status"]
    finally:
        conn.close()

    assert summary.converted_attempts == 0
    assert summary.skipped[-1]["reason"] == "audio_hash_mismatch"
    assert attempt_status == "active"
    assert turn_row["user_audio_path"] == prepared["source_relative_path"]
    assert turn_row["user_audio_sha256"] == persisted_hash


def test_cleanup_rejects_completed_audio_outside_audio_directory(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="106",
            )
            outside_relative_path = "exports/not-audio.webm"
            outside_path = sqlite_settings.data_dir / outside_relative_path
            outside_path.parent.mkdir(parents=True, exist_ok=True)
            prepared["source_path"].replace(outside_path)
            conn.execute(
                "UPDATE conversation_turns SET user_audio_path = ? WHERE session_id = ?",
                (outside_relative_path, prepared["session_id"]),
            )
            conn.execute(
                "UPDATE asr_attempts SET user_audio_path = ? WHERE session_id = ?",
                (outside_relative_path, prepared["session_id"]),
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
    finally:
        conn.close()

    assert summary.converted_attempts == 0
    assert summary.skipped[-1]["reason"] == "audio_path_outside_root"
    assert outside_path.read_bytes() == b"completed day one long audio"


def test_cleanup_updates_only_owned_rows_when_audio_path_is_shared(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="107",
            )
            unrelated_session_id, unrelated_path = _insert_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=1,
                attempt_id=prepared["attempt_id"],
                day_index=1,
                session_uuid="unrelated-shared-path",
                session_status="completed",
                audio_bytes=b"unrelated temporary bytes",
            )
            unrelated_path.unlink()
            conn.execute(
                "UPDATE experiment_sessions SET is_test = 1 WHERE id = ?",
                (unrelated_session_id,),
            )
            conn.execute(
                """
                UPDATE conversation_turns
                SET user_audio_path = ?, user_audio_sha256 = ?
                WHERE session_id = ?
                """,
                (
                    prepared["source_relative_path"],
                    hashlib.sha256(b"completed day one long audio").hexdigest(),
                    unrelated_session_id,
                ),
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        owned_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_path"]
        unrelated_db_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (unrelated_session_id,),
        ).fetchone()["user_audio_path"]
        owner_rows = conn.execute(
            "SELECT owner_table, owner_row_id FROM cleanup_operation_owners ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert owned_path == prepared["destination_relative_path"]
    assert unrelated_db_path == prepared["source_relative_path"]
    assert prepared["source_path"].read_bytes() == b"completed day one long audio"
    assert (sqlite_settings.data_dir / owned_path).read_bytes() == (
        b"completed day one long audio"
    )
    assert ("conversation_turns", unrelated_session_id) not in {
        (row["owner_table"], row["owner_row_id"]) for row in owner_rows
    }


def test_cleanup_wrong_destination_collision_rolls_owned_rows_back(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="108",
            )

        def collide_at_destination(**kwargs):
            destination = sqlite_settings.data_dir / kwargs["destination_path"]
            destination.write_bytes(b"foreign destination")
            raise FileExistsError("destination occupied")

        monkeypatch.setattr(cleanup_attempts, "_finalize_audio_file", collide_at_destination)
        summary = cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        committed_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (prepared["session_id"],),
        ).fetchone()["user_audio_path"]
        operation = conn.execute(
            "SELECT state, last_error FROM cleanup_operations WHERE operation_kind = 'relocate'"
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert committed_path == prepared["source_relative_path"]
    assert prepared["source_path"].read_bytes() == b"completed day one long audio"
    assert (sqlite_settings.data_dir / prepared["destination_relative_path"]).read_bytes() == (
        b"foreign destination"
    )
    assert operation["state"] == "rolled_back"
    assert operation["last_error"] == "destination_hash_mismatch"


def test_cleanup_deletion_survives_crash_after_database_commit(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Durable Delete",
                phone="13800000109",
                phone_hash="hash-durable-delete",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="durable-delete-crash",
            )
            plan = cleanup_attempts.plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        real_delete = cleanup_attempts._finalize_audio_deletion
        monkeypatch.setattr(
            cleanup_attempts,
            "_finalize_audio_deletion",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("delete interrupted")),
        )
        summary = cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        operation_before = conn.execute(
            "SELECT state, last_error FROM cleanup_operations WHERE operation_kind = 'delete'"
        ).fetchone()

        monkeypatch.setattr(cleanup_attempts, "_finalize_audio_deletion", real_delete)
        cleanup_attempts.reconcile_cleanup_operations(
            conn,
            data_dir=sqlite_settings.data_dir,
        )
        operation_after = conn.execute(
            "SELECT state FROM cleanup_operations WHERE operation_kind = 'delete'"
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert operation_before["state"] == "database_committed"
    assert operation_before["last_error"] == "delete_interrupted"
    assert operation_after["state"] == "completed"
    assert not audio_path.exists()


def test_reconciliation_completes_staged_delete_after_unlink_commit_failure(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    import sqlite3

    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Delete Unlink Crash",
                phone="13800000124",
                phone_hash="delete-unlink-crash",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="delete-unlink-crash",
            )
            plan = cleanup_attempts.plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        real_set_state = cleanup_attempts._set_cleanup_operation_state

        def fail_completion_state_write(*args, **kwargs):
            if kwargs.get("state") == "completed" and kwargs.get(
                "expected_states"
            ) == ("staged",):
                raise sqlite3.OperationalError("simulated completion commit failure")
            return real_set_state(*args, **kwargs)

        monkeypatch.setattr(
            cleanup_attempts,
            "_set_cleanup_operation_state",
            fail_completion_state_write,
        )
        with pytest.raises(
            sqlite3.OperationalError,
            match="simulated completion commit failure",
        ):
            cleanup_attempts.apply_attempt_cleanup(
                conn,
                plan=plan,
                data_dir=sqlite_settings.data_dir,
            )
        operation_before = conn.execute(
            """
            SELECT id, state, last_error, staging_path
            FROM cleanup_operations
            WHERE operation_kind = 'delete'
            """
        ).fetchone()
        conn.execute(
            """
            UPDATE cleanup_operations
            SET lease_expires_at = datetime('now', '-1 minute')
            WHERE id = ?
            """,
            (operation_before["id"],),
        )
    finally:
        conn.close()

    assert operation_before["state"] == "staged"
    assert operation_before["last_error"] is None
    assert not (sqlite_settings.data_dir / operation_before["staging_path"]).exists()

    replacement_bytes = b"replacement after cleanup crash"
    audio_path.write_bytes(replacement_bytes)
    monkeypatch.setattr(
        cleanup_attempts,
        "_set_cleanup_operation_state",
        real_set_state,
    )

    restart_conn = get_connection(sqlite_settings)
    try:
        cleanup_attempts.reconcile_cleanup_operations(
            restart_conn,
            data_dir=sqlite_settings.data_dir,
        )
        operation_after = restart_conn.execute(
            """
            SELECT state, last_error
            FROM cleanup_operations
            WHERE id = ?
            """,
            (operation_before["id"],),
        ).fetchone()
    finally:
        restart_conn.close()

    assert operation_after["state"] == "completed"
    assert operation_after["last_error"] == "delete_tombstone_already_unlinked"
    assert audio_path.read_bytes() == replacement_bytes


def test_cleanup_preserves_shared_audio_for_unowned_deletion_reference(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Shared Delete",
                phone="13800000116",
                phone_hash="shared-delete-hash",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="shared-delete-source",
            )
            shared_relative_path = str(audio_path.relative_to(sqlite_settings.data_dir))
            shared_hash = hashlib.sha256(b"incomplete audio").hexdigest()
            unrelated_session_id, unrelated_path = _insert_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=1,
                session_uuid="shared-delete-unrelated",
                session_status="completed",
                audio_bytes=b"temporary unrelated",
            )
            unrelated_path.unlink()
            conn.execute(
                "UPDATE experiment_sessions SET is_test = 1 WHERE id = ?",
                (unrelated_session_id,),
            )
            conn.execute(
                """
                UPDATE conversation_turns
                SET user_audio_path = ?, user_audio_sha256 = ?
                WHERE session_id = ?
                """,
                (shared_relative_path, shared_hash, unrelated_session_id),
            )
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        unrelated_db_path = conn.execute(
            "SELECT user_audio_path FROM conversation_turns WHERE session_id = ?",
            (unrelated_session_id,),
        ).fetchone()["user_audio_path"]
        operation = conn.execute(
            """
            SELECT preserve_source, state, last_error
            FROM cleanup_operations
            WHERE operation_kind = 'delete'
            """
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert summary.deleted_audio_files == 0
    assert unrelated_db_path == shared_relative_path
    assert audio_path.read_bytes() == b"incomplete audio"
    assert operation["preserve_source"] == 1
    assert operation["state"] == "completed"
    assert operation["last_error"] == "shared_source_preserved"


def test_reconciler_does_not_rollback_an_active_apply_lease(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="117",
            )

        participant_id = conn.execute(
            "SELECT participant_id FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["participant_id"]
        operations = cleanup_attempts._prepare_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            participant_id=participant_id,
            source_attempt_id=prepared["attempt_id"],
            worker_token="active-lease-worker",
        )
        other_conn = get_connection(sqlite_settings)
        try:
            with pytest.raises(cleanup_attempts.CleanupReconciliationError) as exc_info:
                cleanup_attempts.reconcile_cleanup_operations(
                    other_conn,
                    data_dir=sqlite_settings.data_dir,
                )
        finally:
            other_conn.close()
        cleanup_attempts._stage_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            relocations=operations,
        )
        final_state = conn.execute(
            "SELECT state FROM cleanup_operations WHERE operation_kind = 'relocate'"
        ).fetchone()["state"]
    finally:
        conn.close()

    assert exc_info.value.operations[0]["last_error"] == "active_lease"
    assert final_state == "staged"


def test_apply_cannot_overwrite_reconciler_terminal_state(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="118",
            )

        participant_id = conn.execute(
            "SELECT participant_id FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["participant_id"]
        operations = cleanup_attempts._prepare_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            participant_id=participant_id,
            source_attempt_id=prepared["attempt_id"],
            worker_token="terminalized-worker",
        )
        conn.execute(
            """
            UPDATE cleanup_operations
            SET state = 'rolled_back', worker_token = NULL,
                lease_expires_at = NULL, last_error = 'reconciler_rollback'
            WHERE operation_kind = 'relocate'
            """
        )
        with pytest.raises(cleanup_attempts.CleanupClaimLost):
            cleanup_attempts._stage_audio_relocations(
                conn,
                data_dir=sqlite_settings.data_dir,
                relocations=operations,
            )
        operation = conn.execute(
            "SELECT state, last_error FROM cleanup_operations WHERE operation_kind = 'relocate'"
        ).fetchone()
    finally:
        conn.close()

    assert operation["state"] == "rolled_back"
    assert operation["last_error"] == "reconciler_rollback"
    assert prepared["source_path"].read_bytes() == b"completed day one long audio"


def test_expired_worker_cannot_stage_source_after_reconciler_rollback(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="121",
            )
        participant_id = conn.execute(
            "SELECT participant_id FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["participant_id"]
        stale_operations = cleanup_attempts._prepare_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            participant_id=participant_id,
            source_attempt_id=prepared["attempt_id"],
            worker_token="expired-stage-worker",
        )
        conn.execute(
            """
            UPDATE cleanup_operations
            SET lease_expires_at = datetime('now', '-1 minute')
            WHERE id = ?
            """,
            (stale_operations[0].operation_id,),
        )
        cleanup_attempts.reconcile_cleanup_operations(
            conn,
            data_dir=sqlite_settings.data_dir,
        )

        with pytest.raises(cleanup_attempts.CleanupClaimLost):
            cleanup_attempts._stage_audio_relocations(
                conn,
                data_dir=sqlite_settings.data_dir,
                relocations=stale_operations,
            )
        terminal_state = conn.execute(
            "SELECT state FROM cleanup_operations WHERE id = ?",
            (stale_operations[0].operation_id,),
        ).fetchone()["state"]
    finally:
        conn.close()

    assert terminal_state == "rolled_back"
    assert prepared["source_path"].read_bytes() == b"completed day one long audio"
    assert not (sqlite_settings.data_dir / stale_operations[0].staging_path).exists()


def test_stale_worker_cannot_finalize_staging_after_terminal_review(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="122",
            )
        participant_id = conn.execute(
            "SELECT participant_id FROM participant_attempts WHERE id = ?",
            (prepared["attempt_id"],),
        ).fetchone()["participant_id"]
        operations = cleanup_attempts._prepare_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            participant_id=participant_id,
            source_attempt_id=prepared["attempt_id"],
            worker_token="expired-finalize-worker",
        )
        cleanup_attempts._stage_audio_relocations(
            conn,
            data_dir=sqlite_settings.data_dir,
            relocations=operations,
        )
        conn.execute(
            """
            UPDATE cleanup_operations
            SET state = 'database_committed'
            WHERE id = ?
            """,
            (operations[0].operation_id,),
        )
        stale_operation = cleanup_attempts._load_cleanup_operation(
            conn,
            operations[0].operation_id,
        )
        assert stale_operation is not None
        conn.execute(
            """
            UPDATE cleanup_operations
            SET state = 'review_needed', worker_token = NULL,
                lease_expires_at = NULL, last_error = 'terminal_review'
            WHERE id = ?
            """,
            (operations[0].operation_id,),
        )

        with pytest.raises(cleanup_attempts.CleanupClaimLost):
            cleanup_attempts._finalize_audio_relocations(
                conn,
                data_dir=sqlite_settings.data_dir,
                relocations=[stale_operation],
                failed_audio_paths=[],
            )
    finally:
        conn.close()

    assert (sqlite_settings.data_dir / stale_operation.staging_path).exists()
    assert not (sqlite_settings.data_dir / stale_operation.destination_path).exists()


def test_stale_worker_cannot_tombstone_source_after_terminal_rollback(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Stale Delete Worker",
                phone="13800000123",
                phone_hash="stale-delete-worker",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="stale-delete-worker",
            )
        operations, _ = cleanup_attempts._prepare_audio_deletions(
            conn,
            data_dir=sqlite_settings.data_dir,
            source_attempt_id=attempt_id,
            worker_token="expired-delete-worker",
        )
        conn.execute(
            "UPDATE cleanup_operations SET state = 'database_committed' WHERE id = ?",
            (operations[0].operation_id,),
        )
        stale_operation = cleanup_attempts._load_cleanup_operation(
            conn,
            operations[0].operation_id,
        )
        assert stale_operation is not None
        conn.execute(
            """
            UPDATE cleanup_operations
            SET state = 'rolled_back', worker_token = NULL,
                lease_expires_at = NULL, last_error = 'terminal_rollback'
            WHERE id = ?
            """,
            (operations[0].operation_id,),
        )

        with pytest.raises(cleanup_attempts.CleanupClaimLost):
            cleanup_attempts._finalize_audio_deletion(
                conn=conn,
                data_dir=sqlite_settings.data_dir,
                operation=stale_operation,
            )
    finally:
        conn.close()

    assert audio_path.read_bytes() == b"incomplete audio"
    assert not (sqlite_settings.data_dir / stale_operation.staging_path).exists()


def test_delete_staging_preserves_replacement_at_original_path(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Delete Replacement",
                phone="13800000119",
                phone_hash="delete-replacement-hash",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="delete-replacement",
            )
            plan = cleanup_attempts.plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        def replace_after_verification(**_kwargs):
            replacement = audio_path.with_name("replacement.tmp")
            replacement.write_bytes(b"replacement audio")
            replacement.replace(audio_path)

        monkeypatch.setattr(
            cleanup_attempts,
            "_before_delete_stage_rename",
            replace_after_verification,
        )
        summary = cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        operation = conn.execute(
            """
            SELECT state, last_error, staging_path
            FROM cleanup_operations
            WHERE operation_kind = 'delete'
            """
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert audio_path.read_bytes() == b"replacement audio"
    assert operation["state"] == "review_needed"
    assert operation["last_error"] == "delete_source_identity_changed"
    assert not (sqlite_settings.data_dir / operation["staging_path"]).exists()


def test_reconciliation_missing_paths_reports_precise_context(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.services import cleanup_attempts

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            prepared = _prepare_completed_audio_cleanup(
                conn,
                sqlite_settings=sqlite_settings,
                suffix="120",
            )
        real_finalize = cleanup_attempts._finalize_audio_file
        monkeypatch.setattr(
            cleanup_attempts,
            "_finalize_audio_file",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("interrupted")),
        )
        cleanup_attempts.apply_attempt_cleanup(
            conn,
            plan=prepared["plan"],
            data_dir=sqlite_settings.data_dir,
        )
        operation = conn.execute(
            """
            SELECT source_path, staging_path, destination_path
            FROM cleanup_operations
            WHERE operation_kind = 'relocate'
            """
        ).fetchone()
        (sqlite_settings.data_dir / operation["staging_path"]).unlink()
        monkeypatch.setattr(cleanup_attempts, "_finalize_audio_file", real_finalize)

        with pytest.raises(cleanup_attempts.CleanupReconciliationError) as exc_info:
            cleanup_attempts.reconcile_cleanup_operations(
                conn,
                data_dir=sqlite_settings.data_dir,
            )
    finally:
        conn.close()

    diagnostic = exc_info.value.operations[0]
    assert diagnostic["last_error"] == "reconcile_source_and_staging_missing"
    assert diagnostic["source_path"] == operation["source_path"]
    assert diagnostic["staging_path"] == operation["staging_path"]
    assert diagnostic["destination_path"] == operation["destination_path"]


def test_cleanup_concurrent_apply_creates_one_converted_attempt(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Concurrent Cleanup",
                phone="13800000110",
                phone_hash="hash-concurrent-cleanup",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
    finally:
        conn.close()

    barrier = threading.Barrier(2)

    def apply_from_connection() -> int:
        worker_conn = get_connection(sqlite_settings)
        try:
            worker_plan = plan_attempt_cleanup(
                worker_conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )
            barrier.wait(timeout=5)
            return apply_attempt_cleanup(
                worker_conn,
                plan=worker_plan,
                data_dir=sqlite_settings.data_dir,
            ).converted_attempts
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: apply_from_connection(), range(2)))

    conn = get_connection(sqlite_settings)
    try:
        converted_count = conn.execute(
            "SELECT COUNT(*) FROM participant_attempts WHERE source_attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert sorted(results) == [0, 1]
    assert converted_count == 1


def test_cleanup_converts_long_day_two_miss_to_short(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Missed Long",
                phone="13800000031",
                phone_hash="hash-missed-long",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            day_one_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=1)
            update_participant_day_status(
                conn,
                participant_day_id=day_one_id,
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="missed-day-two",
            )

            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        attempts = conn.execute(
            """
            SELECT id, status, participant_type, target_days, valid_for_export,
                   source_attempt_id, export_role, blocked_reason
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (participant_id,),
        ).fetchall()
        current_attempt_id = conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
        converted_days = conn.execute(
            """
            SELECT day_index, calendar_date, status
            FROM participant_days
            WHERE attempt_id = ?
            ORDER BY day_index
            """,
            (int(attempts[1]["id"]),),
        ).fetchall()
        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert plan.convertible_attempts[0].missed_day_indexes == [2]
    assert summary.scanned_attempts == 1
    assert summary.converted_attempts == 1
    assert summary.deleted_sessions == 1
    assert summary.deleted_audio_files == 1
    assert [(row["status"], row["participant_type"], row["export_role"]) for row in attempts] == [
        ("converted_to_short", "long", "normal_long"),
        ("completed", "short", "converted_short"),
    ]
    assert attempts[0]["target_days"] == 3
    assert attempts[0]["valid_for_export"] == 0
    assert attempts[0]["blocked_reason"] == "long_term_missed_day"
    assert attempts[1]["target_days"] == 1
    assert attempts[1]["valid_for_export"] == 1
    assert attempts[1]["source_attempt_id"] == attempts[0]["id"]
    assert current_attempt_id == attempts[1]["id"]
    assert [(row["day_index"], row["status"]) for row in converted_days] == [(1, "completed")]
    assert old_session_count == 0
    assert not audio_path.exists()


def test_cleanup_renames_completed_day_one_audio_paths_to_converted_short(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup
    from backend.app.services.file_naming import canonical_audio_relative_path

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            name = "Cleanup Rename"
            phone = "13800000038"
            participant_id = insert_participant_identity(
                conn,
                name=name,
                phone=phone,
                phone_hash="hash-cleanup-rename",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            session_id, old_audio_path, old_relative_path = (
                _insert_completed_day_one_session_with_long_audio(
                    conn,
                    sqlite_settings=sqlite_settings,
                    participant_id=participant_id,
                    attempt_id=attempt_id,
                    name=name,
                    phone=phone,
                    session_uuid="completed-day-one-canonical",
                )
            )
            expected_relative_path = canonical_audio_relative_path(
                name=name,
                phone=phone,
                participant_type="short",
                day_index=1,
                turn_index=1,
                session_id="completed-day-one-canonical",
                suffix=".webm",
            )

            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        turn_audio_path = conn.execute(
            """
            SELECT user_audio_path
            FROM conversation_turns
            WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()["user_audio_path"]
        asr_audio_paths = [
            row["user_audio_path"]
            for row in conn.execute(
                """
                SELECT user_audio_path
                FROM asr_attempts
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert old_relative_path.endswith("_long_day_1_turn_1_completed-day-one-canonical.webm")
    assert turn_audio_path == expected_relative_path
    assert asr_audio_paths == [expected_relative_path]
    assert not old_audio_path.exists()
    assert (sqlite_settings.data_dir / expected_relative_path).read_bytes() == (
        b"completed day one long audio"
    )


def test_cleanup_converts_long_attempt_and_deletes_asr_only_audio(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Missed Long ASR Only",
                phone="13800000036",
                phone_hash="hash-missed-long-asr-only",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            day_one_id = _participant_day_id(conn, attempt_id=attempt_id, day_index=1)
            update_participant_day_status(
                conn,
                participant_day_id=day_one_id,
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_asr_only_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="missed-day-two-asr-only",
            )

            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )

        old_session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
        old_asr_attempt_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            WHERE s.attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert plan.convertible_attempts[0].missed_day_indexes == [2]
    assert summary.converted_attempts == 1
    assert summary.deleted_sessions == 1
    assert summary.deleted_audio_files == 1
    assert old_session_count == 0
    assert old_asr_attempt_count == 0
    assert not audio_path.exists()


def test_cleanup_never_deletes_a_symlink_target_for_incomplete_audio(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Symlink Cleanup",
                phone="13800000039",
                phone_hash="hash-symlink-cleanup",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            managed_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="symlink-incomplete-audio",
            )
            unrelated_path = sqlite_settings.data_dir / "audio" / "unrelated.webm"
            unrelated_path.write_bytes(b"unrelated")
            managed_path.unlink()
            managed_path.symlink_to(unrelated_path.name)
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        deletion_operation = conn.execute(
            """
            SELECT operation_kind, state, last_error
            FROM cleanup_operations
            WHERE source_path = 'audio/symlink-incomplete-audio-turn-1.webm'
            """
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert unrelated_path.read_bytes() == b"unrelated"
    assert deletion_operation["operation_kind"] == "delete"
    assert deletion_operation["state"] == "review_needed"
    assert deletion_operation["last_error"] == "delete_source_symlink"


@pytest.mark.parametrize(
    ("source_state", "expected_error"),
    [
        ("missing", "delete_source_missing"),
        ("nonregular", "delete_source_nonregular"),
        ("traversal", "delete_path_outside_root"),
        ("outside_audio", "delete_path_outside_root"),
    ],
)
def test_cleanup_records_precise_review_for_unsafe_deletion_sources(
    sqlite_settings: Settings,
    source_state: str,
    expected_error: str,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name=f"Unsafe Delete {source_state}",
                phone={
                    "missing": "13800000112",
                    "nonregular": "13800000113",
                    "traversal": "13800000114",
                    "outside_audio": "13800000115",
                }[source_state],
                phone_hash=f"unsafe-delete-{source_state}",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid=f"unsafe-delete-{source_state}",
            )
            if source_state == "missing":
                audio_path.unlink()
            elif source_state == "nonregular":
                audio_path.unlink()
                audio_path.mkdir()
            else:
                unsafe_path = (
                    "audio/../escape.webm"
                    if source_state == "traversal"
                    else "exports/outside.webm"
                )
                conn.execute(
                    "UPDATE conversation_turns SET user_audio_path = ? WHERE user_audio_path = ?",
                    (unsafe_path, str(audio_path.relative_to(sqlite_settings.data_dir))),
                )
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )
        operation = conn.execute(
            """
            SELECT state, last_error
            FROM cleanup_operations
            WHERE operation_kind = 'delete'
            """
        ).fetchone()
    finally:
        conn.close()

    assert summary.converted_attempts == 1
    assert operation["state"] == "review_needed"
    assert operation["last_error"] == expected_error


def test_cleanup_dry_plan_skips_long_day_one_incomplete(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.cleanup_attempts import plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Day One Incomplete",
                phone="13800000032",
                phone_hash="hash-day-one",
            )
            _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )
    finally:
        conn.close()

    assert plan.convertible_attempts == []
    assert plan.skipped == [{"attempt_id": 1, "reason": "day_one_not_completed"}]


def test_cleanup_preserves_completed_day_two_history_when_day_three_missed(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Missed Day Three",
                phone="13800000033",
                phone_hash="hash-missed-day-three",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=2),
                status="completed",
                completed_at="2026-07-03T20:00:00+08:00",
            )
            day_two_session_id, day_two_audio_path = _insert_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="completed-day-two",
                session_status="completed",
                audio_bytes=b"completed day two audio",
            )
            _day_three_session_id, day_three_audio_path = _insert_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=3,
                session_uuid="incomplete-day-three",
                session_status="started",
                audio_bytes=b"incomplete day three audio",
            )
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-05",
                data_dir=sqlite_settings.data_dir,
            )

        summary = apply_attempt_cleanup(
            conn,
            plan=plan,
            data_dir=sqlite_settings.data_dir,
        )

        source_days = conn.execute(
            """
            SELECT day_index, status
            FROM participant_days
            WHERE attempt_id = ?
            ORDER BY day_index
            """,
            (attempt_id,),
        ).fetchall()
        source_sessions = conn.execute(
            """
            SELECT id, participant_day_id, status
            FROM experiment_sessions
            WHERE attempt_id = ?
            ORDER BY id
            """,
            (attempt_id,),
        ).fetchall()
        source_turns = conn.execute(
            """
            SELECT t.session_id, t.user_audio_path
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            WHERE s.attempt_id = ?
            ORDER BY t.id
            """,
            (attempt_id,),
        ).fetchall()
    finally:
        conn.close()

    assert plan.convertible_attempts[0].missed_day_indexes == [3]
    assert summary.converted_attempts == 1
    assert summary.deleted_sessions == 1
    assert summary.deleted_audio_files == 1
    assert [(row["day_index"], row["status"]) for row in source_days] == [
        (1, "completed"),
        (2, "completed"),
        (3, "not_started"),
    ]
    assert [(row["id"], row["status"]) for row in source_sessions] == [
        (day_two_session_id, "completed")
    ]
    assert [(row["session_id"], row["user_audio_path"]) for row in source_turns] == [
        (
            day_two_session_id,
            "audio/completed-day-two-turn-1.webm",
        )
    ]
    assert day_two_audio_path.exists()
    assert not day_three_audio_path.exists()


def test_cleanup_apply_is_idempotent(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Idempotent Cleanup",
                phone="13800000034",
                phone_hash="hash-idempotent-cleanup",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )

            first_plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        first_summary = apply_attempt_cleanup(
            conn,
            plan=first_plan,
            data_dir=sqlite_settings.data_dir,
        )
        second_summary = apply_attempt_cleanup(
            conn,
            plan=first_plan,
            data_dir=sqlite_settings.data_dir,
        )

        attempt_rows = conn.execute(
            """
            SELECT status, participant_type, source_attempt_id
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()

    assert first_summary.converted_attempts == 1
    assert second_summary.converted_attempts == 0
    assert second_summary.skipped[-1]["reason"] == "source_attempt_not_active"
    assert len(attempt_rows) == 2
    assert [(row["status"], row["participant_type"]) for row in attempt_rows] == [
        ("converted_to_short", "long"),
        ("completed", "short"),
    ]


def test_cleanup_rejects_unsafe_caller_owned_transaction(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )
    from backend.app.services.cleanup_attempts import apply_attempt_cleanup, plan_attempt_cleanup

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Cleanup Rollback",
                phone="13800000037",
                phone_hash="hash-cleanup-rollback",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
            audio_path = _insert_incomplete_session_with_audio(
                conn,
                sqlite_settings=sqlite_settings,
                participant_id=participant_id,
                attempt_id=attempt_id,
                day_index=2,
                session_uuid="cleanup-rollback-audio",
            )
            plan = plan_attempt_cleanup(
                conn,
                today="2026-07-04",
                data_dir=sqlite_settings.data_dir,
            )

        with pytest.raises(RuntimeError, match="must own"):
            with transaction(conn):
                apply_attempt_cleanup(
                    conn,
                    plan=plan,
                    data_dir=sqlite_settings.data_dir,
                )

        session_count = conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()[0]
    finally:
        conn.close()

    assert session_count == 1
    assert audio_path.exists()


def test_cleanup_cli_defaults_to_dry_run_and_apply_writes(sqlite_settings: Settings, capsys):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import (
        insert_participant_identity,
        update_participant_day_status,
    )

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="CLI Cleanup",
                phone="13800000035",
                phone_hash="hash-cli-cleanup",
            )
            attempt_id = _insert_long_attempt(
                conn,
                participant_id=participant_id,
                start_date="2026-07-02",
            )
            update_participant_day_status(
                conn,
                participant_day_id=_participant_day_id(conn, attempt_id=attempt_id, day_index=1),
                status="completed",
                completed_at="2026-07-02T20:00:00+08:00",
            )
    finally:
        conn.close()

    monkey_settings = lambda: sqlite_settings
    cleanup_participant_attempts.get_settings = monkey_settings

    dry_run_exit = cleanup_participant_attempts.main(
        ["--today", "2026-07-04", "--json"]
    )
    dry_run_output = json.loads(capsys.readouterr().out)

    conn = get_connection(sqlite_settings)
    try:
        attempt_rows_after_dry_run = conn.execute(
            """
            SELECT status, participant_type
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()

    apply_exit = cleanup_participant_attempts.main(
        ["--apply", "--today", "2026-07-04", "--json"]
    )
    apply_output = json.loads(capsys.readouterr().out)

    conn = get_connection(sqlite_settings)
    try:
        attempt_rows_after_apply = conn.execute(
            """
            SELECT status, participant_type
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no
            """,
            (participant_id,),
        ).fetchall()
    finally:
        conn.close()

    assert dry_run_exit == 0
    assert dry_run_output["mode"] == "dry_run"
    assert dry_run_output["converted_attempts"] == 0
    assert dry_run_output["planned_converted_attempts"] == 1
    assert [(row["status"], row["participant_type"]) for row in attempt_rows_after_dry_run] == [
        ("active", "long")
    ]

    assert apply_exit == 0
    assert apply_output["mode"] == "apply"
    assert apply_output["converted_attempts"] == 1
    assert apply_output["planned_converted_attempts"] == 1
    assert [(row["status"], row["participant_type"]) for row in attempt_rows_after_apply] == [
        ("converted_to_short", "long"),
        ("completed", "short"),
    ]


def test_cleanup_cli_dry_run_does_not_run_migrations(
    sqlite_settings: Settings,
    capsys,
):
    from backend.app.db import get_connection, run_migrations

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    cleanup_participant_attempts.get_settings = lambda: sqlite_settings

    def fail_run_migrations(_conn):
        raise AssertionError("dry-run should not run migrations")

    cleanup_participant_attempts.run_migrations = fail_run_migrations

    exit_code = cleanup_participant_attempts.main(
        ["--dry-run", "--today", "2026-07-04", "--json"]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["mode"] == "dry_run"
    assert payload["planned_converted_attempts"] == 0


def test_cleanup_cli_dry_run_does_not_create_wal_or_shm_sidecars(
    sqlite_settings: Settings,
    capsys,
):
    from backend.app.db import get_connection, run_migrations

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    db_path = Path(sqlite_settings.database_url.removeprefix("sqlite:///"))
    wal_path = db_path.parent / f"{db_path.name}-wal"
    shm_path = db_path.parent / f"{db_path.name}-shm"
    assert not wal_path.exists()
    assert not shm_path.exists()

    cleanup_participant_attempts.get_settings = lambda: sqlite_settings

    exit_code = cleanup_participant_attempts.main(
        ["--dry-run", "--today", "2026-07-04", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_cleanup_cli_dry_run_rejects_existing_wal_sidecar(
    sqlite_settings: Settings,
    capsys,
):
    from backend.app.db import get_connection, run_migrations

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    db_path = Path(sqlite_settings.database_url.removeprefix("sqlite:///"))
    wal_path = db_path.parent / f"{db_path.name}-wal"
    wal_path.touch()
    assert wal_path.exists()

    cleanup_participant_attempts.get_settings = lambda: sqlite_settings

    exit_code = cleanup_participant_attempts.main(
        ["--dry-run", "--today", "2026-07-04", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "WAL" in captured.err or "checkpoint" in captured.err
    assert wal_path.exists()


def test_cleanup_cli_dry_run_against_missing_sqlite_path_does_not_create_files(
    tmp_path: Path,
    capsys,
):
    from backend.app.settings import Settings

    missing_parent = tmp_path / "missing-dir"
    db_path = missing_parent / "cleanup-missing.db"
    sqlite_settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="cleanup-cli-missing",
    )

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)
    cleanup_participant_attempts.get_settings = lambda: sqlite_settings

    exit_code = cleanup_participant_attempts.main(
        ["--dry-run", "--today", "2026-07-04", "--json"]
    )

    captured = capsys.readouterr()
    assert exit_code != 0
    assert "does not exist" in captured.err
    assert not missing_parent.exists()
    assert not db_path.exists()
    assert not (db_path.parent / f"{db_path.name}-wal").exists()
    assert not (db_path.parent / f"{db_path.name}-shm").exists()


def test_cleanup_cli_json_reports_reconciliation_failure_without_traceback(
    sqlite_settings: Settings,
    capsys,
):
    from backend.app.db import get_connection, run_migrations
    from backend.app.services.cleanup_attempts import CleanupReconciliationError

    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts_reconciliation_failure",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    cleanup_participant_attempts.get_settings = lambda: sqlite_settings

    def fail_reconciliation(_conn, *, data_dir):
        del data_dir
        raise CleanupReconciliationError(
            [
                {
                    "operation_id": 7,
                    "operation_kind": "delete",
                    "state": "database_committed",
                    "last_error": "delete_permission_denied",
                }
            ]
        )

    cleanup_participant_attempts.reconcile_cleanup_operations = fail_reconciliation

    exit_code = cleanup_participant_attempts.main(["--apply", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert exit_code == 1
    assert captured.out == ""
    assert payload["error"] == "cleanup_reconciliation_failed"
    assert payload["operations"] == [
        {
            "operation_id": 7,
            "operation_kind": "delete",
            "state": "database_committed",
            "last_error": "delete_permission_denied",
        }
    ]
    assert "Traceback" not in captured.err


def test_cleanup_cli_rejects_invalid_today(capsys):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "cleanup_participant_attempts.py"
    module_spec = importlib.util.spec_from_file_location(
        "cleanup_participant_attempts",
        script_path,
    )
    assert module_spec is not None
    assert module_spec.loader is not None
    cleanup_participant_attempts = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cleanup_participant_attempts)

    with pytest.raises(SystemExit) as exc_info:
        cleanup_participant_attempts.main(["--dry-run", "--today", "2026-07-99"])

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "invalid ISO date" in captured.err
