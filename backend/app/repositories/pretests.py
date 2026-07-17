from __future__ import annotations

import sqlite3


def get_latest_final_pretest_for_participant(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    source_attempt_id: int | None = None,
) -> sqlite3.Row | None:
    params: list[object] = [participant_id]
    attempt_filter = ""
    if source_attempt_id is not None:
        attempt_filter = "AND attempt_id = ?"
        params.append(source_attempt_id)

    return conn.execute(
        f"""
        SELECT *
        FROM pretest_responses
        WHERE participant_id = ?
          AND day_index = 1
          AND status = 'final'
          {attempt_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def copy_latest_final_pretest_to_attempt(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    source_attempt_id: int | None,
    target_attempt_id: int,
) -> int | None:
    source = get_latest_final_pretest_for_participant(
        conn,
        participant_id=participant_id,
        source_attempt_id=source_attempt_id,
    )
    if source is None and source_attempt_id is not None:
        source = get_latest_final_pretest_for_participant(
            conn,
            participant_id=participant_id,
            source_attempt_id=None,
        )
    if source is None:
        return None

    cursor = conn.execute(
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
            source_pretest_response_id
        ) VALUES (?, ?, 1, 'final', ?, 0, ?, ?, ?)
        """,
        (
            participant_id,
            target_attempt_id,
            source["payload_json"],
            source["last_saved_at"],
            source["submitted_at"],
            int(source["id"]),
        ),
    )
    return int(cursor.lastrowid)


def get_latest_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    status: str | None = None,
    attempt_id: int | None = None,
) -> sqlite3.Row | None:
    params: list[object] = [participant_id, day_index]
    status_filter = ""
    attempt_filter = ""
    if status is not None:
        status_filter = "AND status = ?"
        params.append(status)
    if attempt_id is not None:
        attempt_filter = "AND attempt_id = ?"
        params.append(attempt_id)

    return conn.execute(
        f"""
        SELECT *
        FROM pretest_responses
        WHERE participant_id = ? AND day_index = ?
        {status_filter}
        {attempt_filter}
        ORDER BY id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def upsert_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    attempt_id: int | None = None,
    status: str,
    payload_json: str,
    autosave_count: int,
    last_saved_at: str,
    submitted_at: str | None,
) -> int:
    existing = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status=status,
        attempt_id=attempt_id,
    )

    if existing is None:
        cursor = conn.execute(
            """
            INSERT INTO pretest_responses (
                participant_id,
                day_index,
                attempt_id,
                status,
                payload_json,
                autosave_count,
                last_saved_at,
                submitted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                participant_id,
                day_index,
                attempt_id,
                status,
                payload_json,
                autosave_count,
                last_saved_at,
                submitted_at,
            ),
        )
        return int(cursor.lastrowid)

    conn.execute(
        """
        UPDATE pretest_responses
        SET
            attempt_id = ?,
            payload_json = ?,
            autosave_count = ?,
            last_saved_at = ?,
            submitted_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            attempt_id,
            payload_json,
            autosave_count,
            last_saved_at,
            submitted_at,
            existing["id"],
        ),
    )
    return int(existing["id"])


def finalize_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    attempt_id: int,
    payload_json: str,
    autosave_count: int,
    saved_at: str,
) -> int:
    existing_draft = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="draft",
        attempt_id=attempt_id,
    )
    if existing_draft is None:
        return upsert_pretest_response(
            conn,
            participant_id=participant_id,
            day_index=day_index,
            attempt_id=attempt_id,
            status="final",
            payload_json=payload_json,
            autosave_count=autosave_count,
            last_saved_at=saved_at,
            submitted_at=saved_at,
        )

    conn.execute(
        """
        UPDATE pretest_responses
        SET
            status = 'final',
            payload_json = ?,
            autosave_count = ?,
            last_saved_at = ?,
            submitted_at = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (
            payload_json,
            autosave_count,
            saved_at,
            saved_at,
            existing_draft["id"],
        ),
    )
    return int(existing_draft["id"])
