from __future__ import annotations

from dataclasses import dataclass
import random
import sqlite3

from backend.app.models.domain import (
    CONDITIONS,
    ERROR_TYPE_IDS,
    PARTICIPANT_TYPES,
    SUBCONDITIONS,
    TOPIC_KEYS_BY_CELL,
)


CELL_ORDER = [
    ("human", "qa"),
    ("human", "planning"),
    ("human", "chat"),
    ("human", "decision"),
    ("human", "execution"),
    ("tool", "qa"),
    ("tool", "planning"),
    ("tool", "chat"),
    ("tool", "decision"),
    ("tool", "execution"),
]
INTERNAL_TEST_PHONE_HASH = "test-channel"
COUNTED_ATTEMPT_STATUSES = ("active", "completed", "blocked", "converted_to_short")
COUNTED_ATTEMPT_STATUS_PLACEHOLDERS = ", ".join(
    "?" for _ in COUNTED_ATTEMPT_STATUSES
)


@dataclass(frozen=True)
class Assignment:
    participant_type: str
    condition: str
    subcondition: str
    topic_key: str
    error_type_id: str
    target_days: int


def _control_enabled(value: object) -> bool:
    if value is None:
        return True
    return bool(int(value))


def _control_cap(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def choose_topic_for_cell(
    conn: sqlite3.Connection,
    *,
    condition: str,
    subcondition: str,
    chooser=random.choice,
) -> str:
    topic_keys = TOPIC_KEYS_BY_CELL[(condition, subcondition)]
    counts = {
        row["topic_key"]: int(row["participant_count"])
        for row in conn.execute(
            f"""
            SELECT pa.topic_key, COUNT(*) AS participant_count
            FROM participant_attempts pa
            JOIN participants p ON p.id = pa.participant_id
            WHERE
                pa.condition = ?
                AND pa.subcondition = ?
                AND pa.valid_for_export = 1
                AND pa.status IN ({COUNTED_ATTEMPT_STATUS_PLACEHOLDERS})
                AND p.phone_hash != ?
            GROUP BY pa.topic_key
            """,
            (
                condition,
                subcondition,
                *COUNTED_ATTEMPT_STATUSES,
                INTERNAL_TEST_PHONE_HASH,
            ),
        ).fetchall()
    }
    minimum_count = min(counts.get(topic_key, 0) for topic_key in topic_keys)
    least_populated_topics = [
        topic_key
        for topic_key in topic_keys
        if counts.get(topic_key, 0) == minimum_count
    ]
    return chooser(least_populated_topics)


def _available_assignment_units(
    conn: sqlite3.Connection,
    *,
    participant_type_filter: str | None = None,
) -> list[dict[str, object]]:
    counts = {
        (
            row["participant_type"],
            row["condition"],
            row["subcondition"],
            row["error_type_id"],
        ): int(row["participant_count"])
        for row in conn.execute(
            f"""
            SELECT
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.error_type_id,
                COUNT(*) AS participant_count
            FROM participant_attempts pa
            JOIN participants p ON p.id = pa.participant_id
            WHERE
                pa.valid_for_export = 1
                AND pa.status IN ({COUNTED_ATTEMPT_STATUS_PLACEHOLDERS})
                AND p.phone_hash != ?
            GROUP BY
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.error_type_id
            """,
            (*COUNTED_ATTEMPT_STATUSES, INTERNAL_TEST_PHONE_HASH),
        ).fetchall()
    }
    configured_units = {
        (
            row["participant_type"],
            row["condition"],
            row["subcondition"],
            row["error_type_id"],
        ): row
        for row in conn.execute(
            """
            SELECT participant_type, condition, subcondition, error_type_id, cap, enabled
            FROM admin_assignment_units
            """
        ).fetchall()
    }

    available_units: list[dict[str, object]] = []
    participant_types = (
        (participant_type_filter,)
        if participant_type_filter is not None
        else PARTICIPANT_TYPES
    )
    for participant_type in participant_types:
        if participant_type not in PARTICIPANT_TYPES:
            raise ValueError("Unsupported participant_type.")
        for condition in CONDITIONS:
            for subcondition in SUBCONDITIONS:
                for error_type_id in ERROR_TYPE_IDS:
                    configured = configured_units.get(
                        (participant_type, condition, subcondition, error_type_id)
                    )
                    enabled = (
                        _control_enabled(configured["enabled"])
                        if configured is not None
                        else True
                    )
                    cap = (
                        _control_cap(configured["cap"])
                        if configured is not None
                        else None
                    )
                    count = counts.get(
                        (participant_type, condition, subcondition, error_type_id),
                        0,
                    )
                    if not enabled:
                        continue
                    if cap is not None and count >= cap:
                        continue
                    available_units.append(
                        {
                            "participant_type": participant_type,
                            "condition": condition,
                            "subcondition": subcondition,
                            "error_type_id": error_type_id,
                            "count": count,
                        }
                    )
    return available_units


def _select_assignment_unit(
    candidates: list[dict[str, object]],
    *,
    chooser=random.choice,
) -> dict[str, object]:
    if not candidates:
        raise ValueError("No assignment units available for new participants.")
    minimum_count = min(int(candidate["count"]) for candidate in candidates)
    least_populated_candidates = [
        candidate
        for candidate in candidates
        if int(candidate["count"]) == minimum_count
    ]
    return chooser(least_populated_candidates)


def _assignment_from_unit(
    conn: sqlite3.Connection,
    selected: dict[str, object],
) -> Assignment:
    topic_key = choose_topic_for_cell(
        conn,
        condition=str(selected["condition"]),
        subcondition=str(selected["subcondition"]),
    )
    return Assignment(
        participant_type=str(selected["participant_type"]),
        condition=str(selected["condition"]),
        subcondition=str(selected["subcondition"]),
        topic_key=topic_key,
        error_type_id=str(selected["error_type_id"]),
        target_days=1 if selected["participant_type"] == "short" else 3,
    )


def preview_assignment_for_participant_type(
    conn: sqlite3.Connection,
    *,
    participant_type: str,
) -> Assignment:
    candidates = _available_assignment_units(
        conn,
        participant_type_filter=participant_type,
    )
    return _assignment_from_unit(conn, _select_assignment_unit(candidates))


def assign_new_participant(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone_hash: str,
) -> Assignment:
    _ = (name, phone_hash)
    candidates = _available_assignment_units(conn)
    return _assignment_from_unit(conn, _select_assignment_unit(candidates))
