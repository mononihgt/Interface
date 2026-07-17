from pathlib import Path
import sqlite3
import time

import pytest

from backend.app.settings import Settings


REQUIRED_TABLES = {
    "participants",
    "participant_days",
    "pretest_responses",
    "experiment_sessions",
    "conversation_turns",
    "asr_attempts",
    "turn_ratings",
    "task_artifacts",
    "api_call_logs",
    "provider_cooldowns",
    "admin_events",
    "admin_assignment_cells",
    "admin_global_controls",
    "session_risk_flags",
    "schema_migrations",
    "external_operations",
    "cleanup_operations",
    "cleanup_operation_owners",
    "admin_credentials",
    "admin_login_attempts",
    "recruitment_control",
}


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "bootstrap.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
    )


def _create_legacy_admin_global_controls(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE admin_global_controls (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def test_create_app_runs_startup_migrations(sqlite_settings: Settings):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    client = TestClient(create_app(settings=sqlite_settings))

    with client:
        response = client.get("/api/health")

    assert response.status_code == 200

    db_path = sqlite_settings.data_dir / "bootstrap.db"
    assert db_path.exists()

    conn = sqlite3.connect(db_path)
    try:
        table_names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    assert REQUIRED_TABLES.issubset(table_names)


def test_startup_migrations_add_client_response_timing_columns(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(conversation_turns)"
            ).fetchall()
        }
    finally:
        conn.close()

    assert {
        "client_message_sent_at",
        "assistant_render_completed_at",
        "client_response_latency_ms",
        "client_timing_interrupted",
        "render_timing_received_at",
    } <= columns


def test_create_app_reconciles_cleanup_operations_before_serving(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from fastapi.testclient import TestClient

    from backend.app import main

    calls: list[Path] = []
    monkeypatch.setattr(
        main,
        "reconcile_cleanup_operations",
        lambda _conn, *, data_dir: calls.append(data_dir),
    )

    with TestClient(main.create_app(settings=sqlite_settings)) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert calls == [sqlite_settings.data_dir]


def test_create_app_recovers_queued_export_jobs_without_admin_request(
    sqlite_settings: Settings,
) -> None:
    from fastapi.testclient import TestClient

    from backend.app.db import get_connection, run_migrations
    from backend.app.main import create_app
    from backend.app.services.export_jobs import create_export_job

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        queued_job = create_export_job(
            conn,
            export_type="experiment_data",
            filters={},
            include_test=False,
            created_by="admin",
        )
    finally:
        conn.close()

    with TestClient(create_app(settings=sqlite_settings)):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            check_conn = get_connection(sqlite_settings)
            try:
                row = check_conn.execute(
                    "SELECT status, output_path FROM export_jobs WHERE job_uuid = ?",
                    (queued_job["job_uuid"],),
                ).fetchone()
            finally:
                check_conn.close()
            if row["status"] == "succeeded":
                break
            time.sleep(0.02)

    assert row["status"] == "succeeded"
    assert Path(str(row["output_path"])).is_file()


def test_export_recovery_supervisor_waits_for_valid_orphan_lease_to_expire(
    sqlite_settings: Settings,
) -> None:
    from backend.app.db import get_connection, run_migrations
    from backend.app.services.export_jobs import (
        create_export_job,
        start_export_job_recovery,
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
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
                lease_owner = 'orphan-worker',
                lease_token = 'orphan-token',
                lease_expires_at = datetime('now', '+1 second'),
                heartbeat_at = CURRENT_TIMESTAMP,
                attempt_count = 1
            WHERE job_uuid = ?
            """,
            (queued_job["job_uuid"],),
        )
    finally:
        conn.close()

    supervisor = start_export_job_recovery(sqlite_settings, poll_seconds=0.02)
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            check_conn = get_connection(sqlite_settings)
            try:
                row = check_conn.execute(
                    "SELECT status, attempt_count FROM export_jobs WHERE job_uuid = ?",
                    (queued_job["job_uuid"],),
                ).fetchone()
            finally:
                check_conn.close()
            if row["status"] == "succeeded":
                break
            time.sleep(0.02)
    finally:
        supervisor.stop()
        supervisor.join()

    assert row["status"] == "succeeded"
    assert row["attempt_count"] == 2
    assert supervisor.is_alive() is False


def test_create_app_stops_export_recovery_supervisor_without_thread_leak(
    sqlite_settings: Settings,
) -> None:
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    app = create_app(settings=sqlite_settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        supervisor = app.state.export_job_recovery_supervisor
        assert supervisor.is_alive() is True

    assert supervisor.is_alive() is False


def test_get_connection_enables_sqlite_pragmas_and_row_factory(sqlite_settings: Settings):
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_procedure_alignment_columns_exist(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(turn_ratings)").fetchall()
        }
    finally:
        conn.close()

    assert "stance_score" in columns
    assert "trust_score" in columns
    assert "impression_score" not in columns


def test_export_job_lease_migration_preserves_existing_rows(
    sqlite_settings: Settings,
) -> None:
    from backend.app.db import get_connection

    conn = get_connection(sqlite_settings)
    try:
        conn.executescript(
            """
            CREATE TABLE export_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_uuid TEXT NOT NULL UNIQUE,
                export_type TEXT NOT NULL,
                filters_json TEXT NOT NULL,
                include_test INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                progress_message TEXT,
                output_path TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                started_at TEXT,
                completed_at TEXT,
                error_message TEXT
            );
            INSERT INTO export_jobs (
                job_uuid,
                export_type,
                filters_json,
                include_test,
                status,
                output_path,
                created_by
            ) VALUES (
                'preserved-export',
                'experiment_data',
                '{}',
                0,
                'succeeded',
                '/tmp/preserved-export.zip',
                'admin'
            );
            """
        )
        migration_path = Path(__file__).parents[1] / "migrations" / "010_export_job_leases.sql"
        conn.executescript(migration_path.read_text(encoding="utf-8"))

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(export_jobs)").fetchall()
        }
        preserved_row = conn.execute(
            """
            SELECT job_uuid, status, output_path, attempt_count, publication_state
            FROM export_jobs
            WHERE job_uuid = 'preserved-export'
            """
        ).fetchone()
    finally:
        conn.close()

    assert {
        "lease_owner",
        "lease_token",
        "lease_expires_at",
        "heartbeat_at",
        "attempt_count",
        "failure_kind",
        "publication_state",
        "publication_token",
        "staging_path",
        "canonical_path",
        "archive_sha256",
    }.issubset(columns)
    assert dict(preserved_row) == {
        "job_uuid": "preserved-export",
        "status": "succeeded",
        "output_path": "/tmp/preserved-export.zip",
        "attempt_count": 0,
        "publication_state": "published",
    }


@pytest.mark.parametrize("legacy_column", ["impression_score", "perception_score"])
def test_run_migrations_upgrades_legacy_turn_rating_stance_column(
    sqlite_settings: Settings,
    legacy_column: str,
):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        legacy_schema = f"""
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO schema_migrations (version) VALUES
                ('001_initial'),
                ('002_procedure_alignment'),
                ('003_attempt_flow'),
                ('003_provider_cooldowns'),
                ('003_unique_participant_identity'),
                ('004_asr_attempts'),
                ('005_admin_controls'),
                ('006_clean_data_audits_attempt_scope');

            CREATE TABLE turn_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_id INTEGER NOT NULL UNIQUE,
                {legacy_column} INTEGER NOT NULL CHECK ({legacy_column} BETWEEN 1 AND 5),
                trust_score INTEGER NOT NULL CHECK (trust_score BETWEEN 1 AND 7),
                submitted_at TEXT NOT NULL,
                client_elapsed_ms INTEGER CHECK (client_elapsed_ms IS NULL OR client_elapsed_ms >= 0)
            );

            INSERT INTO turn_ratings (
                turn_id,
                {legacy_column},
                trust_score,
                submitted_at,
                client_elapsed_ms
            ) VALUES (1, 4, 6, '2026-07-02T10:00:00+08:00', 1200);
            """
        conn.executescript(legacy_schema)
        _create_legacy_admin_global_controls(conn)

        run_migrations(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(turn_ratings)").fetchall()
        }
        rating_row = conn.execute(
            """
            SELECT stance_score, trust_score, client_elapsed_ms
            FROM turn_ratings
            WHERE turn_id = 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert "stance_score" in columns
    assert legacy_column not in columns
    assert rating_row["stance_score"] == 4
    assert rating_row["trust_score"] == 6
    assert rating_row["client_elapsed_ms"] == 1200


def test_run_migrations_is_idempotent(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        run_migrations(conn)

        migrations = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    assert [row["version"] for row in migrations] == [
        "001_initial",
        "002_procedure_alignment",
        "003_attempt_flow",
        "003_provider_cooldowns",
        "003_unique_participant_identity",
        "004_asr_attempts",
        "005_admin_controls",
        "006_clean_data_audits_attempt_scope",
        "007_external_operations",
        "008_cleanup_operations",
        "009_admin_login_security",
        "010_export_job_leases",
        "011_recruitment_control",
        "012_asr_result_references",
        "013_unified_recruitment",
        "014_error_semantic_evidence",
        "015_client_response_timing",
    ]


def test_unified_recruitment_migration_removes_legacy_admission_rows_without_changing_status(
    sqlite_settings: Settings,
) -> None:
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = '013_unified_recruitment'"
        )
        conn.executemany(
            "INSERT INTO admin_global_controls (key, value) VALUES (?, ?)",
            [
                ("pause_new_participants", "true"),
                ("test_channel_only", "true"),
            ],
        )
        conn.execute(
            "UPDATE recruitment_control SET status = 'open' WHERE id = 1"
        )

        run_migrations(conn)

        legacy_rows = conn.execute(
            """
            SELECT key FROM admin_global_controls
            WHERE key IN ('pause_new_participants', 'test_channel_only')
            ORDER BY key
            """
        ).fetchall()
        status_row = conn.execute(
            "SELECT status FROM recruitment_control WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()

    assert legacy_rows == []
    assert status_row[0] == "open"


def test_publication_control_and_asr_reference_migrations_are_fail_closed_and_additive(
    sqlite_settings: Settings,
) -> None:
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        recruitment_row = conn.execute(
            "SELECT id, status, updated_by FROM recruitment_control"
        ).fetchone()
        asr_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(asr_attempts)")
        }
        result_ref_indexes = {
            row["name"]: bool(row["unique"])
            for row in conn.execute("PRAGMA index_list(asr_attempts)")
        }
        conn.execute(
            """
            INSERT INTO admin_events (
                admin_user, action, target_type, target_id, payload_json
            ) VALUES ('admin', 'set_recruitment', 'recruitment', 'formal', '{"status":"open"}')
            """
        )
    finally:
        conn.close()

    assert dict(recruitment_row) == {
        "id": 1,
        "status": "closed",
        "updated_by": None,
    }
    assert "result_ref" in asr_columns
    assert result_ref_indexes["idx_asr_attempts_result_ref"] is True


def test_cleanup_operation_columns_exist(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(cleanup_operations)").fetchall()
        }
        owner_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(cleanup_operation_owners)"
            ).fetchall()
        }
        converted_source_indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(participant_attempts)").fetchall()
            if row["unique"]
        }
    finally:
        conn.close()

    assert {
        "operation_kind",
        "source_path",
        "staging_path",
        "destination_path",
        "expected_sha256",
        "worker_token",
        "lease_expires_at",
        "state",
    }.issubset(columns)

    assert {
        "operation_id",
        "owner_table",
        "owner_row_id",
        "owner_field",
        "original_path",
        "original_sha256",
    }.issubset(owner_columns)

    assert "idx_participant_attempts_unique_source" in converted_source_indexes


def test_admin_login_throttle_schema_uses_indexed_bounded_state(
    sqlite_settings: Settings,
):
    from backend.app.admin import auth
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(admin_login_attempts)").fetchall()
        }
        statements = [
            (
                getattr(auth, "COUNT_USERNAME_LOGIN_ATTEMPTS_SQL", ""),
                ("a" * 64,),
                "idx_admin_login_attempts_username_expiry",
            ),
            (
                getattr(auth, "COUNT_ADDRESS_LOGIN_ATTEMPTS_SQL", ""),
                ("127.0.0.1",),
                "idx_admin_login_attempts_address_expiry",
            ),
            (
                getattr(auth, "DELETE_EXPIRED_LOGIN_ATTEMPTS_SQL", ""),
                (),
                "idx_admin_login_attempts_expiry",
            ),
        ]
        plans: list[tuple[str, list[str]]] = []
        for statement, parameters, expected_index in statements:
            assert statement
            details = [
                row["detail"]
                for row in conn.execute(
                    f"EXPLAIN QUERY PLAN {statement}",
                    parameters,
                ).fetchall()
            ]
            plans.append((expected_index, details))
    finally:
        conn.close()

    assert columns == {
        "id",
        "reservation_token",
        "username_key",
        "client_address",
        "state",
        "expires_at",
        "created_at",
    }
    for expected_index, details in plans:
        assert any(expected_index in detail for detail in details)
        assert all("SCAN admin_login_attempts" not in detail for detail in details)


def test_run_migrations_upgrades_applied_legacy_cleanup_operations(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations
    from backend.app.repositories.attempts import create_attempt
    from backend.app.repositories.participants import insert_participant_identity
    from backend.app.services.cleanup_attempts import (
        CleanupReconciliationError,
        reconcile_cleanup_operations,
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        participant_id = insert_participant_identity(
            conn,
            name="Legacy Cleanup",
            phone="13800000111",
            phone_hash="legacy-cleanup-hash",
        )
        attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="long",
            condition="human",
            subcondition="qa",
            topic_key="advice",
            error_type_id="factual_minor",
            target_days=3,
        )
        conn.execute("DROP TABLE cleanup_operation_owners")
        conn.execute("ALTER TABLE cleanup_operations RENAME TO cleanup_operations_current")
        conn.execute(
            """
            CREATE TABLE cleanup_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                staging_path TEXT NOT NULL UNIQUE,
                destination_path TEXT NOT NULL,
                expected_sha256 TEXT NOT NULL,
                state TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cleanup_operations (
                attempt_id, source_path, staging_path, destination_path,
                expected_sha256, state
            ) VALUES (?, 'audio/source.webm', 'audio/.stage',
                      'audio/destination.webm', ?, 'database_committed')
            """,
            (attempt_id, "a" * 64),
        )
        conn.execute("DROP TABLE cleanup_operations_current")

        run_migrations(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(cleanup_operations)").fetchall()
        }
        operation = conn.execute(
            "SELECT operation_kind, state, last_error FROM cleanup_operations"
        ).fetchone()
        owner_table_exists = conn.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'cleanup_operation_owners'
            """
        ).fetchone()
        with pytest.raises(CleanupReconciliationError) as exc_info:
            reconcile_cleanup_operations(conn, data_dir=sqlite_settings.data_dir)
    finally:
        conn.close()

    assert "operation_kind" in columns
    assert operation["operation_kind"] == "relocate"
    assert operation["state"] == "review_needed"
    assert operation["last_error"] == "legacy_owner_metadata_missing"
    assert owner_table_exists is not None
    assert exc_info.value.operations[0]["state"] == "review_needed"


def test_external_operation_and_api_log_scope_columns_exist(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        operation_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(external_operations)").fetchall()
        }
        api_log_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(api_call_logs)").fetchall()
        }
    finally:
        conn.close()

    assert {
        "operation_id",
        "request_fingerprint",
        "participant_id",
        "attempt_id",
        "session_id",
        "kind",
        "turn_index",
        "status",
        "result_entity_id",
        "result_json",
        "error_json",
    }.issubset(operation_columns)
    assert {"session_id", "turn_index", "is_test"}.issubset(api_log_columns)


def test_run_migrations_repairs_applied_007_scope_columns(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE api_call_logs (id INTEGER PRIMARY KEY, route TEXT NOT NULL)"
        )
        conn.execute(
            """
            CREATE TABLE external_operations (
                id INTEGER PRIMARY KEY,
                result_entity_id INTEGER
            )
            """
        )
        conn.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [
                ("001_initial",),
                ("002_procedure_alignment",),
                ("003_attempt_flow",),
                ("003_provider_cooldowns",),
                ("003_unique_participant_identity",),
                ("004_asr_attempts",),
                ("005_admin_controls",),
                ("006_clean_data_audits_attempt_scope",),
                ("007_external_operations",),
            ],
        )
        _create_legacy_admin_global_controls(conn)

        run_migrations(conn)

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(api_call_logs)").fetchall()
        }
        indexes = {
            row["name"]
            for row in conn.execute("PRAGMA index_list(api_call_logs)").fetchall()
        }
    finally:
        conn.close()

    assert {"session_id", "turn_index", "is_test"}.issubset(columns)
    assert "idx_api_call_logs_session_turn" in indexes


def test_run_migrations_repairs_legacy_007_operation_uniqueness(
    sqlite_settings: Settings,
):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.execute(
            "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)"
        )
        conn.execute("CREATE TABLE participants (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE participant_attempts (id INTEGER PRIMARY KEY, participant_id INTEGER)"
        )
        conn.execute(
            "CREATE TABLE experiment_sessions (id INTEGER PRIMARY KEY, participant_id INTEGER)"
        )
        conn.execute("INSERT INTO participants (id) VALUES (1)")
        conn.executemany(
            "INSERT INTO participant_attempts (id, participant_id) VALUES (?, 1)",
            [(10,), (11,)],
        )
        conn.executemany(
            "INSERT INTO experiment_sessions (id, participant_id) VALUES (?, 1)",
            [(100,), (101,)],
        )
        conn.execute(
            """
            CREATE TABLE external_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                participant_id INTEGER NOT NULL,
                attempt_id INTEGER,
                session_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                status TEXT NOT NULL,
                result_entity_id INTEGER,
                result_json TEXT,
                error_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (participant_id, kind, operation_id)
            )
            """
        )
        conn.execute(
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
            ) VALUES ('shared-key', 'first', 1, 10, 100, 'turn', 1, 'pending')
            """
        )
        conn.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [
                ("001_initial",),
                ("002_procedure_alignment",),
                ("003_attempt_flow",),
                ("003_provider_cooldowns",),
                ("003_unique_participant_identity",),
                ("004_asr_attempts",),
                ("005_admin_controls",),
                ("006_clean_data_audits_attempt_scope",),
                ("007_external_operations",),
            ],
        )
        _create_legacy_admin_global_controls(conn)

        run_migrations(conn)

        conn.execute(
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
            ) VALUES ('shared-key', 'second', 1, 11, 101, 'turn', 2, 'pending')
            """
        )
        operation_count = conn.execute(
            "SELECT COUNT(*) FROM external_operations WHERE operation_id = 'shared-key'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert operation_count == 2


def test_attempt_flow_tables_and_columns_exist(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        attempt_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(participant_attempts)").fetchall()
        }
        participant_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(participants)").fetchall()
        }
        participant_day_unique_indexes = [
            tuple(
                info["name"]
                for info in conn.execute(
                    f"PRAGMA index_info('{index_row['name']}')"
                ).fetchall()
            )
            for index_row in conn.execute("PRAGMA index_list(participant_days)").fetchall()
            if index_row["unique"]
        ]
    finally:
        conn.close()

    assert {
        "id",
        "participant_id",
        "attempt_no",
        "participant_type",
        "condition",
        "subcondition",
        "topic_key",
        "error_type_id",
        "target_days",
        "status",
        "valid_for_export",
        "source_attempt_id",
        "export_role",
        "blocked_reason",
    }.issubset(attempt_columns)

    assert "current_attempt_id" in participant_columns
    assert ("participant_id", "day_index") not in participant_day_unique_indexes
    assert (
        ("attempt_id", "day_index") in participant_day_unique_indexes
        or ("participant_id", "attempt_id", "day_index")
        in participant_day_unique_indexes
    )

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        for table_name in ("participant_days", "pretest_responses", "experiment_sessions"):
            columns = {
                row["name"]
                for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
            }
            assert "attempt_id" in columns
    finally:
        conn.close()


def test_run_migrations_upgrades_populated_legacy_attempt_flow_schema_without_breaking_session_day_links(
    sqlite_settings: Settings,
):
    from backend.app.db import MIGRATIONS_DIR, get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.executescript(
            (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        )
        conn.executescript(
            (MIGRATIONS_DIR / "002_procedure_alignment.sql").read_text(
                encoding="utf-8"
            )
        )
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [("001_initial",), ("002_procedure_alignment",)],
        )

        conn.execute(
            """
            INSERT INTO participants (
                id,
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
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "Legacy Attempt Participant",
                "legacy-attempt-phone",
                "legacy-attempt-hash",
                "short",
                "human",
                "qa",
                "topic-legacy",
                "factual_minor",
                1,
                "active",
            ),
        )
        conn.execute(
            """
            INSERT INTO participant_days (
                id,
                participant_id,
                day_index,
                calendar_date,
                status
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (1, 1, 1, "2026-07-02", "completed"),
        )
        conn.execute(
            """
            INSERT INTO experiment_sessions (
                id,
                participant_id,
                participant_day_id,
                session_uuid,
                condition,
                subcondition,
                topic_key,
                scenario_id,
                agent_graph_version,
                error_type_id,
                planned_error_turn,
                status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                1,
                1,
                "legacy-attempt-session",
                "human",
                "qa",
                "topic-legacy",
                "scenario-legacy",
                "graph-v1",
                "factual_minor",
                3,
                "completed",
            ),
        )

        run_migrations(conn)

        session_row = conn.execute(
            """
            SELECT
                es.id,
                es.participant_day_id,
                es.attempt_id,
                pd.id AS joined_participant_day_id,
                pd.attempt_id AS participant_day_attempt_id
            FROM experiment_sessions es
            LEFT JOIN participant_days pd ON pd.id = es.participant_day_id
            WHERE es.id = 1
            """
        ).fetchone()
        foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
        participant_day_unique_indexes = [
            tuple(
                info["name"]
                for info in conn.execute(
                    f"PRAGMA index_info('{index_row['name']}')"
                ).fetchall()
            )
            for index_row in conn.execute("PRAGMA index_list(participant_days)").fetchall()
            if index_row["unique"]
        ]
    finally:
        conn.close()

    assert session_row is not None
    assert session_row["participant_day_id"] == 1
    assert session_row["joined_participant_day_id"] == 1
    assert session_row["attempt_id"] is not None
    assert session_row["participant_day_attempt_id"] is not None
    assert foreign_key_issues == []
    assert ("participant_id", "day_index") not in participant_day_unique_indexes
    assert (
        ("participant_id", "attempt_id", "day_index")
        in participant_day_unique_indexes
    )


def test_run_migrations_upgrades_pre_fix_participant_identity_schema(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO schema_migrations (version) VALUES
                ('001_initial'),
                ('003_attempt_flow'),
                ('006_clean_data_audits_attempt_scope');

            CREATE TABLE participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                participant_type TEXT NOT NULL,
                condition TEXT NOT NULL,
                subcondition TEXT NOT NULL,
                topic_key TEXT NOT NULL,
                error_type_id TEXT NOT NULL,
                target_days INTEGER NOT NULL,
                current_status TEXT NOT NULL,
                blocked_reason TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        run_migrations(conn)

        participant_row = (
            "Upgrade Path Participant",
            "test-phone-upgrade-0303",
            "hash-upgrade-0303",
            "short",
            "human",
            "qa",
            "topic-upgrade",
            "factual_minor",
            1,
            "active",
        )
        conn.execute(
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
            participant_row,
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
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
                participant_row,
            )

        migrations = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    assert [row["version"] for row in migrations] == [
        "001_initial",
        "002_procedure_alignment",
        "003_attempt_flow",
        "003_provider_cooldowns",
        "003_unique_participant_identity",
        "004_asr_attempts",
        "005_admin_controls",
        "006_clean_data_audits_attempt_scope",
        "007_external_operations",
        "008_cleanup_operations",
        "009_admin_login_security",
        "010_export_job_leases",
        "011_recruitment_control",
        "012_asr_result_references",
        "013_unified_recruitment",
        "014_error_semantic_evidence",
        "015_client_response_timing",
    ]


@pytest.mark.parametrize(
    "missing_table, setup_sql",
    [
        (
            "participants",
            """
            CREATE TABLE participant_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL
            );

            CREATE TABLE clean_data_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('eligible', 'review_needed', 'excluded')),
                reasons_json TEXT NOT NULL,
                reviewer_note TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """,
        ),
        (
            "participant_attempts",
            """
            CREATE TABLE participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                current_attempt_id INTEGER
            );

            CREATE TABLE clean_data_audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('eligible', 'review_needed', 'excluded')),
                reasons_json TEXT NOT NULL,
                reviewer_note TEXT,
                reviewed_by TEXT,
                reviewed_at TEXT,
                computed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """,
        ),
        (
            "clean_data_audits",
            """
            CREATE TABLE participants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                current_attempt_id INTEGER
            );

            CREATE TABLE participant_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL
            );
            """,
        ),
    ],
)
def test_run_migrations_rejects_missing_core_tables_before_clean_data_attempt_scope(
    sqlite_settings: Settings,
    missing_table: str,
    setup_sql: str,
):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.executescript(
            f"""
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO schema_migrations (version) VALUES
                ('001_initial'),
                ('002_procedure_alignment'),
                ('003_attempt_flow'),
                ('003_provider_cooldowns'),
                ('003_unique_participant_identity'),
                ('004_asr_attempts'),
                ('005_admin_controls');

            {setup_sql}
            """
        )

        with pytest.raises(
            RuntimeError,
            match=rf"006_clean_data_audits_attempt_scope.*missing core tables.*{missing_table}",
        ):
            run_migrations(conn)
    finally:
        conn.close()


def test_run_migrations_backfills_legacy_clean_data_audits_attempt_ids(sqlite_settings: Settings):
    from backend.app.db import MIGRATIONS_DIR, get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        conn.executescript(
            (MIGRATIONS_DIR / "001_initial.sql").read_text(encoding="utf-8")
        )
        conn.executescript(
            (MIGRATIONS_DIR / "002_procedure_alignment.sql").read_text(
                encoding="utf-8"
            )
        )
        conn.execute("ALTER TABLE participants ADD COLUMN current_attempt_id INTEGER")
        conn.execute(
            """
            CREATE TABLE participant_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                participant_id INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.executemany(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            [
                ("001_initial",),
                ("002_procedure_alignment",),
                ("003_attempt_flow",),
                ("003_provider_cooldowns",),
                ("003_unique_participant_identity",),
                ("004_asr_attempts",),
                ("005_admin_controls",),
            ],
        )
        conn.execute(
            """
            INSERT INTO participants (
                id,
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
                current_attempt_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "Legacy Audit Participant",
                "legacy-audit-phone",
                "legacy-audit-hash",
                "short",
                "human",
                "qa",
                "topic-legacy-audit",
                "factual_minor",
                1,
                "active",
                11,
            ),
        )
        conn.execute(
            "INSERT INTO participant_attempts (id, participant_id) VALUES (?, ?)",
            (11, 1),
        )
        conn.execute(
            """
            INSERT INTO clean_data_audits (
                id,
                participant_id,
                status,
                reasons_json,
                reviewer_note,
                reviewed_by,
                reviewed_at,
                computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                3,
                1,
                "eligible",
                '["legacy_reason"]',
                "legacy note",
                "admin",
                "2026-07-03T09:00:00+08:00",
                "2026-07-03T08:00:00+08:00",
            ),
        )
        _create_legacy_admin_global_controls(conn)

        run_migrations(conn)

        audit_row = conn.execute(
            """
            SELECT
                id,
                participant_id,
                attempt_id,
                status,
                reasons_json,
                reviewer_note,
                reviewed_by,
                reviewed_at,
                computed_at
            FROM clean_data_audits
            WHERE id = 3
            """
        ).fetchone()
    finally:
        conn.close()

    assert audit_row is not None
    assert audit_row["participant_id"] == 1
    assert audit_row["attempt_id"] == 11
    assert audit_row["status"] == "eligible"
    assert audit_row["reasons_json"] == '["legacy_reason"]'
    assert audit_row["reviewer_note"] == "legacy note"


def test_run_migrations_rolls_back_failed_migration(sqlite_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from backend.app import db

    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_broken.sql").write_text(
        """
        CREATE TABLE broken_table (
            id INTEGER PRIMARY KEY
        );

        INSERT INTO missing_table (value) VALUES (1);
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(db, "MIGRATIONS_DIR", migrations_dir)

    conn = db.get_connection(sqlite_settings)
    try:
        with pytest.raises(sqlite3.Error):
            db.run_migrations(conn)

        broken_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'broken_table'"
        ).fetchone()
        recorded_versions = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()

    assert broken_table is None
    assert recorded_versions == []


def test_participants_target_days_constraint_rejects_invalid_combinations(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        valid_rows = [
            (
                "Short Participant",
                "phone-short",
                "hash-short",
                "short",
                "human",
                "qa",
                "topic-short",
                "factual_minor",
                1,
                "active",
            ),
            (
                "Long Participant",
                "phone-long",
                "hash-long",
                "long",
                "tool",
                "planning",
                "topic-long",
                "logic_major",
                3,
                "active",
            ),
        ]

        for row in valid_rows:
            conn.execute(
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
                row,
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
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
                    "Invalid Short",
                    "phone-invalid-short",
                    "hash-invalid-short",
                    "short",
                    "human",
                    "chat",
                    "topic-invalid-short",
                    "social_minor",
                    3,
                    "active",
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
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
                    "Invalid Long",
                    "phone-invalid-long",
                    "hash-invalid-long",
                    "long",
                    "tool",
                    "decision",
                    "topic-invalid-long",
                    "social_major",
                    1,
                    "active",
                ),
            )

        participant_types = conn.execute(
            "SELECT participant_type, target_days FROM participants ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert [(row["participant_type"], row["target_days"]) for row in participant_types] == [
        ("short", 1),
        ("long", 3),
    ]


def test_participants_enforce_unique_name_phone_identity(sqlite_settings: Settings):
    from backend.app.db import get_connection, run_migrations

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)

        row = (
            "Duplicate Person",
            "test-phone-identity-0202",
            "hash-identity-0202",
            "short",
            "human",
            "qa",
            "topic-identity",
            "factual_minor",
            1,
            "active",
        )
        conn.execute(
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
            row,
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
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
                row,
            )
    finally:
        conn.close()
