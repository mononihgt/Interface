from __future__ import annotations

import sqlite3


def get_external_operation(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    kind: str,
    turn_index: int,
    operation_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM external_operations
        WHERE participant_id = ?
          AND attempt_id IS ?
          AND session_id = ?
          AND kind = ?
          AND turn_index = ?
          AND operation_id = ?
        """,
        (participant_id, attempt_id, session_id, kind, turn_index, operation_id),
    ).fetchone()


def insert_external_operation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    request_fingerprint: str,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    kind: str,
    turn_index: int,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO external_operations (
            operation_id,
            request_fingerprint,
            participant_id,
            attempt_id,
            session_id,
            kind,
            turn_index,
            status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (
            operation_id,
            request_fingerprint,
            participant_id,
            attempt_id,
            session_id,
            kind,
            turn_index,
        ),
    )
    return int(cursor.lastrowid)


def mark_external_operation_succeeded(
    conn: sqlite3.Connection,
    *,
    operation_row_id: int,
    result_entity_id: int,
    result_json: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE external_operations
        SET status = 'succeeded',
            result_entity_id = ?,
            result_json = ?,
            error_json = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        """,
        (result_entity_id, result_json, operation_row_id),
    )


def mark_external_operation_failed(
    conn: sqlite3.Connection,
    *,
    operation_row_id: int,
    error_json: str,
) -> None:
    conn.execute(
        """
        UPDATE external_operations
        SET status = 'failed',
            error_json = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'pending'
        """,
        (error_json, operation_row_id),
    )


def release_pending_external_operation(
    conn: sqlite3.Connection,
    *,
    operation_row_id: int,
) -> None:
    conn.execute(
        "DELETE FROM external_operations WHERE id = ? AND status = 'pending'",
        (operation_row_id,),
    )
