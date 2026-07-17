from __future__ import annotations

import sqlite3


def insert_task_artifact(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
    artifact_type: str,
    status: str,
    payload_json: str,
    visible_to_participant: bool,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO task_artifacts (
            turn_id,
            artifact_type,
            status,
            payload_json,
            visible_to_participant
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            artifact_type,
            status,
            payload_json,
            int(visible_to_participant),
        ),
    )
    return int(cursor.lastrowid)


def insert_failed_task_artifact(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
    artifact_type: str,
    payload_json: str,
) -> int:
    return insert_task_artifact(
        conn,
        turn_id=turn_id,
        artifact_type=artifact_type,
        status="failed",
        payload_json=payload_json,
        visible_to_participant=False,
    )


def get_visible_artifact_for_turn(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM task_artifacts
        WHERE turn_id = ?
          AND status = 'completed'
          AND visible_to_participant = 1
        ORDER BY id DESC
        LIMIT 1
        """,
        (turn_id,),
    ).fetchone()


def get_latest_artifact_status_for_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT a.*
        FROM task_artifacts a
        JOIN conversation_turns t ON t.id = a.turn_id
        WHERE t.session_id = ?
        ORDER BY t.turn_index DESC, a.id DESC
        LIMIT 1
        """,
        (session_id,),
    ).fetchone()


def get_latest_visible_completed_artifact_for_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    artifact_type: str | None = None,
) -> sqlite3.Row | None:
    rows = list_visible_completed_artifacts_for_session(
        conn,
        session_id=session_id,
        artifact_type=artifact_type,
    )
    return rows[0] if rows else None


def list_visible_completed_artifacts_for_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    artifact_type: str | None = None,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT a.*
        FROM task_artifacts a
        JOIN conversation_turns t ON t.id = a.turn_id
        WHERE t.session_id = ?
          AND a.status = 'completed'
          AND a.visible_to_participant = 1
          AND (? IS NULL OR a.artifact_type = ?)
        ORDER BY t.turn_index DESC, a.id DESC
        """,
        (session_id, artifact_type, artifact_type),
    ).fetchall()


def list_recent_weather_agent_states(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT t.agent_state_json
        FROM conversation_turns t
        JOIN task_artifacts a ON a.turn_id = t.id
        WHERE t.session_id = ?
          AND a.artifact_type = 'weather_card'
          AND a.status = 'completed'
          AND a.visible_to_participant = 1
          AND t.agent_state_json IS NOT NULL
        ORDER BY t.turn_index DESC, a.id DESC
        """,
        (session_id,),
    ).fetchall()
    return [str(row["agent_state_json"]) for row in rows]
