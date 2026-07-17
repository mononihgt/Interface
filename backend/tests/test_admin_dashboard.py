from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3

import pytest

from backend.app.db import get_connection, run_migrations
from backend.app.repositories.attempts import create_attempt, set_current_attempt
from backend.app.repositories.admin import AdminRepository
from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'admin-dashboard.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt="task15-salt",
        admin_password_hash=hashlib.sha256(b"task15-saltadmin-pass").hexdigest(),
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
    phone_hash: str,
    attempt_status: str = "completed",
) -> tuple[int, int]:
    participant_cursor = conn.execute(
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
        ) VALUES (?, '13800138000', ?, 'short', 'human', 'qa', 'topic-qa',
                  'factual_minor', 1, 'completed')
        """,
        (name, phone_hash),
    )
    participant_id = int(participant_cursor.lastrowid)
    attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type="short",
        condition="human",
        subcondition="qa",
        topic_key="topic-qa",
        error_type_id="factual_minor",
        target_days=1,
        status=attempt_status,
    )
    set_current_attempt(conn, participant_id=participant_id, attempt_id=attempt_id)
    return participant_id, attempt_id


def _insert_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    started_at: str,
    completed_at: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO participant_days (
            participant_id,
            attempt_id,
            day_index,
            calendar_date,
            status,
            started_at,
            completed_at
        ) VALUES (?, ?, 1, '2026-07-02', 'completed', ?, ?)
        """,
        (participant_id, attempt_id, started_at, completed_at),
    )
    return int(cursor.lastrowid)


def _insert_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    participant_day_id: int,
    session_uuid: str,
    is_test: bool,
) -> int:
    cursor = conn.execute(
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
            is_test
        ) VALUES (?, ?, ?, ?, 'human', 'qa', 'topic-qa', 'scenario-1',
                  'graph-v1', 'factual_minor', 2, 'completed',
                  '2026-07-02T08:00:00+08:00', '2026-07-02T08:05:00+08:00', ?)
        """,
        (
            participant_id,
            attempt_id,
            participant_day_id,
            session_uuid,
            1 if is_test else 0,
        ),
    )
    return int(cursor.lastrowid)


def _insert_api_failure(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    route: str,
    session_id: int | None,
    is_test: bool | None,
) -> None:
    conn.execute(
        """
        INSERT INTO api_call_logs (
            request_id,
            route,
            provider,
            model,
            status,
            error_code,
            session_id,
            is_test
        ) VALUES (?, ?, 'test-provider', 'test-model', 'http_error',
                  'provider_failure', ?, ?)
        """,
        (request_id, route, session_id, None if is_test is None else int(is_test)),
    )


def test_overview_uses_shanghai_calendar_dates_for_today(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.repositories import admin as admin_repository

    monkeypatch.setattr(admin_repository, "current_shanghai_date", lambda: "2026-07-02")
    participant_id, attempt_id = _insert_participant(
        conn,
        name="Formal Participant",
        phone_hash="formal-participant",
    )
    _insert_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        started_at="2026-07-01T16:00:00Z",
        completed_at="2026-07-01T15:59:59Z",
    )
    other_participant_id, other_attempt_id = _insert_participant(
        conn,
        name="Next Day Participant",
        phone_hash="next-day-participant",
    )
    _insert_day(
        conn,
        participant_id=other_participant_id,
        attempt_id=other_attempt_id,
        started_at="2026-07-01T23:59:00Z",
        completed_at="2026-07-02T16:00:00Z",
    )

    overview = AdminRepository(conn, settings=sqlite_settings).get_overview_metrics()

    assert overview["today_started"] == 2
    assert overview["today_completed"] == 0


def test_assignment_batch_filter_preview_is_bounded_stable_and_versioned(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    repository = AdminRepository(conn, settings=sqlite_settings)
    request_scope = {
        "filter": {
            "participant_type": "short",
            "condition": "human",
        }
    }

    first_preview = repository.preview_assignment_control_batch(
        scope=request_scope,
        changes={"enabled": False},
        cap_is_set=False,
    )
    second_preview = repository.preview_assignment_control_batch(
        scope=request_scope,
        changes={"enabled": False},
        cap_is_set=False,
    )

    assert first_preview == second_preview
    assert first_preview["affected_count"] == 35
    assert first_preview["scope"] == {
        "kind": "filter_snapshot",
        "description": "participant_type=short, condition=human",
        "selected_cells": [
            {
                "participant_type": "short",
                "condition": "human",
                "subcondition": subcondition,
                "error_type_id": error_type_id,
            }
            for subcondition in ("chat", "decision", "execution", "planning", "qa")
            for error_type_id in (
                "factual_major",
                "factual_minor",
                "logic_major",
                "logic_minor",
                "social_major",
                "social_minor",
                "system_failure",
            )
        ],
    }

    repository.update_assignment_controls(
        admin_user="admin",
        operation="cell",
        participant_type="short",
        condition="human",
        subcondition="qa",
        error_type_id="factual_minor",
        cap=3,
        enabled=True,
    )
    changed_preview = repository.preview_assignment_control_batch(
        scope=request_scope,
        changes={"enabled": False},
        cap_is_set=False,
    )

    assert changed_preview["scope_version"] != first_preview["scope_version"]

    with pytest.raises(ValueError, match="bounded"):
        repository.preview_assignment_control_batch(
            scope={"filter": {}},
            changes={"enabled": False},
            cap_is_set=False,
        )


def test_assignment_batch_reached_filter_uses_current_counted_assignments(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    _insert_participant(
        conn,
        name="Reached Assignment Cell",
        phone_hash="reached-assignment-cell",
    )
    repository = AdminRepository(conn, settings=sqlite_settings)
    repository.update_assignment_controls(
        admin_user="admin",
        operation="cell",
        participant_type="short",
        condition="human",
        subcondition="qa",
        error_type_id="factual_minor",
        cap=1,
        enabled=True,
    )

    preview = repository.preview_assignment_control_batch(
        scope={"filter": {"cap_status": "reached"}},
        changes={"enabled": False},
        cap_is_set=False,
    )

    assert preview["affected_count"] == 1
    assert preview["scope"] == {
        "kind": "filter_snapshot",
        "description": "cap_status=reached",
        "selected_cells": [
            {
                "participant_type": "short",
                "condition": "human",
                "subcondition": "qa",
                "error_type_id": "factual_minor",
            }
        ],
    }


def test_formal_dashboard_metrics_exclude_only_test_and_unscoped_data(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.repositories import admin as admin_repository

    monkeypatch.setattr(admin_repository, "current_shanghai_date", lambda: "2026-07-02")
    participant_id, attempt_id = _insert_participant(
        conn,
        name="__test_channel__",
        phone_hash="test-channel",
    )
    day_id = _insert_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        started_at="2026-07-01T16:00:00Z",
        completed_at="2026-07-02T15:59:59Z",
    )
    session_id = _insert_session(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        participant_day_id=day_id,
        session_uuid="internal-test-session",
        is_test=True,
    )
    conn.execute(
        "INSERT INTO session_risk_flags (session_id, flag) VALUES (?, 'api_failure')",
        (session_id,),
    )
    _insert_api_failure(
        conn,
        request_id="test-session-failure",
        route="asr",
        session_id=session_id,
        is_test=True,
    )
    _insert_api_failure(
        conn,
        request_id="unscoped-provider-health-failure",
        route="chat",
        session_id=None,
        is_test=None,
    )
    conn.execute(
        """
        INSERT INTO clean_data_audits (participant_id, attempt_id, status, reasons_json)
        VALUES (?, ?, 'excluded', '["internal_test"]')
        """,
        (participant_id, attempt_id),
    )
    exports_dir = sqlite_settings.data_dir / "exports"
    exports_dir.mkdir()
    test_export_path = exports_dir / "test-only.zip"
    test_export_path.write_bytes(b"test export")
    conn.execute(
        """
        INSERT INTO export_jobs (
            job_uuid,
            export_type,
            filters_json,
            include_test,
            status,
            output_path,
            created_by
        ) VALUES ('test-only-job', 'experiment_data', '{}', 1, 'succeeded', ?, 'admin')
        """,
        (str(test_export_path),),
    )

    repository = AdminRepository(conn, settings=sqlite_settings)
    overview = repository.get_overview_metrics()
    data_monitor = repository.get_data_monitor_summary()
    system_metrics = repository.get_system_metrics()

    assert overview["total_participants"] == 0
    assert overview["completed_sessions"] == 0
    assert overview["active_sessions"] == 0
    assert overview["today_started"] == 0
    assert overview["today_completed"] == 0
    assert overview["risk_sessions"] == 0
    assert overview["completion_by_type"] == {"short": 0, "long": 0}
    assert overview["api_failures"] == 0
    assert overview["asr_failures"] == 0
    assert all(
        cell["count"] == 0
        for row in overview["assignment_matrix"]
        for cell in row["cells"]
    )
    assert data_monitor["metrics"]["clean_data_eligible"] == 0
    assert data_monitor["metrics"]["clean_data_review_needed"] == 0
    assert data_monitor["metrics"]["clean_data_excluded"] == 0
    assert data_monitor["incomplete_sessions"] == []
    assert data_monitor["recent_sessions"] == []
    assert data_monitor["risk_sessions"] == []
    assert system_metrics["experiment"]["api_failures"] == 0
    assert system_metrics["exports_directory"]["files"] == 0
    assert system_metrics["exports_directory"]["size_bytes"] == 0


def test_formal_api_failure_metrics_require_a_formal_session_scope(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    participant_id, attempt_id = _insert_participant(
        conn,
        name="Formal Participant",
        phone_hash="formal-participant",
    )
    day_id = _insert_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        started_at="2026-07-02T00:00:00Z",
        completed_at="2026-07-02T01:00:00Z",
    )
    session_id = _insert_session(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        participant_day_id=day_id,
        session_uuid="formal-session",
        is_test=False,
    )
    test_session_id = _insert_session(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        participant_day_id=day_id,
        session_uuid="test-session-for-formal-participant",
        is_test=True,
    )
    conn.execute(
        "INSERT INTO session_risk_flags (session_id, flag) VALUES (?, 'api_failure')",
        (test_session_id,),
    )
    _insert_api_failure(
        conn,
        request_id="formal-chat-failure",
        route="chat",
        session_id=session_id,
        is_test=None,
    )
    _insert_api_failure(
        conn,
        request_id="formal-asr-failure",
        route="asr",
        session_id=session_id,
        is_test=False,
    )
    _insert_api_failure(
        conn,
        request_id="test-marked-scoped-failure",
        route="asr",
        session_id=session_id,
        is_test=True,
    )
    _insert_api_failure(
        conn,
        request_id="test-session-scoped-failure",
        route="asr",
        session_id=test_session_id,
        is_test=False,
    )

    overview = AdminRepository(conn, settings=sqlite_settings).get_overview_metrics()

    assert overview["api_failures"] == 2
    assert overview["asr_failures"] == 1
    assert overview["completed_sessions"] == 1
    assert overview["risk_sessions"] == 0


def test_system_metrics_count_only_formal_export_job_files(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    exports_dir = sqlite_settings.data_dir / "exports"
    exports_dir.mkdir()
    formal_path = exports_dir / "formal.zip"
    test_path = exports_dir / "test.zip"
    unscoped_path = exports_dir / "unscoped.zip"
    formal_path.write_bytes(b"formal")
    test_path.write_bytes(b"test")
    unscoped_path.write_bytes(b"unscoped")
    conn.executemany(
        """
        INSERT INTO export_jobs (
            job_uuid,
            export_type,
            filters_json,
            include_test,
            status,
            publication_state,
            output_path,
            created_by
        ) VALUES (?, 'experiment_data', '{}', ?, 'succeeded', 'published', ?, 'admin')
        """,
        [
            ("formal-job", 0, str(formal_path)),
            ("test-job", 1, str(test_path)),
        ],
    )
    conn.execute(
        """
        INSERT INTO export_jobs (
            job_uuid,
            export_type,
            filters_json,
            include_test,
            status,
            publication_state,
            output_path,
            created_by
        ) VALUES (
            'publication-incomplete-job',
            'experiment_data',
            '{}',
            0,
            'succeeded',
            'publishing',
            ?,
            'admin'
        )
        """,
        (str(unscoped_path),),
    )

    metrics = AdminRepository(conn, settings=sqlite_settings).get_system_metrics()

    assert metrics["exports_directory"]["files"] == 1
    assert metrics["exports_directory"]["size_bytes"] == len(b"formal")


def test_deepseek_usage_reports_configuration_timeouts_and_redacts_credentials(
    conn: sqlite3.Connection,
    sqlite_settings: Settings,
) -> None:
    deepseek_key = "DEEPSEEK_PRIVATE_KEY_SENTINEL"
    configured_settings = sqlite_settings.model_copy(
        update={
            "deepseek_api_key": deepseek_key,
            "deepseek_timeout_seconds": 15.0,
        }
    )
    conn.executemany(
        """
        INSERT INTO api_call_logs (
            request_id,
            route,
            provider,
            model,
            status,
            error_code,
            error_message_summary,
            latency_ms,
            cooldown_applied
        ) VALUES (?, 'chat', 'deepseek', 'deepseek-v4-pro', ?, ?, ?, ?, ?)
        """,
        [
            ("deepseek-success", "success", None, None, 25, 0),
            (
                "deepseek-timeout",
                "timeout",
                "timeout",
                f"provider timeout at private.invalid with {deepseek_key}",
                15_000,
                1,
            ),
        ],
    )

    payload = AdminRepository(
        conn,
        settings=configured_settings,
    ).get_provider_model_usage()

    assert payload["deepseek_configuration"] == {
        "status": "configured",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": "https://api.deepseek.com",
        "timeout_seconds": 15.0,
    }
    assert deepseek_key not in str(payload)
    assert "private.invalid" not in str(payload)
    all_time = next(
        window for window in payload["windows"] if window["window"] == "all_time"
    )
    provider_row = next(
        row
        for row in all_time["provider_model_rows"]
        if row["provider"] == "deepseek" and row["model"] == "deepseek-v4-pro"
    )
    route_row = next(
        row
        for row in all_time["route_rows"]
        if row["route"] == "chat"
        and row["provider"] == "deepseek"
        and row["model"] == "deepseek-v4-pro"
    )
    assert provider_row["timeout_count"] == 1
    assert route_row["timeout_count"] == 1
    assert provider_row["last_failure_summary"] == "timeout:timeout"
