from __future__ import annotations

import sqlite3


def insert_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    participant_day_id: int,
    attempt_id: int | None,
    session_uuid: str,
    condition: str,
    subcondition: str,
    topic_key: str,
    scenario_id: str,
    agent_graph_version: str,
    error_type_id: str,
    planned_error_turn: int,
    status: str,
    started_at: str,
    client_info_json: str,
    is_test: bool,
) -> int:
    cursor = conn.execute(
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
            manipulation_status,
            started_at,
            client_info_json,
            is_test
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        """,
        (
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
            client_info_json,
            int(is_test),
        ),
    )
    return int(cursor.lastrowid)


def update_manipulation_status(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    manipulation_status: str,
) -> None:
    if manipulation_status not in {"presented", "failed"}:
        raise ValueError("invalid_terminal_manipulation_status")
    conn.execute(
        """
        UPDATE experiment_sessions
        SET manipulation_status = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (manipulation_status, session_id),
    )


def get_session_by_uuid(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*, d.day_index, d.status AS participant_day_status
        FROM experiment_sessions s
        JOIN participant_days d ON d.id = s.participant_day_id
        WHERE s.session_uuid = ?
        """,
        (session_uuid,),
    ).fetchone()


def get_session_by_uuid_for_participant(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
    participant_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*, d.day_index, d.status AS participant_day_status
        FROM experiment_sessions s
        JOIN participant_days d ON d.id = s.participant_day_id
        WHERE s.session_uuid = ? AND s.participant_id = ?
        """,
        (session_uuid, participant_id),
    ).fetchone()


def get_session_by_uuid_for_participant_attempt(
    conn: sqlite3.Connection,
    *,
    session_uuid: str,
    participant_id: int,
    attempt_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*, d.day_index, d.status AS participant_day_status
        FROM experiment_sessions s
        JOIN participant_days d ON d.id = s.participant_day_id
        WHERE s.session_uuid = ? AND s.participant_id = ? AND s.attempt_id = ?
        """,
        (session_uuid, participant_id, attempt_id),
    ).fetchone()


def get_latest_session_for_participant_day(
    conn: sqlite3.Connection,
    *,
    participant_day_id: int,
    is_test: bool,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT s.*, d.day_index, d.status AS participant_day_status
        FROM experiment_sessions s
        JOIN participant_days d ON d.id = s.participant_day_id
        WHERE s.participant_day_id = ? AND s.is_test = ?
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (participant_day_id, int(is_test)),
    ).fetchone()


def update_session_status(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    status: str,
    completed_at: str | None,
) -> None:
    conn.execute(
        """
        UPDATE experiment_sessions
        SET
            status = ?,
            completed_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, completed_at, session_id),
    )


def list_incomplete_formal_sessions_for_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM experiment_sessions
        WHERE attempt_id = ?
          AND is_test = 0
          AND status != 'completed'
        ORDER BY id
        """,
        (attempt_id,),
    ).fetchall()


def delete_sessions_by_ids(
    conn: sqlite3.Connection,
    *,
    session_ids: list[int],
) -> None:
    if not session_ids:
        return

    placeholders = ",".join("?" for _ in session_ids)
    conn.execute(
        f"DELETE FROM experiment_sessions WHERE id IN ({placeholders})",
        tuple(session_ids),
    )
