from pathlib import Path
from random import Random

import pytest

from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "assignment.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )


def test_assignment_randomizes_ties_while_preserving_least_populated_balance():
    from backend.app.models.domain import (
        CONDITIONS,
        ERROR_TYPE_IDS,
        PARTICIPANT_TYPES,
        SUBCONDITIONS,
    )
    from backend.app.services.assignment import _select_assignment_unit

    candidates = [
        {
            "participant_type": participant_type,
            "condition": condition,
            "subcondition": subcondition,
            "error_type_id": error_type_id,
            "count": 0,
        }
        for participant_type in PARTICIPANT_TYPES
        for condition in CONDITIONS
        for subcondition in SUBCONDITIONS
        for error_type_id in ERROR_TYPE_IDS
    ]
    chooser = Random(20260711).choice

    first_assignments = []
    for _ in range(70):
        selected = _select_assignment_unit(candidates, chooser=chooser)
        first_assignments.append(selected.copy())
        selected["count"] = int(selected["count"]) + 1

    assert len(
        {
            (assignment["condition"], assignment["subcondition"])
            for assignment in first_assignments
        }
    ) > 1
    assert max(int(candidate["count"]) for candidate in candidates) - min(
        int(candidate["count"]) for candidate in candidates
    ) <= 1


def test_assignment_chooser_only_receives_minimum_count_candidates():
    from backend.app.services.assignment import _select_assignment_unit

    candidates = [
        {"condition": "human", "count": 2},
        {"condition": "tool", "count": 1},
        {"condition": "human", "count": 1},
    ]
    offered_candidates = []

    def choose_last(options):
        offered_candidates.extend(options)
        return options[-1]

    selected = _select_assignment_unit(candidates, chooser=choose_last)

    assert [candidate["count"] for candidate in offered_candidates] == [1, 1]
    assert selected is candidates[-1]


def test_long_participant_keeps_assignment_across_days_when_short_units_unavailable(
    sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.models.domain import CONDITIONS, ERROR_TYPE_IDS, SUBCONDITIONS
    from backend.app.services.participants import login_participant
    from backend.app import services

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                ("short", condition, subcondition, error_type_id, 0)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )

        with transaction(conn):
            participant = login_participant(
                conn,
                name="Example Long",
                phone="19900000101",
            )

        day_rows = conn.execute(
            """
            SELECT day_index, calendar_date, status
            FROM participant_days
            WHERE participant_id = ?
            ORDER BY day_index
            """,
            (participant.participant_id,),
        ).fetchall()
    finally:
        conn.close()

    assert participant.participant_type == "long"
    assert participant.target_days == 3
    assert participant.condition in {"human", "tool"}
    assert participant.subcondition in {
        "qa",
        "planning",
        "chat",
        "decision",
        "execution",
    }
    assert participant.topic_key
    assert participant.error_type_id
    assert [
        (row["day_index"], row["calendar_date"], row["status"]) for row in day_rows
    ] == [
        (1, "2026-07-02", "not_started"),
        (2, "2026-07-03", "not_started"),
        (3, "2026-07-04", "not_started"),
    ]


def test_assignment_stays_stable_for_repeat_login_before_day_one_completion(
    sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import list_attempts_for_participant
    from backend.app.services.participants import login_participant
    from backend.app import services

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        with transaction(conn):
            first_login = login_participant(
                conn,
                name="Repeat Login",
                phone="19900000102",
            )

        with transaction(conn):
            second_login = login_participant(
                conn,
                name="Repeat Login",
                phone="19900000102",
                data_dir=sqlite_settings.data_dir,
            )

        participant_count = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE name = ? AND phone = ?",
            ("Repeat Login", "19900000102"),
        ).fetchone()[0]
        attempts = list_attempts_for_participant(
            conn,
            participant_id=second_login.participant_id,
        )
    finally:
        conn.close()

    assert participant_count == 1
    assert second_login.attempt_id == first_login.attempt_id
    assert second_login.condition == first_login.condition
    assert second_login.subcondition == first_login.subcondition
    assert second_login.topic_key == first_login.topic_key
    assert second_login.error_type_id == first_login.error_type_id
    assert [row["status"] for row in attempts] == ["active"]


def test_assignment_controls_persist_and_skip_disabled_or_full_cells(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations
    from backend.app.models.domain import ERROR_TYPE_IDS
    from backend.app.services.assignment import assign_new_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("short", "human", "qa", error_type_id, 0, 0)
                for error_type_id in ERROR_TYPE_IDS
            ]
            + [
                ("short", "human", "planning", error_type_id, 0, 1)
                for error_type_id in ERROR_TYPE_IDS
            ],
        )

        assignment = assign_new_participant(
            conn, name="Task Two", phone_hash="hash-task-two"
        )
    finally:
        conn.close()

    assert (
        assignment.participant_type,
        assignment.condition,
        assignment.subcondition,
    ) not in {
        ("short", "human", "qa"),
        ("short", "human", "planning"),
    }


def test_assignment_cap_counts_attempt_rows_not_legacy_participant_placeholders(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.models.domain import CONDITIONS, ERROR_TYPE_IDS, SUBCONDITIONS
    from backend.app.repositories.participants import LEGACY_COMPAT_CONDITION
    from backend.app.services.assignment import assign_new_participant
    from backend.app.services.participants import login_participant

    target_unit = ("short", "tool", "qa", "factual_major")
    short_unit_controls = []
    for condition in CONDITIONS:
        for subcondition in SUBCONDITIONS:
            for error_type_id in ERROR_TYPE_IDS:
                is_target_unit = (
                    condition,
                    subcondition,
                    error_type_id,
                ) == target_unit[1:]
                short_unit_controls.append(
                    (
                        "short",
                        condition,
                        subcondition,
                        error_type_id,
                        1 if is_target_unit else None,
                        1 if is_target_unit else 0,
                    )
                )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET
                cap = excluded.cap,
                enabled = excluded.enabled
            """,
            short_unit_controls,
        )
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                enabled
            ) VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET enabled = excluded.enabled
            """,
            [
                ("long", condition, subcondition, error_type_id)
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )

        with transaction(conn):
            participant = login_participant(
                conn,
                name="Attempt Cap Source",
                phone="19900000103",
            )

        legacy_row = conn.execute(
            """
            SELECT condition
            FROM participants
            WHERE id = ?
            """,
            (participant.participant_id,),
        ).fetchone()

        with pytest.raises(ValueError, match="No assignment units available"):
            assign_new_participant(
                conn,
                name="Cap Should Be Full",
                phone_hash="hash-cap-should-be-full",
            )
    finally:
        conn.close()

    assert (
        participant.participant_type,
        participant.condition,
        participant.subcondition,
        participant.error_type_id,
    ) == target_unit
    assert legacy_row["condition"] == LEGACY_COMPAT_CONDITION


def test_topic_balancing_counts_attempt_topics_not_legacy_participant_topics(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.assignment import choose_topic_for_cell

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="Attempt Topic Source",
                phone="19900000104",
                phone_hash="hash-attempt-topic-source",
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="tool",
                subcondition="qa",
                topic_key="weather",
                error_type_id="factual_minor",
                target_days=1,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )

        topic_key = choose_topic_for_cell(conn, condition="tool", subcondition="qa")
    finally:
        conn.close()

    assert topic_key == "physics"


def test_legacy_global_rows_do_not_block_assignment(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations
    from backend.app.services.assignment import assign_new_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_global_controls (key, value)
            VALUES (?, ?)
            """,
            [
                ("pause_new_participants", "true"),
                ("test_channel_only", "true"),
            ],
        )

        assignment = assign_new_participant(
            conn,
            name="Legacy Controls Ignored",
            phone_hash="hash-legacy-controls",
        )
    finally:
        conn.close()

    assert assignment.participant_type in {"short", "long"}


def test_assignment_rejects_when_all_cells_disabled(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations
    from backend.app.models.domain import (
        CONDITIONS,
        ERROR_TYPE_IDS,
        PARTICIPANT_TYPES,
        SUBCONDITIONS,
    )
    from backend.app.services.assignment import assign_new_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                enabled
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (participant_type, condition, subcondition, error_type_id, 0)
                for participant_type in PARTICIPANT_TYPES
                for condition in CONDITIONS
                for subcondition in SUBCONDITIONS
                for error_type_id in ERROR_TYPE_IDS
            ],
        )

        with pytest.raises(ValueError, match="No assignment units available"):
            assign_new_participant(conn, name="None Left", phone_hash="hash-none")
    finally:
        conn.close()


def test_existing_participant_can_relogin_when_test_channel_only_is_enabled(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.participants import update_participant_day_status
    from backend.app.services.participants import login_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        with transaction(conn):
            first_login = login_participant(
                conn,
                name="Existing Participant",
                phone="19900000105",
            )
            day_row = conn.execute(
                """
                SELECT id
                FROM participant_days
                WHERE attempt_id = ? AND day_index = 1
                """,
                (first_login.attempt_id,),
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
                (first_login.attempt_id,),
            )

        conn.execute(
            """
            INSERT INTO admin_global_controls (key, value)
            VALUES ('test_channel_only', 'true')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """
        )

        with transaction(conn):
            second_login = login_participant(
                conn,
                name="Existing Participant",
                phone="19900000105",
            )

        participant_count = conn.execute(
            "SELECT COUNT(*) FROM participants WHERE name = ? AND phone = ?",
            ("Existing Participant", "19900000105"),
        ).fetchone()[0]
    finally:
        conn.close()

    assert participant_count == 1
    assert first_login.participant_id == second_login.participant_id


def test_assignment_selects_participant_type_and_error_unit(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations
    from backend.app.services.assignment import assign_new_participant

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        assignment = assign_new_participant(conn, name="张三", phone_hash="hash-a")
    finally:
        conn.close()

    assert assignment.participant_type in {"short", "long"}
    assert assignment.condition in {"human", "tool"}
    assert assignment.subcondition in {"qa", "planning", "chat", "decision", "execution"}
    assert assignment.error_type_id in {
        "factual_minor",
        "factual_major",
        "logic_minor",
        "logic_major",
        "social_minor",
        "social_major",
        "system_failure",
    }
    assert assignment.target_days == (1 if assignment.participant_type == "short" else 3)


def test_tool_qa_topic_balances_between_weather_and_physics(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations, transaction
    from backend.app.repositories.attempts import create_attempt, set_current_attempt
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.assignment import choose_topic_for_cell

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        first = choose_topic_for_cell(conn, condition="tool", subcondition="qa")
        with transaction(conn):
            participant_id = insert_participant_identity(
                conn,
                name="A",
                phone="13800000000",
                phone_hash="h1",
            )
            attempt_id = create_attempt(
                conn,
                participant_id=participant_id,
                participant_type="short",
                condition="tool",
                subcondition="qa",
                topic_key=first,
                error_type_id="factual_minor",
                target_days=1,
            )
            set_current_attempt(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
        second = choose_topic_for_cell(conn, condition="tool", subcondition="qa")
    finally:
        conn.close()

    assert {first, second} == {"weather", "physics"}


def test_tool_qa_topic_randomizes_tied_topics_with_injected_chooser(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations
    from backend.app.services.assignment import choose_topic_for_cell

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        topic_key = choose_topic_for_cell(
            conn,
            condition="tool",
            subcondition="qa",
            chooser=lambda topics: topics[-1],
        )
    finally:
        conn.close()

    assert topic_key == "physics"
