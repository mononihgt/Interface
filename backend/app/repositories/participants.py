from __future__ import annotations

from datetime import date, timedelta
import sqlite3


LEGACY_COMPAT_PARTICIPANT_TYPE = "short"
LEGACY_COMPAT_CONDITION = "human"
LEGACY_COMPAT_SUBCONDITION = "qa"
LEGACY_COMPAT_TOPIC_KEY = "advice"
LEGACY_COMPAT_ERROR_TYPE_ID = "factual_minor"
LEGACY_COMPAT_TARGET_DAYS = 1
LEGACY_COMPAT_CURRENT_STATUS = "active"


def get_participant_by_name_phone(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM participants
        WHERE name = ? AND phone = ?
        """,
        (name, phone),
    ).fetchone()


def get_participant_by_id(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM participants
        WHERE id = ?
        """,
        (participant_id,),
    ).fetchone()


def insert_participant(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    phone_hash: str,
    participant_type: str,
    condition: str,
    subcondition: str,
    topic_key: str,
    error_type_id: str,
    target_days: int,
) -> int:
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
            current_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            phone,
            phone_hash,
            participant_type,
            condition,
            subcondition,
            topic_key,
            error_type_id,
            target_days,
            "active",
        ),
    )
    return int(cursor.lastrowid)


def insert_participant_identity(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    phone_hash: str,
) -> int:
    """Insert identity-only participants.

    The participants table still requires legacy assignment columns. These
    values are compatibility placeholders only; the source of truth for attempt-
    scoped assignment now lives in participant_attempts.
    """
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
            current_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            phone,
            phone_hash,
            LEGACY_COMPAT_PARTICIPANT_TYPE,
            LEGACY_COMPAT_CONDITION,
            LEGACY_COMPAT_SUBCONDITION,
            LEGACY_COMPAT_TOPIC_KEY,
            LEGACY_COMPAT_ERROR_TYPE_ID,
            LEGACY_COMPAT_TARGET_DAYS,
            LEGACY_COMPAT_CURRENT_STATUS,
        ),
    )
    return int(cursor.lastrowid)


def create_participant_days(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    target_days: int,
    start_date: date,
    attempt_id: int | None = None,
) -> None:
    for offset in range(target_days):
        calendar_date = (start_date + timedelta(days=offset)).isoformat()
        conn.execute(
            """
            INSERT INTO participant_days (
                participant_id,
                day_index,
                calendar_date,
                status,
                attempt_id
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                participant_id,
                offset + 1,
                calendar_date,
                "not_started",
                attempt_id,
            ),
        )


def set_attempt_id_for_participant_days(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
) -> None:
    conn.execute(
        """
        UPDATE participant_days
        SET attempt_id = ?, updated_at = CURRENT_TIMESTAMP
        WHERE participant_id = ? AND attempt_id IS NULL
        """,
        (attempt_id, participant_id),
    )


def get_participant_days(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int | None = None,
) -> list[sqlite3.Row]:
    params: list[object] = [participant_id]
    attempt_filter = ""
    if attempt_id is not None:
        attempt_filter = "AND attempt_id = ?"
        params.append(attempt_id)

    return conn.execute(
        f"""
        SELECT *
        FROM participant_days
        WHERE participant_id = ?
        {attempt_filter}
        ORDER BY day_index, id
        """,
        tuple(params),
    ).fetchall()


def get_participant_day_for_date(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    calendar_date: str,
    attempt_id: int | None = None,
) -> sqlite3.Row | None:
    params: list[object] = [participant_id, calendar_date]
    attempt_filter = ""
    if attempt_id is not None:
        attempt_filter = "AND attempt_id = ?"
        params.append(attempt_id)

    return conn.execute(
        f"""
        SELECT *
        FROM participant_days
        WHERE participant_id = ? AND calendar_date = ?
        {attempt_filter}
        ORDER BY day_index, id
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()


def get_participant_day_by_index(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM participant_days
        WHERE participant_id = ? AND day_index = ?
        """,
        (participant_id, day_index),
    ).fetchone()


def update_participant_day_status(
    conn: sqlite3.Connection,
    *,
    participant_day_id: int,
    status: str,
    started_at: str | None = None,
    completed_at: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE participant_days
        SET
            status = ?,
            started_at = COALESCE(started_at, ?),
            completed_at = COALESCE(?, completed_at),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, started_at, completed_at, participant_day_id),
    )
