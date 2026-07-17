from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Callable, Iterator, Optional
from urllib.parse import unquote, urlparse

from backend.app.settings import Settings, get_settings


MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
MIGRATIONS_REQUIRING_FOREIGN_KEYS_OFF = {"003_attempt_flow"}
_POST_COMMIT_HOOKS: dict[int, list[Callable[[], None]]] = {}


class ReadOnlyDatabaseError(RuntimeError):
    pass


def _sqlite_path_from_url(database_url: str) -> Path:
    parsed = urlparse(database_url)
    if parsed.scheme != "sqlite":
        raise ValueError("Only sqlite database URLs are supported.")

    if parsed.netloc not in ("", "localhost"):
        raise ValueError("SQLite database URLs must use a local filesystem path.")

    raw_path = unquote(parsed.path)
    if not raw_path:
        raise ValueError("SQLite database URL must include a filesystem path.")

    if parsed.netloc == "localhost" and not raw_path.startswith("/"):
        raw_path = f"/{raw_path}"

    return Path(raw_path)


def _configure_connection(
    conn: sqlite3.Connection,
    *,
    journal_mode: str | None,
) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    if journal_mode is not None:
        conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_connection(settings: Optional[Settings] = None) -> sqlite3.Connection:
    app_settings = settings or get_settings()
    db_path = _sqlite_path_from_url(app_settings.database_url)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    return _configure_connection(conn, journal_mode="WAL")


def get_read_only_connection(settings: Optional[Settings] = None) -> sqlite3.Connection:
    app_settings = settings or get_settings()
    db_path = _sqlite_path_from_url(app_settings.database_url)
    if not db_path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {db_path}")

    wal_path = db_path.parent / f"{db_path.name}-wal"
    shm_path = db_path.parent / f"{db_path.name}-shm"
    existing_sidecars = [path.name for path in (wal_path, shm_path) if path.exists()]
    if existing_sidecars:
        sidecar_list = ", ".join(existing_sidecars)
        raise ReadOnlyDatabaseError(
            "SQLite WAL sidecars are present for "
            f"{db_path}: {sidecar_list}. Checkpoint or close writers before running cleanup dry-run."
        )

    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro&immutable=1",
        isolation_level=None,
        uri=True,
    )
    return _configure_connection(conn, journal_mode=None)


def get_live_read_only_connection(
    settings: Optional[Settings] = None,
) -> sqlite3.Connection:
    app_settings = settings or get_settings()
    db_path = _sqlite_path_from_url(app_settings.database_url)
    if not db_path.exists():
        raise FileNotFoundError("SQLite database does not exist.")

    conn = sqlite3.connect(
        f"{db_path.resolve().as_uri()}?mode=ro",
        isolation_level=None,
        uri=True,
    )
    return _configure_connection(conn, journal_mode=None)


def _connection_key(conn: sqlite3.Connection) -> int:
    return id(conn)


def add_post_commit_hook(
    conn: sqlite3.Connection,
    hook: Callable[[], None],
) -> None:
    if not conn.in_transaction:
        hook()
        return

    _POST_COMMIT_HOOKS.setdefault(_connection_key(conn), []).append(hook)


def _pop_post_commit_hooks(conn: sqlite3.Connection) -> list[Callable[[], None]]:
    return _POST_COMMIT_HOOKS.pop(_connection_key(conn), [])


def _clear_post_commit_hooks(conn: sqlite3.Connection) -> None:
    _POST_COMMIT_HOOKS.pop(_connection_key(conn), None)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        _clear_post_commit_hooks(conn)
        raise
    else:
        try:
            conn.commit()
        except Exception:
            conn.rollback()
            _clear_post_commit_hooks(conn)
            raise

        for hook in _pop_post_commit_hooks(conn):
            hook()


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _ensure_schema_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _iter_sql_statements(migration_sql: str) -> Iterator[str]:
    buffer = ""
    for line in migration_sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                yield statement
            buffer = ""

    trailing = buffer.strip()
    if trailing:
        yield trailing


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _ensure_turn_ratings_stance_column(conn: sqlite3.Connection) -> None:
    columns = _get_table_columns(conn, "turn_ratings")
    if not columns or "stance_score" in columns:
        return

    for legacy_column in ("impression_score", "perception_score"):
        if legacy_column in columns:
            with transaction(conn):
                conn.execute(
                    f"ALTER TABLE turn_ratings RENAME COLUMN {legacy_column} TO stance_score"
                )
            return

    raise RuntimeError("turn_ratings table is missing stance_score column.")


def _prepare_recruitment_control_migration(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "admin_events"):
        return
    conn.execute(
        """
        CREATE TABLE admin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN (
                'login',
                'update_assignment_cap',
                'block_participant',
                'export_data',
                'test_agent'
            )),
            target_type TEXT,
            target_id TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX idx_admin_events_created_at ON admin_events (created_at)"
    )


def _prepare_asr_result_references_migration(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "asr_attempts"):
        conn.execute(
            """
            CREATE TABLE asr_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                turn_index INTEGER NOT NULL CHECK (turn_index BETWEEN 1 AND 5),
                attempt_no INTEGER NOT NULL CHECK (attempt_no >= 1),
                user_audio_path TEXT NOT NULL,
                user_audio_sha256 TEXT NOT NULL,
                asr_provider TEXT,
                asr_status TEXT NOT NULL CHECK (
                    asr_status IN ('success', 'failed', 'timeout')
                ),
                asr_text TEXT,
                asr_latency_ms INTEGER CHECK (asr_latency_ms IS NULL OR asr_latency_ms >= 0),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (session_id, turn_index, attempt_no),
                FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_asr_attempts_session_turn "
            "ON asr_attempts (session_id, turn_index, id)"
        )
    if "result_ref" not in _get_table_columns(conn, "asr_attempts"):
        conn.execute("ALTER TABLE asr_attempts ADD COLUMN result_ref TEXT")


def _prepare_clean_data_audits_attempt_scope_migration(
    conn: sqlite3.Connection,
) -> None:
    required_tables = ("participants", "participant_attempts", "clean_data_audits")
    missing_tables = [
        table_name for table_name in required_tables if not _table_exists(conn, table_name)
    ]
    if missing_tables:
        missing_tables_sql = ", ".join(missing_tables)
        raise RuntimeError(
            "Invalid migration state before 006_clean_data_audits_attempt_scope: "
            f"missing core tables: {missing_tables_sql}"
        )

    columns = _get_table_columns(conn, "clean_data_audits")
    if "attempt_id" in columns:
        return

    with transaction(conn):
        conn.execute(
            """
            ALTER TABLE clean_data_audits
            ADD COLUMN attempt_id INTEGER REFERENCES participant_attempts(id) ON DELETE CASCADE
            """
        )

        if not _table_exists(conn, "participants"):
            return

        participant_columns = _get_table_columns(conn, "participants")
        if "current_attempt_id" not in participant_columns:
            return

        conn.execute(
            """
            UPDATE clean_data_audits
            SET attempt_id = (
                SELECT p.current_attempt_id
                FROM participants p
                WHERE p.id = clean_data_audits.participant_id
            )
            WHERE attempt_id IS NULL
            """
        )


def _prepare_external_operations_migration(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "api_call_logs"):
        return

    columns = _get_table_columns(conn, "api_call_logs")
    additions = {
        "session_id": (
            "ALTER TABLE api_call_logs ADD COLUMN session_id INTEGER "
            "REFERENCES experiment_sessions(id) ON DELETE CASCADE"
        ),
        "turn_index": (
            "ALTER TABLE api_call_logs ADD COLUMN turn_index INTEGER "
            "CHECK (turn_index IS NULL OR turn_index BETWEEN 1 AND 5)"
        ),
        "is_test": (
            "ALTER TABLE api_call_logs ADD COLUMN is_test INTEGER "
            "CHECK (is_test IS NULL OR is_test IN (0, 1))"
        ),
    }
    with transaction(conn):
        for column_name, statement in additions.items():
            if column_name not in columns:
                conn.execute(statement)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_call_logs_session_turn
            ON api_call_logs (session_id, turn_index, route)
            """
        )


def _ensure_external_operation_result_entity_column(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "external_operations"):
        return
    if "result_entity_id" in _get_table_columns(conn, "external_operations"):
        return
    with transaction(conn):
        conn.execute(
            "ALTER TABLE external_operations ADD COLUMN result_entity_id INTEGER"
        )


def _ensure_external_operation_scope(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "external_operations"):
        return
    required_columns = {
        "id",
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
        "created_at",
        "updated_at",
    }
    if not required_columns.issubset(
        _get_table_columns(conn, "external_operations")
    ):
        return

    has_legacy_unique_constraint = False
    for index_row in conn.execute(
        "PRAGMA index_list(external_operations)"
    ).fetchall():
        if not bool(index_row["unique"]):
            continue
        index_columns = tuple(
            str(row["name"])
            for row in conn.execute(
                f"PRAGMA index_info('{index_row['name']}')"
            ).fetchall()
        )
        if index_columns == ("participant_id", "kind", "operation_id"):
            has_legacy_unique_constraint = True
            break

    if has_legacy_unique_constraint:
        with transaction(conn):
            conn.execute(
                "ALTER TABLE external_operations RENAME TO external_operations_legacy_scope"
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
                    kind TEXT NOT NULL CHECK (kind IN ('turn', 'asr')),
                    turn_index INTEGER NOT NULL CHECK (turn_index BETWEEN 1 AND 5),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
                    result_entity_id INTEGER,
                    result_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (participant_id) REFERENCES participants(id) ON DELETE CASCADE,
                    FOREIGN KEY (attempt_id) REFERENCES participant_attempts(id) ON DELETE CASCADE,
                    FOREIGN KEY (session_id) REFERENCES experiment_sessions(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                INSERT INTO external_operations (
                    id,
                    operation_id,
                    request_fingerprint,
                    participant_id,
                    attempt_id,
                    session_id,
                    kind,
                    turn_index,
                    status,
                    result_entity_id,
                    result_json,
                    error_json,
                    created_at,
                    updated_at
                )
                SELECT
                    id,
                    operation_id,
                    request_fingerprint,
                    participant_id,
                    attempt_id,
                    session_id,
                    kind,
                    turn_index,
                    status,
                    result_entity_id,
                    result_json,
                    error_json,
                    created_at,
                    updated_at
                FROM external_operations_legacy_scope
                """
            )
            conn.execute("DROP TABLE external_operations_legacy_scope")

    with transaction(conn):
        conn.execute("DROP INDEX IF EXISTS idx_external_operations_scope")
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_external_operations_scope
            ON external_operations (
                participant_id,
                COALESCE(attempt_id, 0),
                session_id,
                kind,
                turn_index,
                operation_id
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_external_operations_status
            ON external_operations (status, updated_at)
            """
        )


def _ensure_unique_converted_attempt_source(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "participant_attempts"):
        return
    if "source_attempt_id" not in _get_table_columns(conn, "participant_attempts"):
        return
    with transaction(conn):
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_participant_attempts_unique_source
            ON participant_attempts (source_attempt_id)
            WHERE source_attempt_id IS NOT NULL
            """
        )


def _ensure_cleanup_operation_schema(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "cleanup_operations"):
        return
    columns = _get_table_columns(conn, "cleanup_operations")
    if "operation_kind" in columns:
        with transaction(conn):
            if "worker_token" not in columns:
                conn.execute("ALTER TABLE cleanup_operations ADD COLUMN worker_token TEXT")
            if "lease_expires_at" not in columns:
                conn.execute(
                    "ALTER TABLE cleanup_operations ADD COLUMN lease_expires_at TEXT"
                )
            if not _table_exists(conn, "cleanup_operation_owners"):
                _create_cleanup_operation_owners_table(conn)
        return

    with transaction(conn):
        conn.execute(
            "ALTER TABLE cleanup_operations RENAME TO cleanup_operations_legacy"
        )
        conn.execute(
            """
            CREATE TABLE cleanup_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                attempt_id INTEGER NOT NULL,
                operation_kind TEXT NOT NULL CHECK (operation_kind IN ('relocate', 'delete')),
                source_path TEXT NOT NULL,
                staging_path TEXT UNIQUE,
                destination_path TEXT,
                expected_sha256 TEXT,
                preserve_source INTEGER NOT NULL DEFAULT 0 CHECK (preserve_source IN (0, 1)),
                worker_token TEXT,
                lease_expires_at TEXT,
                state TEXT NOT NULL CHECK (
                    state IN (
                        'planned', 'staged', 'database_committed', 'completed',
                        'rolled_back', 'review_needed'
                    )
                ),
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (attempt_id) REFERENCES participant_attempts(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            INSERT INTO cleanup_operations (
                id, attempt_id, operation_kind, source_path, staging_path,
                destination_path, expected_sha256, preserve_source,
                worker_token, lease_expires_at, state,
                last_error, created_at, updated_at
            )
            SELECT
                id, attempt_id, 'relocate', source_path, staging_path,
                destination_path, expected_sha256, 0, NULL, NULL,
                CASE
                    WHEN state IN ('completed', 'rolled_back') THEN state
                    ELSE 'review_needed'
                END,
                CASE
                    WHEN state IN ('completed', 'rolled_back') THEN last_error
                    ELSE 'legacy_owner_metadata_missing'
                END,
                created_at, updated_at
            FROM cleanup_operations_legacy
            """
        )
        conn.execute("DROP TABLE cleanup_operations_legacy")
        _create_cleanup_operation_owners_table(conn)
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cleanup_operations_state
            ON cleanup_operations (state, operation_kind, id)
            """
        )


def _create_cleanup_operation_owners_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cleanup_operation_owners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            operation_id INTEGER NOT NULL,
            owner_table TEXT NOT NULL CHECK (
                owner_table IN ('conversation_turns', 'asr_attempts')
            ),
            owner_row_id INTEGER NOT NULL,
            owner_field TEXT NOT NULL CHECK (owner_field = 'user_audio_path'),
            original_path TEXT NOT NULL,
            destination_path TEXT,
            original_sha256 TEXT,
            FOREIGN KEY (operation_id) REFERENCES cleanup_operations(id) ON DELETE CASCADE,
            UNIQUE (operation_id, owner_table, owner_row_id, owner_field)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_cleanup_operation_owners_operation
        ON cleanup_operation_owners (operation_id, id)
        """
    )


def _format_foreign_key_issues(foreign_key_issues: list[sqlite3.Row]) -> str:
    return ", ".join(
        f"{row['table']}:{row['rowid']}->{row['parent']}"
        for row in foreign_key_issues[:5]
    )


def _run_migration_with_foreign_keys_disabled(
    conn: sqlite3.Connection, version: str, migration_sql: str
) -> None:
    foreign_keys_enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    if foreign_keys_enabled:
        conn.execute("PRAGMA foreign_keys=OFF")

    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in _iter_sql_statements(migration_sql):
                conn.execute(statement)

            foreign_key_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_issues:
                raise RuntimeError(
                    f"Migration {version} failed foreign key validation: "
                    f"{_format_foreign_key_issues(foreign_key_issues)}"
                )

            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (version,),
            )
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
    finally:
        if foreign_keys_enabled:
            conn.execute("PRAGMA foreign_keys=ON")


def expected_migration_versions() -> tuple[str, ...]:
    return tuple(path.stem for path in sorted(MIGRATIONS_DIR.glob("*.sql")))


def migration_state_is_current(conn: sqlite3.Connection) -> bool:
    if not _table_exists(conn, "schema_migrations"):
        return False
    applied_versions = tuple(
        sorted(
            str(row["version"])
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        )
    )
    return applied_versions == tuple(sorted(expected_migration_versions()))


def probe_database_read_write(conn: sqlite3.Connection) -> None:
    conn.execute("SELECT 1").fetchone()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            CREATE TABLE readiness_write_probe (
                probe_value INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO readiness_write_probe (probe_value) VALUES (1)"
        )
    finally:
        conn.rollback()


def run_migrations(conn: sqlite3.Connection) -> None:
    _ensure_schema_migrations_table(conn)

    applied_versions = {
        row["version"]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }

    for migration_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = migration_path.stem
        if version in applied_versions:
            continue

        if version == "006_clean_data_audits_attempt_scope":
            _prepare_clean_data_audits_attempt_scope_migration(conn)
        if version == "007_external_operations":
            _prepare_external_operations_migration(conn)
        if version == "011_recruitment_control":
            _prepare_recruitment_control_migration(conn)
        if version == "012_asr_result_references":
            _prepare_asr_result_references_migration(conn)
        if version == "014_error_semantic_evidence":
            with transaction(conn):
                _apply_error_semantic_evidence_migration(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
            applied_versions.add(version)
            continue
        if version == "015_client_response_timing":
            with transaction(conn):
                _apply_client_response_timing_migration(conn)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
            applied_versions.add(version)
            continue

        migration_sql = migration_path.read_text(encoding="utf-8")
        if version in MIGRATIONS_REQUIRING_FOREIGN_KEYS_OFF:
            _run_migration_with_foreign_keys_disabled(conn, version, migration_sql)
        else:
            with transaction(conn):
                for statement in _iter_sql_statements(migration_sql):
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES (?)",
                    (version,),
                )
        applied_versions.add(version)

    _ensure_turn_ratings_stance_column(conn)
    _prepare_external_operations_migration(conn)
    _ensure_external_operation_result_entity_column(conn)
    _ensure_external_operation_scope(conn)
    _ensure_cleanup_operation_schema(conn)
    _ensure_unique_converted_attempt_source(conn)
    _prepare_asr_result_references_migration(conn)


def _apply_error_semantic_evidence_migration(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "experiment_sessions"):
        session_columns = _get_table_columns(conn, "experiment_sessions")
        if "manipulation_status" not in session_columns:
            conn.execute(
                """
                ALTER TABLE experiment_sessions
                ADD COLUMN manipulation_status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (manipulation_status IN ('unknown', 'pending', 'presented', 'failed'))
                """
            )
    if not _table_exists(conn, "conversation_turns"):
        return
    turn_columns = _get_table_columns(conn, "conversation_turns")
    statements = {
        "error_mutation_json": (
            "ALTER TABLE conversation_turns ADD COLUMN error_mutation_json TEXT"
        ),
        "error_semantic_attempt_count": (
            "ALTER TABLE conversation_turns "
            "ADD COLUMN error_semantic_attempt_count INTEGER NOT NULL DEFAULT 0 "
            "CHECK (error_semantic_attempt_count BETWEEN 0 AND 5)"
        ),
        "error_failure_reason": (
            "ALTER TABLE conversation_turns ADD COLUMN error_failure_reason TEXT"
        ),
        "error_attempts_json": (
            "ALTER TABLE conversation_turns ADD COLUMN error_attempts_json TEXT"
        ),
    }
    for column_name, statement in statements.items():
        if column_name not in turn_columns:
            conn.execute(statement)


def _apply_client_response_timing_migration(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "conversation_turns"):
        return
    turn_columns = _get_table_columns(conn, "conversation_turns")
    statements = {
        "client_message_sent_at": (
            "ALTER TABLE conversation_turns ADD COLUMN client_message_sent_at TEXT"
        ),
        "assistant_render_completed_at": (
            "ALTER TABLE conversation_turns "
            "ADD COLUMN assistant_render_completed_at TEXT"
        ),
        "client_response_latency_ms": (
            "ALTER TABLE conversation_turns "
            "ADD COLUMN client_response_latency_ms INTEGER "
            "CHECK (client_response_latency_ms IS NULL "
            "OR client_response_latency_ms >= 0)"
        ),
        "client_timing_interrupted": (
            "ALTER TABLE conversation_turns "
            "ADD COLUMN client_timing_interrupted INTEGER "
            "CHECK (client_timing_interrupted IS NULL "
            "OR client_timing_interrupted IN (0, 1))"
        ),
        "render_timing_received_at": (
            "ALTER TABLE conversation_turns ADD COLUMN render_timing_received_at TEXT"
        ),
    }
    for column_name, statement in statements.items():
        if column_name not in turn_columns:
            conn.execute(statement)
