from __future__ import annotations

import sqlite3


AttemptRow = sqlite3.Row


def _export_role(participant_type: str, source_attempt_id: int | None) -> str:
    if source_attempt_id is not None:
        return "converted_short"
    return "normal_long" if participant_type == "long" else "normal_short"


def create_attempt(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    participant_type: str,
    condition: str,
    subcondition: str,
    topic_key: str,
    error_type_id: str,
    target_days: int,
    status: str = "active",
    valid_for_export: bool = True,
    source_attempt_id: int | None = None,
    export_role: str | None = None,
    blocked_reason: str | None = None,
) -> int:
    next_attempt_no = int(
        conn.execute(
            """
            SELECT COALESCE(MAX(attempt_no), 0) + 1
            FROM participant_attempts
            WHERE participant_id = ?
            """,
            (participant_id,),
        ).fetchone()[0]
    )
    cursor = conn.execute(
        """
        INSERT INTO participant_attempts (
            participant_id,
            attempt_no,
            participant_type,
            condition,
            subcondition,
            topic_key,
            error_type_id,
            target_days,
            status,
            valid_for_export,
            source_attempt_id,
            export_role,
            blocked_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            participant_id,
            next_attempt_no,
            participant_type,
            condition,
            subcondition,
            topic_key,
            error_type_id,
            target_days,
            status,
            int(valid_for_export),
            source_attempt_id,
            export_role or _export_role(participant_type, source_attempt_id),
            blocked_reason,
        ),
    )
    return int(cursor.lastrowid)


def get_attempt_by_id(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
) -> AttemptRow | None:
    return conn.execute(
        "SELECT * FROM participant_attempts WHERE id = ?",
        (attempt_id,),
    ).fetchone()


def get_attempt_by_source_attempt_id(
    conn: sqlite3.Connection,
    *,
    source_attempt_id: int,
) -> AttemptRow | None:
    return conn.execute(
        """
        SELECT *
        FROM participant_attempts
        WHERE source_attempt_id = ?
        ORDER BY attempt_no DESC
        LIMIT 1
        """,
        (source_attempt_id,),
    ).fetchone()


def get_current_attempt(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
) -> AttemptRow | None:
    return conn.execute(
        """
        SELECT pa.*
        FROM participants p
        JOIN participant_attempts pa ON pa.id = p.current_attempt_id
        WHERE p.id = ?
        """,
        (participant_id,),
    ).fetchone()


def set_current_attempt(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
) -> None:
    conn.execute(
        """
        UPDATE participants
        SET current_attempt_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (attempt_id, participant_id),
    )


def update_attempt_status(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    status: str,
    valid_for_export: bool,
    blocked_reason: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE participant_attempts
        SET
            status = ?,
            valid_for_export = ?,
            blocked_reason = COALESCE(?, blocked_reason),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, int(valid_for_export), blocked_reason, attempt_id),
    )


def list_attempts_for_participant(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
) -> list[AttemptRow]:
    return conn.execute(
        """
        SELECT *
        FROM participant_attempts
        WHERE participant_id = ?
        ORDER BY attempt_no
        """,
        (participant_id,),
    ).fetchall()
