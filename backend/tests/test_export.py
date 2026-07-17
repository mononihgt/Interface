from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_connection, run_migrations
from backend.app.repositories.attempts import create_attempt
from backend.app.settings import Settings
from backend.app.services.export import (
    build_export_payload,
    build_clean_data_export_payload,
    create_clean_data_export,
    create_reimbursement_export,
    create_v2_export,
)
from backend.app.time_utils import parse_stored_timestamp


class _CommitFailingConnection:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        fail_when: Callable[[], bool],
        persistent: bool,
    ) -> None:
        self._conn = conn
        self._fail_when = fail_when
        self._persistent = persistent
        self.commit_failures = 0
        self.rollback_calls = 0

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def execute(self, *args, **kwargs):
        return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        if self._fail_when() and (self._persistent or self.commit_failures == 0):
            self.commit_failures += 1
            raise sqlite3.OperationalError("simulated commit failure")
        self._conn.commit()

    def rollback(self) -> None:
        self.rollback_calls += 1
        self._conn.rollback()


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"task13-salt{password}".encode("utf-8")).hexdigest()


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "export.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt="task13-salt",
        admin_password_hash=_password_hash("admin-pass-123"),
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
    name: str,
    phone: str,
    phone_hash: str,
    created_at: str,
    participant_type: str = "short",
    target_days: int | None = None,
) -> int:
    resolved_target_days = target_days if target_days is not None else (3 if participant_type == "long" else 1)
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
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'topic-qa', 'factual_minor', ?, 'active', ?, ?)
        """,
        (name, phone, phone_hash, participant_type, resolved_target_days, created_at, created_at),
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
        target_days=resolved_target_days,
        status="active",
        valid_for_export=True,
    )
    conn.execute(
        "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
        (attempt_id, participant_id),
    )
    return participant_id


def _insert_participant_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None = None,
    day_index: int = 1,
    created_at: str,
) -> int:
    resolved_attempt_id = attempt_id
    if resolved_attempt_id is None:
        row = conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        resolved_attempt_id = int(row["current_attempt_id"])
    cursor = conn.execute(
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
        ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            day_index,
            f"2026-07-{day_index + 1:02d}",
            created_at,
            created_at,
            created_at,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _insert_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None = None,
    day_index: int = 1,
    created_at: str,
    payload: dict[str, object] | None = None,
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
        ) VALUES (?, ?, ?, 'final', ?, 1, ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            day_index,
            json.dumps(
                payload
                or {
                    "ai_familiarity": 4,
                    "trust_expectation": 5,
                    "usage_frequency": "weekly",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            created_at,
            created_at,
            created_at,
            created_at,
        ),
    )


def _insert_session_bundle(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None = None,
    participant_day_id: int,
    session_uuid: str,
    is_test: bool,
    created_at: str,
) -> None:
    resolved_attempt_id = attempt_id
    if resolved_attempt_id is None:
        row = conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()
        resolved_attempt_id = int(row["current_attempt_id"])
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
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'topic-qa', 'scenario-1', 'graph-v1', 'factual_minor', 2, 'completed', ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            resolved_attempt_id,
            participant_day_id,
            session_uuid,
            created_at,
            created_at,
            json.dumps({"platform": "desktop"}, ensure_ascii=False, sort_keys=True),
            1 if is_test else 0,
            created_at,
            created_at,
        ),
    )
    session_id = int(session_cursor.lastrowid)
    audio_bytes = f"audio-{session_uuid}".encode()
    audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
    database_path = Path(
        str(conn.execute("PRAGMA database_list").fetchone()["file"])
    )
    audio_path = database_path.parent / "audio" / f"{session_uuid}.wav"
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
        ) VALUES (?, 1, ?, 'voice', ?, ?, 'tencent', 'success', ?, 1200, ?, 800, 'yi-zhan', 'gpt-5.1', 'chat', ?, 0, NULL, 0, 'none', NULL, NULL, NULL, ?, ?)
        """,
        (
            session_id,
            f"user-{session_uuid}",
            f"audio/{session_uuid}.wav",
            audio_sha256,
            f"asr-{session_uuid}",
            f"assistant-{session_uuid}",
            json.dumps([{"provider": "yi-zhan", "model": "gpt-5.1"}], ensure_ascii=False),
            json.dumps({"step": "complete"}, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )
    turn_id = int(turn_cursor.lastrowid)

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

    conn.execute(
        """
        INSERT INTO task_artifacts (
            turn_id,
            artifact_type,
            status,
            payload_json,
            visible_to_participant,
            created_at
        ) VALUES (?, 'plan_card', 'completed', ?, 1, ?)
        """,
        (
            turn_id,
            json.dumps({"title": f"artifact-{session_uuid}"}, ensure_ascii=False, sort_keys=True),
            created_at,
        ),
    )

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
            http_status,
            error_code,
            error_message_summary,
            latency_ms,
            cooldown_applied,
            created_at
        ) VALUES (?, ?, 1, ?, 'chat', 'yi-zhan', 'gpt-5.1', 'success', 200, NULL, NULL, 800, 0, ?)
        """,
        (f"{session_uuid}-turn-1", session_id, int(is_test), created_at),
    )


def test_turn_export_includes_client_response_timing(
    conn: sqlite3.Connection,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Timing Participant",
        phone="13800138009",
        phone_hash="hash-client-timing",
        created_at="2026-07-13T10:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-13T10:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-client-timing",
        is_test=False,
        created_at="2026-07-13T10:01:00+08:00",
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET
            client_message_sent_at = '2026-07-13T10:01:05.000+08:00',
            assistant_render_completed_at = '2026-07-13T10:01:09.230+08:00',
            client_response_latency_ms = 4230,
            client_timing_interrupted = 1,
            render_timing_received_at = '2026-07-13T02:01:09'
        """
    )

    payload = build_export_payload(conn)

    turn_row = payload["turns.csv"][0]
    assert turn_row["client_message_sent_at"] == "2026-07-13T10:01:05.000+08:00"
    assert (
        turn_row["assistant_render_completed_at"]
        == "2026-07-13T10:01:09.230+08:00"
    )
    assert turn_row["client_response_latency_ms"] == 4230
    assert turn_row["client_timing_interrupted"] is True
    assert turn_row["render_timing_received_at"] == "2026-07-13T02:01:09"


def _insert_eligible_clean_export_candidate(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
) -> tuple[int, Path]:
    participant_id = _insert_participant(
        conn,
        name="Clean Export Candidate",
        phone="13800138000",
        phone_hash=f"hash-{session_uuid}",
        created_at="2026-07-02T09:00:00+08:00",
    )
    attempt_id = int(
        conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid=session_uuid,
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (participant_id, attempt_id),
    )
    database_path = Path(
        str(conn.execute("PRAGMA database_list").fetchone()["file"])
    )
    return participant_id, database_path.parent / "audio" / f"{session_uuid}.wav"


def _read_csv_from_zip(archive_path: Path, member_name: str) -> tuple[list[str], list[dict[str, str]]]:
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member_name) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8", newline="")
            reader = csv.DictReader(text)
            return list(reader.fieldnames or []), list(reader)


def _read_jsonl_from_zip(archive_path: Path, member_name: str) -> list[dict[str, object]]:
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member_name) as handle:
            text = io.TextIOWrapper(handle, encoding="utf-8")
            return [json.loads(line) for line in text if line.strip()]


def _read_json_from_zip(archive_path: Path, member_name: str) -> dict[str, object]:
    with zipfile.ZipFile(archive_path) as archive:
        with archive.open(member_name) as handle:
            return json.loads(handle.read().decode("utf-8"))


def test_export_ratings_use_stance_score() -> None:
    from backend.app.services.export import CSV_FIELDNAMES

    assert "stance_score" in CSV_FIELDNAMES["ratings.csv"]
    assert "trust_score" in CSV_FIELDNAMES["ratings.csv"]
    assert "impression_score" not in CSV_FIELDNAMES["ratings.csv"]


def test_stored_timestamp_parser_rejects_date_only_values() -> None:
    with pytest.raises(ValueError, match="explicit time component"):
        parse_stored_timestamp("2026-07-02")


def test_export_participants_rows_use_current_attempt_state(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Attempt Export Participant",
        phone="13800138222",
        phone_hash="hash-attempt-export",
        created_at="2026-07-02T09:00:00+08:00",
        participant_type="short",
    )
    attempt_row = conn.execute(
        "SELECT current_attempt_id FROM participants WHERE id = ?",
        (participant_id,),
    ).fetchone()
    assert attempt_row is not None
    attempt_id = int(attempt_row["current_attempt_id"])
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        created_at="2026-07-02T10:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        participant_day_id=participant_day_id,
        session_uuid="attempt-export-session",
        is_test=False,
        created_at="2026-07-02T10:30:00+08:00",
    )
    conn.execute(
        """
        UPDATE participant_attempts
        SET
            participant_type = 'long',
            condition = 'tool',
            subcondition = 'planning',
            topic_key = 'goalPlan',
            error_type_id = 'logic_major',
            target_days = 3,
            status = 'blocked',
            blocked_reason = 'manual_review'
        WHERE id = ?
        """,
        (attempt_id,),
    )
    conn.execute(
        """
        UPDATE participants
        SET
            participant_type = 'short',
            condition = 'human',
            subcondition = 'qa',
            topic_key = 'legacy-topic',
            error_type_id = 'factual_minor',
            target_days = 1,
            current_status = 'active'
        WHERE id = ?
        """,
        (participant_id,),
    )
    conn.commit()

    archive_path = tmp_path / "attempt-state-export.zip"
    create_v2_export(conn, sqlite_settings, archive_path)
    _, participant_rows = _read_csv_from_zip(archive_path, "participants.csv")

    assert len(participant_rows) == 1
    row = participant_rows[0]
    assert row["participant_type"] == "long"
    assert row["condition"] == "tool"
    assert row["subcondition"] == "planning"
    assert row["topic_key"] == "goalPlan"
    assert row["error_type_id"] == "logic_major"
    assert row["target_days"] == "3"
    assert row["current_status"] == "blocked"
    assert row["blocked_reason"] == "manual_review"


def _assert_archive_excludes_known_stored_identity_bytes(
    archive_path: Path,
    *,
    forbidden_values: tuple[str, ...],
) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.comment == b""
        for member in archive.infolist():
            searchable_metadata = b"\n".join(
                (
                    member.filename.encode("utf-8"),
                    member.comment,
                    member.extra,
                )
            )
            content = archive.read(member)
            for forbidden_value in forbidden_values:
                forbidden_bytes = forbidden_value.encode("utf-8")
                assert forbidden_bytes not in searchable_metadata, member.filename
                assert forbidden_bytes not in content, member.filename


def test_experiment_and_clean_data_archives_sanitize_known_stored_identity_bytes(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    raw_name = "隐私测试甲"
    raw_phone = "13800138000"
    reimbursement_id = "330102199001011234"
    participant_id = _insert_participant(
        conn,
        name=raw_name,
        phone=raw_phone,
        phone_hash="hash-formal",
        created_at="2026-07-02T09:00:00+08:00",
    )
    attempt_id = int(
        conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
        payload={
            "demographics": {
                "birthDate": "1990-01-01",
                "gender": "female",
                "idNumber": reimbursement_id,
            },
            "scales": {"q1": 4},
        },
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-formal",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.execute(
        """
        UPDATE experiment_sessions
        SET client_info_json = ?
        WHERE session_uuid = 'session-formal'
        """,
        (
            json.dumps(
                {"participant_name": raw_name, "participant_phone": raw_phone},
                ensure_ascii=False,
            ),
        ),
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET user_text = ?, agent_state_json = ?
        WHERE session_id = (
            SELECT id FROM experiment_sessions WHERE session_uuid = 'session-formal'
        )
        """,
        (
            f"我的姓名是{raw_name}，手机号是{raw_phone}",
            json.dumps(
                {"phone_hash": "hash-formal", "reimbursement_id": reimbursement_id},
                ensure_ascii=False,
            ),
        ),
    )
    conn.execute(
        """
        UPDATE task_artifacts
        SET payload_json = ?
        WHERE turn_id IN (
            SELECT id
            FROM conversation_turns
            WHERE session_id = (
                SELECT id FROM experiment_sessions WHERE session_uuid = 'session-formal'
            )
        )
        """,
        (json.dumps({"owner": raw_name, "phone": raw_phone}, ensure_ascii=False),),
    )
    conn.execute(
        """
        UPDATE api_call_logs
        SET error_message_summary = ?
        WHERE request_id = 'session-formal-turn-1'
        """,
        (f"hidden {raw_phone} {reimbursement_id}",),
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (participant_id, attempt_id),
    )
    conn.commit()

    experiment_archive = tmp_path / "experiment.zip"
    clean_archive = tmp_path / "clean.zip"
    create_v2_export(conn, sqlite_settings, experiment_archive)
    create_clean_data_export(conn, sqlite_settings, clean_archive)

    forbidden_values = (
        raw_name,
        raw_phone,
        "138****8000",
        "hash-formal",
        reimbursement_id,
        '"idNumber"',
    )
    _assert_archive_excludes_known_stored_identity_bytes(
        experiment_archive,
        forbidden_values=forbidden_values,
    )
    _assert_archive_excludes_known_stored_identity_bytes(
        clean_archive,
        forbidden_values=forbidden_values,
    )

    for archive_path in (experiment_archive, clean_archive):
        participant_fieldnames, participant_rows = _read_csv_from_zip(
            archive_path,
            "participants.csv",
        )
        assert {"name", "phone", "masked_phone", "phone_hash"}.isdisjoint(
            participant_fieldnames
        )
        assert participant_rows[0]["participant_id"].startswith("participant-")
        assert participant_rows[0]["attempt_id"].startswith("attempt-")

        _, session_rows = _read_csv_from_zip(archive_path, "sessions.csv")
        assert session_rows[0]["participant_id"] == participant_rows[0]["participant_id"]
        assert session_rows[0]["attempt_id"] == participant_rows[0]["attempt_id"]

        pretest_rows = _read_jsonl_from_zip(archive_path, "pretest_responses.jsonl")
        assert pretest_rows[0]["participant_id"] == participant_rows[0]["participant_id"]
        assert pretest_rows[0]["attempt_id"] == participant_rows[0]["attempt_id"]
        assert pretest_rows[0]["payload"]["demographics"] == {
            "birthDate": "1990-01-01",
            "gender": "female",
        }


def test_export_preserves_authoritative_taxonomies_when_names_collide(
    conn: sqlite3.Connection,
) -> None:
    participant_types = ("short", "long")
    conditions = ("human", "tool")
    subconditions = ("qa", "planning", "chat", "decision", "execution")
    error_type_ids = (
        "factual_minor",
        "factual_major",
        "logic_minor",
        "logic_major",
        "social_minor",
        "social_major",
        "system_failure",
    )
    collision_names = (
        *participant_types,
        *conditions,
        *subconditions,
        *error_type_ids,
    )
    expected_rows: list[dict[str, str]] = []

    for index, collision_name in enumerate(collision_names, start=1):
        participant_type = (
            collision_name
            if collision_name in participant_types
            else participant_types[index % 2]
        )
        condition = (
            collision_name if collision_name in conditions else conditions[index % 2]
        )
        subcondition = (
            collision_name if collision_name in subconditions else subconditions[index % 5]
        )
        error_type_id = (
            collision_name if collision_name in error_type_ids else error_type_ids[index % 7]
        )
        participant_id = _insert_participant(
            conn,
            name=collision_name,
            phone=f"1991{index:07d}",
            phone_hash=f"hash-taxonomy-{index}",
            created_at=f"2026-07-02T09:{index:02d}:00+08:00",
            participant_type=participant_type,
        )
        attempt_id = int(
            conn.execute(
                "SELECT current_attempt_id FROM participants WHERE id = ?",
                (participant_id,),
            ).fetchone()["current_attempt_id"]
        )
        participant_day_id = _insert_participant_day(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
            created_at=f"2026-07-02T10:{index:02d}:00+08:00",
        )
        session_uuid = f"session-taxonomy-{index:02d}"
        _insert_session_bundle(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
            participant_day_id=participant_day_id,
            session_uuid=session_uuid,
            is_test=False,
            created_at=f"2026-07-02T11:{index:02d}:00+08:00",
        )
        conn.execute(
            """
            UPDATE participant_attempts
            SET participant_type = ?, condition = ?, subcondition = ?, error_type_id = ?
            WHERE id = ?
            """,
            (participant_type, condition, subcondition, error_type_id, attempt_id),
        )
        conn.execute(
            """
            UPDATE experiment_sessions
            SET condition = ?, subcondition = ?, error_type_id = ?
            WHERE session_uuid = ?
            """,
            (condition, subcondition, error_type_id, session_uuid),
        )
        expected_rows.append(
            {
                "participant_type": participant_type,
                "condition": condition,
                "subcondition": subcondition,
                "error_type_id": error_type_id,
            }
        )

    payload = build_export_payload(conn)

    assert [
        {
            "participant_type": row["participant_type"],
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "error_type_id": row["error_type_id"],
        }
        for row in payload["participants.csv"]
    ] == expected_rows
    assert [
        {
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "error_type_id": row["error_type_id"],
        }
        for row in payload["sessions.csv"]
    ] == [
        {
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "error_type_id": row["error_type_id"],
        }
        for row in expected_rows
    ]


def test_export_redacts_normalized_identity_variants_only_in_untrusted_content(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    stored_name = "Élodie Privacy"
    canonical_phone = "13800138000"
    participant_id = _insert_participant(
        conn,
        name=stored_name,
        phone=canonical_phone,
        phone_hash="hash-normalized-identity",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-normalized-identity",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    variants = (
        "E\u0301LODIE PRIVACY",
        "éLoDiE pRiVaCy",
        "+86 138-0013-8000",
        "138 0013 8000",
        "0086-138-0013-8000",
    )
    conn.execute(
        """
        UPDATE experiment_sessions
        SET client_info_json = ?
        WHERE session_uuid = 'session-normalized-identity'
        """,
        (json.dumps({"owner": variants[0], "phone": variants[2]}, ensure_ascii=False),),
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET user_text = ?, assistant_text = ?, agent_state_json = ?
        WHERE session_id = (
            SELECT id
            FROM experiment_sessions
            WHERE session_uuid = 'session-normalized-identity'
        )
        """,
        (
            f"user {variants[1]} {variants[3]}",
            f"assistant {variants[0]} {variants[2]}",
            json.dumps({"identity": variants[1], "phone": variants[4]}, ensure_ascii=False),
        ),
    )
    conn.execute(
        """
        UPDATE task_artifacts
        SET payload_json = ?
        WHERE turn_id IN (
            SELECT id
            FROM conversation_turns
            WHERE session_id = (
                SELECT id
                FROM experiment_sessions
                WHERE session_uuid = 'session-normalized-identity'
            )
        )
        """,
        (json.dumps({"owner": variants[0], "phone": variants[4]}, ensure_ascii=False),),
    )
    conn.execute(
        """
        UPDATE api_call_logs
        SET error_message_summary = ?
        WHERE request_id = 'session-normalized-identity-turn-1'
        """,
        (f"summary {variants[1]} {variants[2]}",),
    )
    conn.commit()

    archive_path = tmp_path / "normalized-identity.zip"
    create_v2_export(conn, sqlite_settings, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        structured_content = b"\n".join(
            archive.read(member_name)
            for member_name in archive.namelist()
            if not member_name.startswith("audio/")
        )
    for variant in variants:
        assert variant.encode("utf-8") not in structured_content
    assert structured_content.count(b"[REDACTED]") >= 10

    payload = build_export_payload(conn)
    participant_row = payload["participants.csv"][0]
    session_row = payload["sessions.csv"][0]
    assert participant_row["participant_type"] == "short"
    assert participant_row["condition"] == "human"
    assert participant_row["subcondition"] == "qa"
    assert participant_row["error_type_id"] == "factual_minor"
    assert session_row["condition"] == "human"
    assert session_row["subcondition"] == "qa"
    assert session_row["error_type_id"] == "factual_minor"


def test_export_redacts_full_unicode_casefold_expansions_in_untrusted_text(
    conn: sqlite3.Connection,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Straße",
        phone="13800138000",
        phone_hash="hash-unicode-casefold",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-unicode-casefold",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.execute(
        """
        UPDATE conversation_turns
        SET user_text = 'Owner STRASSE remains private'
        WHERE session_id = (
            SELECT id
            FROM experiment_sessions
            WHERE session_uuid = 'session-unicode-casefold'
        )
        """
    )

    payload = build_export_payload(conn)

    assert payload["turns.csv"][0]["user_text"] == "Owner [REDACTED] remains private"
    assert payload["participants.csv"][0]["condition"] == "human"
    assert payload["participants.csv"][0]["subcondition"] == "qa"
    assert payload["participants.csv"][0]["error_type_id"] == "factual_minor"


def test_reimbursement_archive_contains_only_required_payment_identity_fields(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="报销测试甲",
        phone="13800138001",
        phone_hash="hash-reimbursement-private",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
        payload={"demographics": {"idNumber": "330102199001011235"}},
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-reimbursement-private",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.commit()

    archive_path = tmp_path / "reimbursement.zip"
    create_reimbursement_export(conn, sqlite_settings, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["reimbursement.csv"]
        assert archive.comment == b""
        member = archive.infolist()[0]
        assert member.comment == b""
        assert b"hash-reimbursement-private" not in member.extra

    fieldnames, rows = _read_csv_from_zip(archive_path, "reimbursement.csv")
    assert fieldnames == [
        "name",
        "phone",
        "id_number",
        "target_days",
        "completed_days",
    ]
    assert rows == [
        {
            "name": "报销测试甲",
            "phone": "13800138001",
            "id_number": "330102199001011235",
            "target_days": "1",
            "completed_days": "1",
        }
    ]


def test_interface_export_members_include_json_audio_and_integrated_csv(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Export Person",
        phone="13800138000",
        phone_hash="hash-interface-export",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-formal",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    source_audio_path = sqlite_settings.data_dir / "audio" / "session-formal.wav"
    source_audio_path.parent.mkdir(parents=True, exist_ok=True)
    source_audio_path.write_bytes(b"interface export audio")
    conn.execute(
        "UPDATE conversation_turns SET user_audio_sha256 = ? WHERE user_audio_path = ?",
        (
            hashlib.sha256(b"interface export audio").hexdigest(),
            "audio/session-formal.wav",
        ),
    )
    conn.commit()

    archive_path = tmp_path / "interface-export.zip"
    create_v2_export(conn, sqlite_settings, archive_path)

    canonical_stem = (
        "participant-00000001_attempt-00000001_short_day_1_turn_1_session-00000001"
    )
    json_member = f"json/{canonical_stem}.json"
    audio_member = f"audio/{canonical_stem}.wav"
    with zipfile.ZipFile(archive_path) as archive:
        member_names = set(archive.namelist())
        assert json_member in member_names
        assert audio_member in member_names
        assert "integrated.csv" in member_names
        assert archive.read(audio_member) == b"interface export audio"

    exported_json = _read_json_from_zip(archive_path, json_member)
    assert exported_json["participantId"] == "participant-00000001"
    assert exported_json["attemptId"] == "attempt-00000001"
    assert "participantName" not in exported_json
    assert "participantPhone" not in exported_json
    assert exported_json["participantType"] == "short"
    assert exported_json["day"] == 1
    assert exported_json["turn"] == 1
    assert exported_json["sessionId"] == "session-formal"
    assert exported_json["audioFile"] == audio_member
    assert exported_json["trials"][0]["conversationHistory"][0]["speaker"] == "user"
    assert exported_json["trials"][0]["conversationHistory"][1]["speaker"] == "ai"
    assert exported_json["trials"][0]["llmProvider"] == "yi-zhan"
    assert exported_json["trials"][0]["llmModel"] == "gpt-5.1"
    assert exported_json["trials"][0]["llmRoute"] == "chat"

    _, integrated_rows = _read_csv_from_zip(archive_path, "integrated.csv")
    assert integrated_rows == [
        {
            "json_file": json_member,
            "audio_file": audio_member,
            "participant_id": "participant-00000001",
            "attempt_id": "attempt-00000001",
            "participant_type": "short",
            "day": "1",
            "turn": "1",
            "session_id": "session-formal",
            "user_text": "user-session-formal",
            "assistant_text": "assistant-session-formal",
            "llm_provider": "yi-zhan",
            "llm_model": "gpt-5.1",
            "llm_route": "chat",
            "stance_score": "4",
            "trust_score": "6",
        }
    ]
    integrated_text = "\n".join(",".join(row.values()) for row in integrated_rows)
    assert "interface export audio" not in integrated_text


def test_api_call_logs_export_uses_session_fk_and_rebuilds_safe_summary(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    malicious_error_code = (
        "unavailable host=https://provider.invalid prompt=private "
        "token=secret DEEPSEEK_API_KEY=key RAW_PROVIDER_ERROR_SENTINEL"
    )
    participant_id = _insert_participant(
        conn,
        name="API Log Export Person",
        phone="13800138000",
        phone_hash="hash-api-log-export",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-api-log-export",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    session_row = conn.execute(
        "SELECT id FROM experiment_sessions WHERE session_uuid = ?",
        ("session-api-log-export",),
    ).fetchone()
    conn.execute(
        """
        UPDATE api_call_logs
        SET status = 'http_error',
            http_status = 503,
            error_code = ?,
            error_message_summary = 'raw provider secret prompt bearer token=abc123'
        WHERE session_id = ?
        """,
        (malicious_error_code, session_row["id"]),
    )
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
            error_message_summary,
            cooldown_applied,
            created_at
        ) VALUES (?, NULL, 1, 0, 'chat', 'evil-provider', 'evil-model',
                  'http_error', 'must not be selected by request prefix', 0, ?)
        """,
        ("session-api-log-export-spoofed", "2026-07-02T10:00:00+08:00"),
    )
    attempt_id = conn.execute(
        "SELECT current_attempt_id FROM participants WHERE id = ?",
        (participant_id,),
    ).fetchone()["current_attempt_id"]
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (participant_id, attempt_id),
    )
    conn.commit()

    archive_path = tmp_path / "api-log-export.zip"
    clean_archive_path = tmp_path / "api-log-clean-export.zip"
    create_v2_export(conn, sqlite_settings, archive_path)
    create_clean_data_export(conn, sqlite_settings, clean_archive_path)

    forbidden_fragments = (
        b"provider.invalid",
        b"prompt=private",
        b"token=secret",
        b"DEEPSEEK_API_KEY",
        b"RAW_PROVIDER_ERROR_SENTINEL",
        b"raw provider secret prompt",
        b"must not be selected by request prefix",
    )
    for exported_archive_path in (archive_path, clean_archive_path):
        _, rows = _read_csv_from_zip(exported_archive_path, "api_call_logs.csv")
        assert len(rows) == 1
        assert rows[0]["request_id"] == "session-api-log-export-turn-1"
        assert rows[0]["session_id"] == str(session_row["id"])
        assert rows[0]["turn_index"] == "1"
        assert rows[0]["is_test"] == "0"
        assert rows[0]["error_code"] == "http_error"
        assert rows[0]["error_message_summary"] == "http_error:503:http_error"
        with zipfile.ZipFile(exported_archive_path) as archive:
            archive_content = b"\n".join(
                archive.read(member_name)
                for member_name in archive.namelist()
                if not member_name.startswith("audio/")
            )
        for forbidden_fragment in forbidden_fragments:
            assert forbidden_fragment not in archive_content


def test_formal_export_excludes_is_test_rows_by_default(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    formal_participant_id = _insert_participant(
        conn,
        name="Formal Participant",
        phone="13800138000",
        phone_hash="hash-formal",
        created_at="2026-07-02T09:00:00+08:00",
    )
    formal_day_id = _insert_participant_day(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=formal_participant_id,
        participant_day_id=formal_day_id,
        session_uuid="session-formal",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    test_participant_id = _insert_participant(
        conn,
        name="Test Participant",
        phone="13900139000",
        phone_hash="hash-test",
        created_at="2026-07-02T11:00:00+08:00",
    )
    test_day_id = _insert_participant_day(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=test_participant_id,
        participant_day_id=test_day_id,
        session_uuid="session-test",
        is_test=True,
        created_at="2026-07-02T12:00:00+08:00",
    )

    archive_path = tmp_path / "formal-only.zip"
    create_v2_export(conn, sqlite_settings, archive_path)

    _, participant_rows = _read_csv_from_zip(archive_path, "participants.csv")
    _, session_rows = _read_csv_from_zip(archive_path, "sessions.csv")
    _, turn_rows = _read_csv_from_zip(archive_path, "turns.csv")
    _, rating_rows = _read_csv_from_zip(archive_path, "ratings.csv")
    _, api_call_log_rows = _read_csv_from_zip(archive_path, "api_call_logs.csv")
    artifact_rows = _read_jsonl_from_zip(archive_path, "artifacts.jsonl")
    pretest_rows = _read_jsonl_from_zip(archive_path, "pretest_responses.jsonl")

    assert [row["participant_id"] for row in participant_rows] == [
        "participant-00000001"
    ]
    assert {"name", "phone", "masked_phone", "phone_hash"}.isdisjoint(
        participant_rows[0]
    )
    assert [row["session_uuid"] for row in session_rows] == ["session-formal"]
    assert [row["session_id"] for row in turn_rows] == [session_rows[0]["session_id"]]
    assert len(rating_rows) == 1
    assert [row["request_id"] for row in api_call_log_rows] == ["session-formal-turn-1"]
    assert artifact_rows[0]["session_uuid"] == "session-formal"
    assert [row["participant_id"] for row in pretest_rows] == [
        f"participant-{formal_participant_id:08d}"
    ]


def test_converted_short_export_only_includes_source_day_one(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Converted Participant",
        phone="13800138000",
        phone_hash="hash-converted",
        created_at="2026-07-02T09:00:00+08:00",
        participant_type="long",
        target_days=3,
    )
    source_attempt_id = conn.execute(
        """
        UPDATE participant_attempts
        SET status = 'converted_to_short', valid_for_export = 0, export_role = 'normal_long'
        WHERE participant_id = ?
        RETURNING id
        """,
        (participant_id,),
    ).fetchone()["id"]
    converted_attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type="short",
        condition="human",
        subcondition="qa",
        topic_key="advice",
        error_type_id="factual_minor",
        target_days=1,
        status="completed",
        valid_for_export=True,
        source_attempt_id=int(source_attempt_id),
        export_role="converted_short",
    )
    conn.execute(
        "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
        (converted_attempt_id, participant_id),
    )
    day_one_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        attempt_id=int(source_attempt_id),
        day_index=1,
        created_at="2026-07-02T09:00:00+08:00",
    )
    day_two_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        attempt_id=int(source_attempt_id),
        day_index=2,
        created_at="2026-07-03T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        attempt_id=int(source_attempt_id),
        day_index=1,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        attempt_id=int(source_attempt_id),
        participant_day_id=day_one_id,
        session_uuid="converted-day-one",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        attempt_id=int(source_attempt_id),
        participant_day_id=day_two_id,
        session_uuid="converted-day-two-history",
        is_test=False,
        created_at="2026-07-03T10:00:00+08:00",
    )

    archive_path = tmp_path / "converted.zip"
    create_v2_export(conn, sqlite_settings, archive_path)
    _, session_rows = _read_csv_from_zip(archive_path, "sessions.csv")
    _, participant_rows = _read_csv_from_zip(archive_path, "participants.csv")

    assert [row["session_uuid"] for row in session_rows] == ["converted-day-one"]
    assert participant_rows[0]["attempt_id"] == f"attempt-{converted_attempt_id:08d}"
    assert participant_rows[0]["source_attempt_id"] == f"attempt-{source_attempt_id:08d}"
    assert participant_rows[0]["export_role"] == "converted_short"
    assert participant_rows[0]["export_day_scope"] == "day_1_only"
    assert session_rows[0]["attempt_id"] == f"attempt-{converted_attempt_id:08d}"
    assert session_rows[0]["source_attempt_id"] == f"attempt-{source_attempt_id:08d}"
    assert session_rows[0]["export_role"] == "converted_short"
    assert session_rows[0]["export_day_scope"] == "day_1_only"


def test_export_archive_contains_expected_files_and_can_include_test_rows(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    formal_participant_id = _insert_participant(
        conn,
        name="Formal Participant",
        phone="13800138000",
        phone_hash="hash-formal",
        created_at="2026-07-02T09:00:00+08:00",
    )
    formal_day_id = _insert_participant_day(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=formal_participant_id,
        participant_day_id=formal_day_id,
        session_uuid="session-formal",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    test_participant_id = _insert_participant(
        conn,
        name="Test Participant",
        phone="13900139000",
        phone_hash="hash-test",
        created_at="2026-07-02T11:00:00+08:00",
    )
    test_day_id = _insert_participant_day(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=test_participant_id,
        participant_day_id=test_day_id,
        session_uuid="session-test",
        is_test=True,
        created_at="2026-07-02T12:00:00+08:00",
    )

    default_archive_path = tmp_path / "default.zip"
    create_v2_export(
        conn,
        sqlite_settings,
        default_archive_path,
    )

    _, default_api_call_log_rows = _read_csv_from_zip(
        default_archive_path,
        "api_call_logs.csv",
    )

    include_test_archive_path = tmp_path / "include-test.zip"
    result = create_v2_export(
        conn,
        sqlite_settings,
        include_test_archive_path,
        include_test=True,
    )

    with zipfile.ZipFile(include_test_archive_path) as archive:
        assert set(archive.namelist()) == {
            "participants.csv",
            "sessions.csv",
            "turns.csv",
            "ratings.csv",
            "artifacts.jsonl",
            "api_call_logs.csv",
            "pretest_responses.jsonl",
                "integrated.csv",
            "audio/participant-00000001_attempt-00000001_short_day_1_turn_1_session-00000001.wav",
            "audio/participant-00000002_attempt-00000002_short_day_1_turn_1_session-00000002.wav",
            "json/participant-00000001_attempt-00000001_short_day_1_turn_1_session-00000001.json",
            "json/participant-00000002_attempt-00000002_short_day_1_turn_1_session-00000002.json",
        }

    _, session_rows = _read_csv_from_zip(include_test_archive_path, "sessions.csv")
    _, api_call_log_rows = _read_csv_from_zip(
        include_test_archive_path,
        "api_call_logs.csv",
    )

    assert result.output_path == include_test_archive_path
    assert result.include_test is True
    assert [row["request_id"] for row in default_api_call_log_rows] == [
        "session-formal-turn-1",
    ]
    assert [row["request_id"] for row in api_call_log_rows] == [
        "session-formal-turn-1",
        "session-test-turn-1",
    ]
    assert [row["is_test"] for row in session_rows] == ["0", "1"]


def test_clean_data_export_only_includes_eligible_non_test_participants(
    conn: sqlite3.Connection,
) -> None:
    def _current_attempt_id(participant_id: int) -> int:
        return int(
            conn.execute(
                "SELECT current_attempt_id FROM participants WHERE id = ?",
                (participant_id,),
            ).fetchone()["current_attempt_id"]
        )

    eligible_participant_id = _insert_participant(
        conn,
        name="Eligible Participant",
        phone="13800138000",
        phone_hash="hash-eligible",
        created_at="2026-07-02T09:00:00+08:00",
    )
    eligible_day_id = _insert_participant_day(
        conn,
        participant_id=eligible_participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=eligible_participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=eligible_participant_id,
        participant_day_id=eligible_day_id,
        session_uuid="session-eligible",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (eligible_participant_id, _current_attempt_id(eligible_participant_id)),
    )

    excluded_participant_id = _insert_participant(
        conn,
        name="Excluded Participant",
        phone="13900139000",
        phone_hash="hash-excluded",
        created_at="2026-07-02T11:00:00+08:00",
    )
    excluded_day_id = _insert_participant_day(
        conn,
        participant_id=excluded_participant_id,
        created_at="2026-07-02T11:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=excluded_participant_id,
        created_at="2026-07-02T11:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=excluded_participant_id,
        participant_day_id=excluded_day_id,
        session_uuid="session-excluded",
        is_test=False,
        created_at="2026-07-02T12:00:00+08:00",
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'excluded', ?)
        """,
        (
            excluded_participant_id,
            _current_attempt_id(excluded_participant_id),
            json.dumps(["api_failure"], ensure_ascii=False),
        ),
    )

    test_only_participant_id = _insert_participant(
        conn,
        name="Test Eligible Participant",
        phone="13700137000",
        phone_hash="hash-test-eligible",
        created_at="2026-07-02T13:00:00+08:00",
    )
    test_day_id = _insert_participant_day(
        conn,
        participant_id=test_only_participant_id,
        created_at="2026-07-02T13:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=test_only_participant_id,
        created_at="2026-07-02T13:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=test_only_participant_id,
        participant_day_id=test_day_id,
        session_uuid="session-test-only",
        is_test=True,
        created_at="2026-07-02T14:00:00+08:00",
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (test_only_participant_id, _current_attempt_id(test_only_participant_id)),
    )

    payload = build_clean_data_export_payload(conn)

    assert [row["participant_id"] for row in payload["participants.csv"]] == [
        f"participant-{eligible_participant_id:08d}"
    ]
    assert "phone_hash" not in payload["participants.csv"][0]
    assert [row["session_uuid"] for row in payload["sessions.csv"]] == ["session-eligible"]
    assert {row["participant_id"] for row in payload["pretest_responses.jsonl"]} == {
        f"participant-{eligible_participant_id:08d}"
    }


def test_clean_data_export_includes_interface_members_for_eligible_rows(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    participant_id = _insert_participant(
        conn,
        name="Eligible Interface",
        phone="13800138000",
        phone_hash="hash-eligible-interface",
        created_at="2026-07-02T09:00:00+08:00",
    )
    attempt_id = int(
        conn.execute(
            "SELECT current_attempt_id FROM participants WHERE id = ?",
            (participant_id,),
        ).fetchone()["current_attempt_id"]
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-clean-interface",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    source_audio_path = sqlite_settings.data_dir / "audio" / "session-clean-interface.wav"
    source_audio_path.parent.mkdir(parents=True, exist_ok=True)
    source_audio_path.write_bytes(b"clean interface audio")
    conn.execute(
        "UPDATE conversation_turns SET user_audio_sha256 = ? WHERE user_audio_path = ?",
        (
            hashlib.sha256(b"clean interface audio").hexdigest(),
            "audio/session-clean-interface.wav",
        ),
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (participant_id, attempt_id),
    )

    archive_path = tmp_path / "clean-interface.zip"
    create_clean_data_export(conn, sqlite_settings, archive_path)

    canonical_stem = (
        "participant-00000001_attempt-00000001_short_day_1_turn_1_session-00000001"
    )
    json_member = f"json/{canonical_stem}.json"
    audio_member = f"audio/{canonical_stem}.wav"
    with zipfile.ZipFile(archive_path) as archive:
        member_names = set(archive.namelist())
        assert json_member in member_names
        assert audio_member in member_names
        assert "integrated.csv" in member_names
        assert archive.read(audio_member) == b"clean interface audio"

    _, integrated_rows = _read_csv_from_zip(archive_path, "integrated.csv")
    assert integrated_rows[0]["participant_id"] == "participant-00000001"
    assert integrated_rows[0]["attempt_id"] == "attempt-00000001"
    assert integrated_rows[0]["json_file"] == json_member
    assert integrated_rows[0]["audio_file"] == audio_member


def test_clean_data_export_fails_explicitly_when_expected_audio_is_missing(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _, source_audio_path = _insert_eligible_clean_export_candidate(
        conn,
        session_uuid="session-missing-export-audio",
    )
    source_audio_path.unlink()
    archive_path = tmp_path / "missing-audio.zip"

    with pytest.raises(RuntimeError, match="audio_missing"):
        create_clean_data_export(
            conn,
            sqlite_settings,
            archive_path,
        )

    assert not archive_path.exists()
    assert list(tmp_path.glob(f".{archive_path.name}.*.tmp")) == []


def test_failed_export_overwrite_preserves_existing_successful_archive(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _, source_audio_path = _insert_eligible_clean_export_candidate(
        conn,
        session_uuid="session-preserve-successful-export",
    )
    archive_path = tmp_path / "preserved-success.zip"
    create_clean_data_export(conn, sqlite_settings, archive_path)
    successful_archive_bytes = archive_path.read_bytes()
    source_audio_path.unlink()

    with pytest.raises(RuntimeError, match="audio_missing"):
        create_clean_data_export(conn, sqlite_settings, archive_path)

    assert archive_path.read_bytes() == successful_archive_bytes
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None


def test_failed_export_removes_only_its_owned_temporary_archive(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    _, source_audio_path = _insert_eligible_clean_export_candidate(
        conn,
        session_uuid="session-owned-temp-cleanup",
    )
    source_audio_path.unlink()
    archive_path = tmp_path / "owned-temp-cleanup.zip"
    adjacent_temps = [
        tmp_path / f".{archive_path.name}.adjacent-one.tmp",
        tmp_path / f".{archive_path.name}.adjacent-two.tmp",
    ]
    for index, adjacent_temp in enumerate(adjacent_temps, start=1):
        adjacent_temp.write_bytes(f"other export {index}".encode())

    with pytest.raises(RuntimeError, match="audio_missing"):
        create_clean_data_export(conn, sqlite_settings, archive_path)

    assert [path.read_bytes() for path in adjacent_temps] == [
        b"other export 1",
        b"other export 2",
    ]
    assert not archive_path.exists()


def test_export_copies_validated_audio_bytes_without_reopening_swapped_path(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_audio_path = _insert_eligible_clean_export_candidate(
        conn,
        session_uuid="session-export-swap-race",
    )
    expected_audio = source_audio_path.read_bytes()
    outside_path = tmp_path / "outside-participant-audio.wav"
    outside_path.write_bytes(b"outside participant data")
    archive_path = tmp_path / "swap-race.zip"
    original_write = zipfile.ZipFile.write
    original_writestr = zipfile.ZipFile.writestr
    swapped = False

    def swap_source_once(member_name: str) -> None:
        nonlocal swapped
        if not swapped and member_name.startswith("audio/"):
            source_audio_path.unlink()
            source_audio_path.symlink_to(outside_path)
            swapped = True

    def racing_write(
        archive: zipfile.ZipFile,
        filename: str | Path,
        arcname: str | None = None,
        compress_type: int | None = None,
        compresslevel: int | None = None,
    ) -> None:
        swap_source_once(str(arcname or filename))
        original_write(
            archive,
            filename,
            arcname=arcname,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )

    def racing_writestr(
        archive: zipfile.ZipFile,
        zinfo_or_arcname: str | zipfile.ZipInfo,
        data: str | bytes,
        compress_type: int | None = None,
        compresslevel: int | None = None,
    ) -> None:
        member_name = (
            zinfo_or_arcname.filename
            if isinstance(zinfo_or_arcname, zipfile.ZipInfo)
            else str(zinfo_or_arcname)
        )
        swap_source_once(member_name)
        original_writestr(
            archive,
            zinfo_or_arcname,
            data,
            compress_type=compress_type,
            compresslevel=compresslevel,
        )

    monkeypatch.setattr(zipfile.ZipFile, "write", racing_write)
    monkeypatch.setattr(zipfile.ZipFile, "writestr", racing_writestr)

    create_clean_data_export(conn, sqlite_settings, archive_path)

    assert swapped is True
    with zipfile.ZipFile(archive_path) as archive:
        audio_members = [
            member_name
            for member_name in archive.namelist()
            if member_name.startswith("audio/")
        ]
        assert len(audio_members) == 1
        assert archive.read(audio_members[0]) == expected_audio


def test_failed_export_job_preserves_existing_successful_destination(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    _, source_audio_path = _insert_eligible_clean_export_candidate(
        conn,
        session_uuid="session-failed-export-job-audio",
    )
    source_audio_path.unlink()
    queued_job = create_export_job(
        conn,
        export_type="complete_no_external_error_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    output_dir = sqlite_settings.data_dir / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    expected_output = output_dir / (
        f"{queued_job['job_uuid']}_complete_no_external_error_data.zip"
    )
    with zipfile.ZipFile(expected_output, mode="w") as successful_archive:
        successful_archive.writestr("successful.txt", b"published export")
    successful_archive_bytes = expected_output.read_bytes()

    failed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert failed_job["status"] == "failed"
    assert failed_job["error_message"] == "audio_missing"
    assert failed_job["output_path"] is None
    assert expected_output.read_bytes() == successful_archive_bytes
    with zipfile.ZipFile(expected_output) as archive:
        assert archive.read("successful.txt") == b"published export"
    assert list(output_dir.glob(f".{expected_output.name}.*.tmp")) == []


def test_export_job_creates_queued_job(
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job

    job = create_export_job(
        conn,
        export_type="complete_no_external_error_data",
        filters={},
        include_test=False,
        created_by="admin",
    )

    assert job["status"] == "queued"
    assert job["export_type"] == "complete_no_external_error_data"
    assert job["output_path"] is None
    assert job["progress_message"] is None


def test_export_job_claim_is_atomic_across_duplicate_workers(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import claim_export_job, create_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    barrier = threading.Barrier(2)

    def claim(worker_id: str) -> dict[str, object] | None:
        worker_conn = get_connection(sqlite_settings)
        try:
            barrier.wait()
            return claim_export_job(
                worker_conn,
                settings=sqlite_settings,
                job_uuid=str(queued_job["job_uuid"]),
                worker_id=worker_id,
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim, ("worker-a", "worker-b")))

    owned_claims = [claim for claim in claims if claim is not None]
    assert len(owned_claims) == 1
    assert owned_claims[0]["lease_owner"] in {"worker-a", "worker-b"}
    assert owned_claims[0]["lease_token"]
    assert owned_claims[0]["attempt_count"] == 1


def test_stale_running_export_job_is_reclaimed_after_restart(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import claim_export_job, create_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    conn.execute(
        """
        UPDATE export_jobs
        SET status = 'running',
            lease_owner = 'dead-worker',
            lease_token = 'dead-token',
            lease_expires_at = '2000-01-01 00:00:00',
            heartbeat_at = '2000-01-01 00:00:00',
            attempt_count = 1
        WHERE job_uuid = ?
        """,
        (queued_job["job_uuid"],),
    )

    claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="restart-worker",
    )

    assert claim is not None
    assert claim["status"] == "running"
    assert claim["lease_owner"] == "restart-worker"
    assert claim["lease_token"] != "dead-token"
    assert claim["attempt_count"] == 2
    assert claim["failure_kind"] == "recoverable"


def test_stale_export_job_becomes_terminal_after_retry_exhaustion(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, get_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    conn.execute(
        """
        UPDATE export_jobs
        SET status = 'running',
            lease_owner = 'dead-worker',
            lease_token = 'dead-token',
            lease_expires_at = '2000-01-01 00:00:00',
            heartbeat_at = '2000-01-01 00:00:00',
            attempt_count = ?
        WHERE job_uuid = ?
        """,
        (export_jobs.EXPORT_JOB_MAX_ATTEMPTS, queued_job["job_uuid"]),
    )

    export_jobs.recover_stale_export_jobs(conn, settings=sqlite_settings)
    terminal_job = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))

    assert terminal_job["status"] == "failed"
    assert terminal_job["failure_kind"] == "terminal"
    assert terminal_job["error_message"] == "Export retry limit exhausted."
    assert terminal_job["completed_at"] is not None


def test_export_job_heartbeat_and_terminal_updates_require_current_lease_owner(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import (
        claim_export_job,
        create_export_job,
        finalize_export_job_failure,
        get_export_job,
        renew_export_job_lease,
    )

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    first_claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="first-worker",
    )
    assert first_claim is not None
    conn.execute(
        "UPDATE export_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE job_uuid = ?",
        (queued_job["job_uuid"],),
    )

    assert renew_export_job_lease(
        conn,
        job_uuid=str(queued_job["job_uuid"]),
        lease_token=str(first_claim["lease_token"]),
    ) is False
    assert finalize_export_job_failure(
        conn,
        job_uuid=str(queued_job["job_uuid"]),
        lease_token=str(first_claim["lease_token"]),
        error_message="stale worker failure",
    ) is False
    second_claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="new-worker",
    )
    assert second_claim is not None
    assert second_claim["lease_token"] != first_claim["lease_token"]
    assert finalize_export_job_failure(
        conn,
        job_uuid=str(queued_job["job_uuid"]),
        lease_token=str(first_claim["lease_token"]),
        error_message="stale worker failure",
    ) is False
    current_job = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))
    assert current_job["status"] == "running"
    assert current_job["lease_owner"] == "new-worker"
    assert current_job["error_message"] is None


def test_export_job_heartbeat_retries_transient_connection_failure(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import claim_export_job, create_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="heartbeat-worker",
    )
    assert claim is not None
    original_get_connection = export_jobs.get_connection
    connection_attempts = 0

    def transient_get_connection(settings: Settings):
        nonlocal connection_attempts
        connection_attempts += 1
        if connection_attempts == 1:
            raise sqlite3.OperationalError("temporary database unavailable")
        return original_get_connection(settings)

    monkeypatch.setattr(export_jobs, "EXPORT_JOB_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(export_jobs, "EXPORT_JOB_HEARTBEAT_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(export_jobs, "get_connection", transient_get_connection)
    heartbeat = export_jobs._ExportJobHeartbeat(
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        lease_token=str(claim["lease_token"]),
    )

    heartbeat.start()
    deadline = time.monotonic() + 1
    while connection_attempts < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    heartbeat.stop()

    assert connection_attempts >= 2
    assert heartbeat.ownership_lost is False


def test_stale_worker_cannot_publish_over_newer_owner_staging_or_canonical(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import claim_export_job, create_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    first_claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="stale-worker",
    )
    assert first_claim is not None
    first_staging = Path(str(first_claim["staging_path"]))
    first_staging.parent.mkdir(parents=True, exist_ok=True)
    first_staging.write_bytes(b"stale-worker-staging")
    conn.execute(
        "UPDATE export_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE job_uuid = ?",
        (queued_job["job_uuid"],),
    )
    second_claim = claim_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
        worker_id="new-worker",
    )
    assert second_claim is not None
    second_staging = Path(str(second_claim["staging_path"]))
    second_staging.write_bytes(b"new-worker-staging")
    canonical_path = Path(str(second_claim["canonical_path"]))
    canonical_path.write_bytes(b"new-worker-canonical")

    with pytest.raises(export_jobs.ExportJobOwnershipLost):
        export_jobs._publish_staging_archive(
            conn,
            job_uuid=str(queued_job["job_uuid"]),
            lease_token=str(first_claim["lease_token"]),
            staging_path=first_staging,
            canonical_path=canonical_path,
        )
    with pytest.raises(export_jobs.ExportJobOwnershipLost):
        with export_jobs._immediate_transaction(conn):
            export_jobs._mark_export_job_succeeded(
                conn,
                job_uuid=str(queued_job["job_uuid"]),
                lease_token=str(first_claim["lease_token"]),
                output_path=canonical_path,
            )

    current = conn.execute(
        "SELECT lease_token, status, staging_path FROM export_jobs WHERE job_uuid = ?",
        (queued_job["job_uuid"],),
    ).fetchone()
    assert current["lease_token"] == second_claim["lease_token"]
    assert current["status"] == "running"
    assert current["staging_path"] == str(second_staging)
    assert first_staging.read_bytes() == b"stale-worker-staging"
    assert second_staging.read_bytes() == b"new-worker-staging"
    assert canonical_path.read_bytes() == b"new-worker-canonical"


def test_crash_before_archive_publication_leaves_job_recoverable(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, get_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    original_publish = export_jobs._publish_staging_archive

    def crash_before_publish(*args, **kwargs) -> None:
        raise SystemExit("simulated worker crash")

    monkeypatch.setattr(export_jobs, "_publish_staging_archive", crash_before_publish)
    with pytest.raises(SystemExit, match="simulated worker crash"):
        run_export_job(
            conn,
            settings=sqlite_settings,
            job_uuid=str(queued_job["job_uuid"]),
        )

    crashed_job = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))
    assert crashed_job["status"] == "running"
    assert crashed_job["publication_state"] == "publishing"
    assert not Path(str(crashed_job["canonical_path"])).exists()

    monkeypatch.setattr(export_jobs, "_publish_staging_archive", original_publish)
    conn.execute(
        "UPDATE export_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE job_uuid = ?",
        (queued_job["job_uuid"],),
    )
    recovered_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert recovered_job["status"] == "succeeded"
    assert Path(str(recovered_job["output_path"])).is_file()


def test_crash_after_publication_before_commit_recovers_published_archive(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, get_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    original_mark_succeeded = export_jobs._mark_export_job_succeeded

    def crash_before_commit(*args, **kwargs) -> None:
        raise SystemExit("simulated post-publication crash")

    monkeypatch.setattr(export_jobs, "_mark_export_job_succeeded", crash_before_commit)
    with pytest.raises(SystemExit, match="simulated post-publication crash"):
        run_export_job(
            conn,
            settings=sqlite_settings,
            job_uuid=str(queued_job["job_uuid"]),
        )

    crashed_job = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))
    canonical_path = Path(str(crashed_job["canonical_path"]))
    assert crashed_job["status"] == "running"
    assert crashed_job["publication_state"] == "publishing"
    assert canonical_path.is_file()
    with zipfile.ZipFile(canonical_path) as archive:
        assert archive.testzip() is None

    monkeypatch.setattr(export_jobs, "_mark_export_job_succeeded", original_mark_succeeded)
    conn.execute(
        "UPDATE export_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE job_uuid = ?",
        (queued_job["job_uuid"],),
    )
    export_jobs.recover_stale_export_jobs(conn, settings=sqlite_settings)
    recovered_job = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))

    assert recovered_job["status"] == "succeeded"
    assert recovered_job["publication_state"] == "published"
    assert recovered_job["output_path"] == str(canonical_path)


def test_transient_database_failure_after_publication_reconciles_success(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    original_mark_succeeded = export_jobs._mark_export_job_succeeded
    attempts = 0
    success_connections: list[sqlite3.Connection] = []

    def fail_first_success_commit(*args, **kwargs) -> None:
        nonlocal attempts
        attempts += 1
        success_connections.append(args[0])
        if attempts == 1:
            raise sqlite3.OperationalError("simulated success write failure")
        original_mark_succeeded(*args, **kwargs)

    monkeypatch.setattr(
        export_jobs,
        "_mark_export_job_succeeded",
        fail_first_success_commit,
    )

    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert attempts == 2
    assert success_connections[0] is conn
    assert success_connections[1] is not conn
    assert completed_job["status"] == "succeeded"
    assert completed_job["publication_state"] == "published"
    assert Path(str(completed_job["output_path"])).is_file()


@pytest.mark.parametrize("persistent", [False, True])
def test_commit_failure_after_publication_rolls_back_and_reconciles_on_fresh_connection(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    persistent: bool,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    canonical_path = sqlite_settings.data_dir / "exports" / (
        f"{queued_job['job_uuid']}_experiment_data.zip"
    )
    failing_conn = _CommitFailingConnection(
        conn,
        fail_when=canonical_path.exists,
        persistent=persistent,
    )

    completed_job = run_export_job(
        failing_conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert failing_conn.commit_failures == 1
    assert failing_conn.rollback_calls == 1
    assert conn.in_transaction is False
    persistent_conn = get_connection(sqlite_settings)
    try:
        persisted = persistent_conn.execute(
            "SELECT status, publication_state, output_path FROM export_jobs WHERE job_uuid = ?",
            (queued_job["job_uuid"],),
        ).fetchone()
    finally:
        persistent_conn.close()
    assert completed_job["status"] == "succeeded"
    assert persisted["status"] == "succeeded"
    assert persisted["publication_state"] == "published"
    assert persisted["output_path"] == str(canonical_path)


def test_persistent_success_write_failure_leaves_published_archive_recoverable(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    success_connections: list[sqlite3.Connection] = []

    def persistent_write_failure(connection, *args, **kwargs) -> None:
        success_connections.append(connection)
        raise sqlite3.OperationalError("persistent success write failure")

    monkeypatch.setattr(
        export_jobs,
        "_mark_export_job_succeeded",
        persistent_write_failure,
    )

    result = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert conn.in_transaction is False
    assert success_connections
    assert any(connection is not conn for connection in success_connections)
    persistent_conn = get_connection(sqlite_settings)
    try:
        persisted = persistent_conn.execute(
            """
            SELECT status, publication_state, output_path, canonical_path, archive_sha256
            FROM export_jobs
            WHERE job_uuid = ?
            """,
            (queued_job["job_uuid"],),
        ).fetchone()
    finally:
        persistent_conn.close()
    assert result["status"] == "running"
    assert result["publication_state"] == "publishing"
    assert persisted["status"] == "running"
    assert persisted["publication_state"] == "publishing"
    assert persisted["output_path"] is None
    assert persisted["archive_sha256"]
    assert Path(str(persisted["canonical_path"])).is_file()


def test_lease_expiry_during_publication_cannot_mark_success(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import (
        create_export_job,
        get_export_job,
        recover_stale_export_jobs,
        run_export_job,
    )

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    original_mark_succeeded = export_jobs._mark_export_job_succeeded

    def wait_for_expiry_then_mark(
        connection: sqlite3.Connection,
        *,
        job_uuid: str,
        lease_token: str,
        output_path: Path,
    ) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            expired = connection.execute(
                """
                SELECT lease_expires_at <= CURRENT_TIMESTAMP
                FROM export_jobs
                WHERE job_uuid = ?
                """,
                (job_uuid,),
            ).fetchone()[0]
            if expired:
                break
            time.sleep(0.01)
        assert expired == 1
        original_mark_succeeded(
            connection,
            job_uuid=job_uuid,
            lease_token=lease_token,
            output_path=output_path,
        )

    monkeypatch.setattr(export_jobs, "EXPORT_JOB_LEASE_SECONDS", 1)
    monkeypatch.setattr(
        export_jobs,
        "_mark_export_job_succeeded",
        wait_for_expiry_then_mark,
    )

    result = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert result["status"] == "running"
    assert result["publication_state"] == "publishing"
    assert Path(str(result["canonical_path"])).is_file()
    persistent = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))
    assert persistent["status"] == "running"
    recover_stale_export_jobs(conn, settings=sqlite_settings)
    recovered = get_export_job(conn, job_uuid=str(queued_job["job_uuid"]))
    assert recovered["status"] == "succeeded"


def test_export_job_publishes_archive_before_recording_success(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.services import export_jobs
    from backend.app.services.export_jobs import create_export_job, run_export_job

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    original_mark_succeeded = export_jobs._mark_export_job_succeeded

    def assert_published_before_success(
        connection: sqlite3.Connection,
        *,
        job_uuid: str,
        lease_token: str,
        output_path: Path,
    ) -> None:
        assert output_path.is_file()
        with zipfile.ZipFile(output_path) as archive:
            assert archive.testzip() is None
        current_status = connection.execute(
            "SELECT status FROM export_jobs WHERE job_uuid = ?",
            (job_uuid,),
        ).fetchone()["status"]
        assert current_status == "running"
        original_mark_succeeded(
            connection,
            job_uuid=job_uuid,
            lease_token=lease_token,
            output_path=output_path,
        )

    monkeypatch.setattr(
        export_jobs,
        "_mark_export_job_succeeded",
        assert_published_before_success,
    )

    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "succeeded"


def test_admin_create_export_job_returns_queued_job_without_waiting_for_archive(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import main as app_main

    launched_jobs: list[str] = []

    def fake_run_export_job_background(*, settings: Settings, job_uuid: str) -> None:
        launched_jobs.append(job_uuid)

    monkeypatch.setattr(
        app_main,
        "run_export_job_background",
        fake_run_export_job_background,
    )

    client = TestClient(app_main.create_app(settings=sqlite_settings))
    with client:
        login_response = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "admin-pass-123"},
        )
        response = client.post(
            "/api/admin/export-jobs",
            json={
                "export_type": "experiment_data",
                "filters": {},
                "include_test": False,
            },
        )

    assert login_response.status_code == 200
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "queued"
    assert payload["progress_message"] is None
    assert payload["output_path"] is None
    assert launched_jobs == [payload["job_uuid"]]


def test_export_job_rejects_reimbursement_include_test(
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job

    with pytest.raises(ValueError, match="reimbursement exports do not support include_test"):
        create_export_job(
            conn,
            export_type="reimbursement",
            filters={},
            include_test=True,
            created_by="admin",
        )


def test_run_export_job_for_complete_no_external_error_data_creates_zip_in_exports_dir(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> None:
    from backend.app.services.export import build_clean_data_export_payload
    from backend.app.services.export_jobs import create_export_job, run_export_job

    participant_id = _insert_participant(
        conn,
        name="Clean Export Participant",
        phone="13800138000",
        phone_hash="hash-clean-export",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
        payload={
            "demographics": {
                "birthDate": "1990-01-01",
                "gender": "female",
                "idNumber": "ID-CLEAN-001",
            },
            "ai_familiarity": 4,
            "trust_expectation": 5,
            "usage_frequency": "weekly",
        },
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-clean-export",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'eligible', '[]')
        """,
        (
            participant_id,
            conn.execute(
                "SELECT current_attempt_id FROM participants WHERE id = ?",
                (participant_id,),
            ).fetchone()["current_attempt_id"],
        ),
    )

    queued_job = create_export_job(
        conn,
        export_type="complete_no_external_error_data",
        filters={},
        include_test=False,
        created_by="admin",
    )
    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "succeeded"
    assert completed_job["output_path"]
    output_path = Path(str(completed_job["output_path"]))
    assert output_path.exists()
    assert output_path.is_relative_to(tmp_path / "exports")
    participant_row = build_clean_data_export_payload(conn)["participants.csv"][0]
    assert participant_row["participant_id"] == f"participant-{participant_id:08d}"
    assert "phone_hash" not in participant_row


def test_run_export_job_applies_session_date_range_filters(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    inside_participant_id = _insert_participant(
        conn,
        name="Inside Range Participant",
        phone="13800138011",
        phone_hash="hash-inside-range",
        created_at="2026-07-02T09:00:00+08:00",
    )
    inside_day_id = _insert_participant_day(
        conn,
        participant_id=inside_participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=inside_participant_id,
        created_at="2026-07-02T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=inside_participant_id,
        participant_day_id=inside_day_id,
        session_uuid="session-inside-range",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    outside_participant_id = _insert_participant(
        conn,
        name="Outside Range Participant",
        phone="13800138012",
        phone_hash="hash-outside-range",
        created_at="2026-07-05T09:00:00+08:00",
    )
    outside_day_id = _insert_participant_day(
        conn,
        participant_id=outside_participant_id,
        created_at="2026-07-05T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=outside_participant_id,
        created_at="2026-07-05T09:05:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=outside_participant_id,
        participant_day_id=outside_day_id,
        session_uuid="session-outside-range",
        is_test=False,
        created_at="2026-07-05T10:00:00+08:00",
    )

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={"start_date": "2026-07-02", "end_date": "2026-07-02"},
        include_test=False,
        created_by="admin",
    )
    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "succeeded"
    output_path = Path(str(completed_job["output_path"]))
    _, session_rows = _read_csv_from_zip(output_path, "sessions.csv")
    _, turn_rows = _read_csv_from_zip(output_path, "turns.csv")

    assert [row["session_uuid"] for row in session_rows] == ["session-inside-range"]
    assert [row["user_text"] for row in turn_rows] == ["user-session-inside-range"]


def test_run_export_job_applies_inclusive_shanghai_calendar_date_filters(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    session_timestamps = {
        "before-local-midnight": "2026-07-01T15:59:59Z",
        "local-midnight": "2026-07-01T16:00:00Z",
        "local-0759": "2026-07-01T23:59:00Z",
        "local-0800": "2026-07-02T00:00:00+00:00",
        "before-next-local-midnight": "2026-07-02T15:59:59Z",
        "next-local-midnight": "2026-07-02T16:00:00Z",
        "legacy-naive-utc-midnight": "2026-07-01 16:00:00",
    }
    for index, (session_uuid, created_at) in enumerate(
        session_timestamps.items(),
        start=20,
    ):
        participant_id = _insert_participant(
            conn,
            name=f"Shanghai Boundary {index}",
            phone=f"13800138{index:03d}",
            phone_hash=f"hash-shanghai-boundary-{index}",
            created_at=created_at,
        )
        participant_day_id = _insert_participant_day(
            conn,
            participant_id=participant_id,
            created_at=created_at,
        )
        _insert_session_bundle(
            conn,
            participant_id=participant_id,
            participant_day_id=participant_day_id,
            session_uuid=session_uuid,
            is_test=False,
            created_at=created_at,
        )

    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={"start_date": "2026-07-02", "end_date": "2026-07-02"},
        include_test=False,
        created_by="admin",
    )
    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "succeeded"
    output_path = Path(str(completed_job["output_path"]))
    _, session_rows = _read_csv_from_zip(output_path, "sessions.csv")
    assert {row["session_uuid"] for row in session_rows} == {
        "local-midnight",
        "local-0759",
        "local-0800",
        "before-next-local-midnight",
        "legacy-naive-utc-midnight",
    }


@pytest.mark.parametrize(
    ("created_at", "session_uuid"),
    [
        ("not-a-timestamp", "malformed-session-timestamp"),
        ("2026-07-02", "date-only-session-timestamp"),
    ],
)
def test_run_export_job_rejects_malformed_session_timestamp_in_date_filter(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
    created_at: str,
    session_uuid: str,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    participant_id = _insert_participant(
        conn,
        name="Malformed Timestamp Participant",
        phone="13800138990",
        phone_hash="hash-malformed-timestamp",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid=session_uuid,
        is_test=False,
        created_at=created_at,
    )
    queued_job = create_export_job(
        conn,
        export_type="experiment_data",
        filters={"start_date": "2026-07-02", "end_date": "2026-07-02"},
        include_test=False,
        created_by="admin",
    )

    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "failed"
    assert completed_job["error_message"] == (
        f"Session {session_uuid} has an invalid created_at timestamp."
    )


def test_run_export_job_ignores_stored_reimbursement_include_test_flag(
    sqlite_settings: Settings,
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export_jobs import create_export_job, run_export_job

    participant_id = _insert_participant(
        conn,
        name="Stored Flag Reimbursement Participant",
        phone="13800138002",
        phone_hash="hash-stored-flag-reimbursement",
        created_at="2026-07-02T09:00:00+08:00",
    )
    participant_day_id = _insert_participant_day(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=participant_id,
        created_at="2026-07-02T09:05:00+08:00",
        payload={
            "demographics": {
                "birthDate": "1990-01-01",
                "gender": "female",
                "idNumber": "ID-STORED-001",
            },
            "ai_familiarity": 4,
            "trust_expectation": 5,
            "usage_frequency": "weekly",
        },
    )
    _insert_session_bundle(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        session_uuid="session-stored-flag-reimbursement",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    queued_job = create_export_job(
        conn,
        export_type="reimbursement",
        filters={},
        include_test=False,
        created_by="admin",
    )
    conn.execute(
        """
        UPDATE export_jobs
        SET include_test = 1
        WHERE job_uuid = ?
        """,
        (str(queued_job["job_uuid"]),),
    )
    conn.commit()

    completed_job = run_export_job(
        conn,
        settings=sqlite_settings,
        job_uuid=str(queued_job["job_uuid"]),
    )

    assert completed_job["status"] == "succeeded"
    assert completed_job["include_test"] is False
    assert completed_job["output_path"]
    assert Path(str(completed_job["output_path"])).exists()


def test_reimbursement_export_excludes_test_data_and_extracts_id_number(
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export import build_reimbursement_export_rows

    formal_participant_id = _insert_participant(
        conn,
        name="Formal Reimbursement Participant",
        phone="13800138001",
        phone_hash="hash-formal-reimbursement",
        created_at="2026-07-02T09:00:00+08:00",
    )
    formal_day_id = _insert_participant_day(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=formal_participant_id,
        created_at="2026-07-02T09:05:00+08:00",
        payload={
            "demographics": {
                "birthDate": "1990-01-01",
                "gender": "female",
                "idNumber": "ID-REIMB-001",
            },
            "ai_familiarity": 4,
            "trust_expectation": 5,
            "usage_frequency": "weekly",
        },
    )
    _insert_session_bundle(
        conn,
        participant_id=formal_participant_id,
        participant_day_id=formal_day_id,
        session_uuid="session-formal-reimbursement",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    test_participant_id = _insert_participant(
        conn,
        name="__test_channel__",
        phone="00000000000",
        phone_hash="test-channel",
        created_at="2026-07-02T11:00:00+08:00",
    )
    test_day_id = _insert_participant_day(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=test_participant_id,
        created_at="2026-07-02T11:05:00+08:00",
        payload={
            "demographics": {
                "birthDate": "1990-01-01",
                "gender": "female",
                "idNumber": "ID-TEST-001",
            },
            "ai_familiarity": 4,
            "trust_expectation": 5,
            "usage_frequency": "weekly",
        },
    )
    _insert_session_bundle(
        conn,
        participant_id=test_participant_id,
        participant_day_id=test_day_id,
        session_uuid="session-test-reimbursement",
        is_test=True,
        created_at="2026-07-02T12:00:00+08:00",
    )

    rows = build_reimbursement_export_rows(conn)

    assert rows
    assert any(row["name"] == "Formal Reimbursement Participant" for row in rows)
    assert not any(row["name"] == "__test_channel__" for row in rows)
    formal_row = next(row for row in rows if row["name"] == "Formal Reimbursement Participant")
    assert set(formal_row) == {
        "name",
        "phone",
        "id_number",
        "target_days",
        "completed_days",
    }
    assert formal_row["target_days"] == 1
    assert formal_row["completed_days"] == 1
    assert formal_row["id_number"] == "ID-REIMB-001"


def test_reimbursement_export_counts_completed_days_by_effective_attempt_scope(
    conn: sqlite3.Connection,
) -> None:
    from backend.app.services.export import build_reimbursement_export_rows

    short_participant_id = _insert_participant(
        conn,
        name="Short Reimbursement Participant",
        phone="13800138011",
        phone_hash="hash-short-reimbursement",
        created_at="2026-07-02T09:00:00+08:00",
        participant_type="short",
    )
    short_day_id = _insert_participant_day(
        conn,
        participant_id=short_participant_id,
        day_index=1,
        created_at="2026-07-02T09:00:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=short_participant_id,
        day_index=1,
        created_at="2026-07-02T09:05:00+08:00",
        payload={"demographics": {"idNumber": "ID-SHORT-001"}},
    )
    _insert_session_bundle(
        conn,
        participant_id=short_participant_id,
        participant_day_id=short_day_id,
        session_uuid="session-short-reimbursement",
        is_test=False,
        created_at="2026-07-02T10:00:00+08:00",
    )

    long_participant_id = _insert_participant(
        conn,
        name="Long Reimbursement Participant",
        phone="13800138012",
        phone_hash="hash-long-reimbursement",
        created_at="2026-07-02T09:10:00+08:00",
        participant_type="long",
        target_days=3,
    )
    for day_index in (1, 2, 3):
        day_id = _insert_participant_day(
            conn,
            participant_id=long_participant_id,
            day_index=day_index,
            created_at=f"2026-07-0{day_index + 1}T09:10:00+08:00",
        )
        _insert_session_bundle(
            conn,
            participant_id=long_participant_id,
            participant_day_id=day_id,
            session_uuid=f"session-long-reimbursement-day-{day_index}",
            is_test=False,
            created_at=f"2026-07-0{day_index + 1}T10:10:00+08:00",
        )
    _insert_pretest_response(
        conn,
        participant_id=long_participant_id,
        day_index=1,
        created_at="2026-07-02T09:15:00+08:00",
        payload={"demographics": {"idNumber": "ID-LONG-001"}},
    )

    converted_participant_id = _insert_participant(
        conn,
        name="Converted Reimbursement Participant",
        phone="13800138013",
        phone_hash="hash-converted-reimbursement",
        created_at="2026-07-02T09:20:00+08:00",
        participant_type="long",
        target_days=3,
    )
    source_attempt_id = int(
        conn.execute(
            """
            UPDATE participant_attempts
            SET status = 'converted_to_short', valid_for_export = 0, export_role = 'normal_long'
            WHERE participant_id = ?
            RETURNING id
            """,
            (converted_participant_id,),
        ).fetchone()["id"]
    )
    converted_attempt_id = create_attempt(
        conn,
        participant_id=converted_participant_id,
        participant_type="short",
        condition="human",
        subcondition="qa",
        topic_key="topic-qa",
        error_type_id="factual_minor",
        target_days=1,
        status="completed",
        valid_for_export=True,
        source_attempt_id=source_attempt_id,
        export_role="converted_short",
    )
    conn.execute(
        "UPDATE participants SET current_attempt_id = ? WHERE id = ?",
        (converted_attempt_id, converted_participant_id),
    )
    converted_day_one_id = _insert_participant_day(
        conn,
        participant_id=converted_participant_id,
        attempt_id=source_attempt_id,
        day_index=1,
        created_at="2026-07-02T09:20:00+08:00",
    )
    converted_day_two_id = _insert_participant_day(
        conn,
        participant_id=converted_participant_id,
        attempt_id=source_attempt_id,
        day_index=2,
        created_at="2026-07-03T09:20:00+08:00",
    )
    _insert_pretest_response(
        conn,
        participant_id=converted_participant_id,
        attempt_id=source_attempt_id,
        day_index=1,
        created_at="2026-07-02T09:25:00+08:00",
        payload={"demographics": {"idNumber": "ID-CONVERTED-001"}},
    )
    _insert_session_bundle(
        conn,
        participant_id=converted_participant_id,
        attempt_id=source_attempt_id,
        participant_day_id=converted_day_one_id,
        session_uuid="session-converted-reimbursement-day-1",
        is_test=False,
        created_at="2026-07-02T10:20:00+08:00",
    )
    _insert_session_bundle(
        conn,
        participant_id=converted_participant_id,
        attempt_id=source_attempt_id,
        participant_day_id=converted_day_two_id,
        session_uuid="session-converted-reimbursement-day-2",
        is_test=False,
        created_at="2026-07-03T10:20:00+08:00",
    )

    rows = build_reimbursement_export_rows(conn)
    rows_by_name = {row["name"]: row for row in rows}

    assert rows_by_name["Short Reimbursement Participant"]["target_days"] == 1
    assert rows_by_name["Short Reimbursement Participant"]["completed_days"] == 1
    assert rows_by_name["Long Reimbursement Participant"]["target_days"] == 3
    assert rows_by_name["Long Reimbursement Participant"]["completed_days"] == 3
    assert rows_by_name["Converted Reimbursement Participant"]["target_days"] == 1
    assert rows_by_name["Converted Reimbursement Participant"]["completed_days"] == 1


def test_export_records_reimbursement_rejects_include_test(monkeypatch, capsys):
    import sys

    from scripts import export_records

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "export_records.py",
            "--export-type",
            "reimbursement",
            "--include-test",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        export_records.parse_args()

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert "reimbursement exports do not support --include-test." in captured.err
