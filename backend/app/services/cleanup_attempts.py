from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from backend.app.db import transaction
from backend.app.repositories.attempts import (
    create_attempt,
    get_attempt_by_id,
    get_attempt_by_source_attempt_id,
    set_current_attempt,
    update_attempt_status,
)
from backend.app.repositories.participants import (
    create_participant_days,
    update_participant_day_status,
)
from backend.app.repositories.sessions import (
    delete_sessions_by_ids,
    list_incomplete_formal_sessions_for_attempt,
)
from backend.app.services.attempts import CleanupResult
from backend.app.services.file_naming import canonical_audio_relative_path


@dataclass(frozen=True)
class ConvertibleAttempt:
    participant_id: int
    source_attempt_id: int
    missed_day_indexes: list[int]


@dataclass(frozen=True)
class CleanupPlan:
    today: str
    scanned_attempts: int
    convertible_attempts: list[ConvertibleAttempt] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True)
class CleanupSummary:
    scanned_attempts: int
    converted_attempts: int
    deleted_sessions: int
    skipped: list[dict[str, object]]
    deleted_audio_paths: list[str] = field(default_factory=list)
    failed_audio_paths: list[str] = field(default_factory=list)

    @property
    def deleted_audio_files(self) -> int:
        return len(self.deleted_audio_paths)


@dataclass(frozen=True)
class AudioOwner:
    owner_table: str
    owner_row_id: int
    owner_field: str
    original_path: str
    destination_path: str | None
    original_sha256: str | None


@dataclass(frozen=True)
class AudioOperation:
    operation_id: int
    attempt_id: int
    operation_kind: str
    source_path: str
    staging_path: str | None
    destination_path: str | None
    expected_sha256: str | None
    preserve_source: bool
    worker_token: str | None
    lease_expires_at: str | None
    state: str
    last_error: str | None
    owners: tuple[AudioOwner, ...]


class AudioRelocationError(RuntimeError):
    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{reason}: {path}")


class CleanupEligibilityChanged(RuntimeError):
    pass


class CleanupClaimLost(RuntimeError):
    pass


class AudioFilesystemError(OSError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CleanupReconciliationError(RuntimeError):
    def __init__(self, operations: list[dict[str, object]]) -> None:
        self.operations = operations
        super().__init__(f"Unresolved cleanup operations: {operations}")


_DELETION_REVIEW_ERRORS = {
    "delete_hash_mismatch",
    "delete_path_outside_root",
    "delete_source_identity_changed",
    "delete_source_nonregular",
    "delete_source_symlink",
}


def plan_attempt_cleanup(
    conn: sqlite3.Connection,
    *,
    today: str,
    data_dir: Path,
) -> CleanupPlan:
    del data_dir
    today_iso = date.fromisoformat(today).isoformat()
    rows = conn.execute(
        """
        SELECT *
        FROM participant_attempts
        WHERE status = 'active'
          AND participant_type = 'long'
        ORDER BY id
        """
    ).fetchall()
    convertible_attempts: list[ConvertibleAttempt] = []
    skipped: list[dict[str, object]] = []

    for attempt_row in rows:
        attempt_id = int(attempt_row["id"])
        convertible, missed_day_indexes, reason = _cleanup_eligibility(
            conn,
            attempt_id=attempt_id,
            today=today_iso,
        )
        if not convertible:
            skipped.append({"attempt_id": attempt_id, "reason": reason})
            continue
        convertible_attempts.append(
            ConvertibleAttempt(
                participant_id=int(attempt_row["participant_id"]),
                source_attempt_id=attempt_id,
                missed_day_indexes=missed_day_indexes,
            )
        )

    return CleanupPlan(
        today=today_iso,
        scanned_attempts=len(rows),
        convertible_attempts=convertible_attempts,
        skipped=skipped,
    )


def _cleanup_eligibility(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    today: str,
) -> tuple[bool, list[int], str]:
    attempt_row = get_attempt_by_id(conn, attempt_id=attempt_id)
    if attempt_row is None:
        return False, [], "source_attempt_missing"
    if str(attempt_row["status"]) != "active":
        return False, [], "source_attempt_not_active"
    if str(attempt_row["participant_type"]) != "long":
        return False, [], "source_attempt_not_long"

    day_rows = conn.execute(
        """
        SELECT day_index, calendar_date, status
        FROM participant_days
        WHERE attempt_id = ?
        ORDER BY day_index
        """,
        (attempt_id,),
    ).fetchall()
    day_status = {int(row["day_index"]): str(row["status"]) for row in day_rows}
    if day_status.get(1) != "completed":
        return False, [], "day_one_not_completed"
    missed_day_indexes = [
        int(row["day_index"])
        for row in day_rows
        if int(row["day_index"]) in {2, 3}
        and str(row["calendar_date"]) < today
        and str(row["status"]) != "completed"
    ]
    if not missed_day_indexes:
        return False, [], "no_missed_days"
    return True, missed_day_indexes, "eligible"


def apply_attempt_cleanup(
    conn: sqlite3.Connection,
    *,
    plan: CleanupPlan,
    data_dir: Path,
) -> CleanupSummary:
    if conn.in_transaction:
        raise RuntimeError("apply_attempt_cleanup must own its database transactions")

    converted_attempts = 0
    deleted_sessions = 0
    deleted_audio_paths: list[str] = []
    failed_audio_paths: list[str] = []
    skipped = list(plan.skipped)

    for convertible_attempt in plan.convertible_attempts:
        worker_token = uuid.uuid4().hex
        source_attempt = get_attempt_by_id(
            conn,
            attempt_id=convertible_attempt.source_attempt_id,
        )
        if source_attempt is None or str(source_attempt["status"]) != "active":
            skipped.append(
                {
                    "attempt_id": convertible_attempt.source_attempt_id,
                    "reason": "source_attempt_not_active",
                }
            )
            continue
        if get_attempt_by_source_attempt_id(
            conn,
            source_attempt_id=convertible_attempt.source_attempt_id,
        ) is not None:
            skipped.append(
                {
                    "attempt_id": convertible_attempt.source_attempt_id,
                    "reason": "converted_attempt_already_exists",
                }
            )
            continue

        try:
            relocations = _prepare_audio_relocations(
                conn,
                data_dir=data_dir,
                participant_id=convertible_attempt.participant_id,
                source_attempt_id=convertible_attempt.source_attempt_id,
                worker_token=worker_token,
            )
        except AudioRelocationError as exc:
            failed_audio_paths.append(exc.path)
            skipped.append(
                {
                    "attempt_id": convertible_attempt.source_attempt_id,
                    "reason": exc.reason,
                    "audio_path": exc.path,
                }
            )
            continue

        staging_failure = _stage_audio_relocations(
            conn,
            data_dir=data_dir,
            relocations=relocations,
        )
        if staging_failure is not None:
            failed_audio_paths.append(staging_failure)
            skipped.append(
                {
                    "attempt_id": convertible_attempt.source_attempt_id,
                    "reason": "audio_stage_failed",
                    "audio_path": staging_failure,
                }
            )
            continue

        try:
            deletion_operations, session_ids = _prepare_audio_deletions(
                conn,
                data_dir=data_dir,
                source_attempt_id=convertible_attempt.source_attempt_id,
                worker_token=worker_token,
            )
        except Exception:
            _restore_staged_relocations(
                conn,
                data_dir=data_dir,
                relocations=relocations,
            )
            raise

        failed_audio_paths.extend(
            operation.source_path
            for operation in deletion_operations
            if operation.state == "review_needed"
        )
        try:
            with transaction(conn):
                cleanup_result = _commit_converted_attempt(
                    conn,
                    today=plan.today,
                    data_dir=data_dir,
                    convertible_attempt=convertible_attempt,
                    source_attempt=source_attempt,
                    relocations=relocations,
                    deletion_operations=deletion_operations,
                    session_ids=session_ids,
                )
        except CleanupEligibilityChanged as exc:
            _restore_staged_relocations(
                conn,
                data_dir=data_dir,
                relocations=relocations,
            )
            _roll_back_planned_deletions(conn, deletion_operations)
            skipped.append(
                {
                    "attempt_id": convertible_attempt.source_attempt_id,
                    "reason": str(exc),
                }
            )
            continue
        except Exception:
            _restore_staged_relocations(
                conn,
                data_dir=data_dir,
                relocations=relocations,
            )
            _roll_back_planned_deletions(conn, deletion_operations)
            raise

        deleted_sessions += len(cleanup_result.deleted_session_ids)
        _finalize_audio_relocations(
            conn,
            data_dir=data_dir,
            relocations=relocations,
            failed_audio_paths=failed_audio_paths,
        )
        _finalize_audio_deletions(
            conn,
            data_dir=data_dir,
            operations=deletion_operations,
            deleted_audio_paths=deleted_audio_paths,
            failed_audio_paths=failed_audio_paths,
        )
        converted_attempts += 1

    return CleanupSummary(
        scanned_attempts=plan.scanned_attempts,
        converted_attempts=converted_attempts,
        deleted_sessions=deleted_sessions,
        skipped=skipped,
        deleted_audio_paths=deleted_audio_paths,
        failed_audio_paths=failed_audio_paths,
    )


def _commit_converted_attempt(
    conn: sqlite3.Connection,
    *,
    today: str,
    data_dir: Path,
    convertible_attempt: ConvertibleAttempt,
    source_attempt: sqlite3.Row,
    relocations: list[AudioOperation],
    deletion_operations: list[AudioOperation],
    session_ids: list[int],
) -> CleanupResult:
    del data_dir
    eligible, _, reason = _cleanup_eligibility(
        conn,
        attempt_id=convertible_attempt.source_attempt_id,
        today=today,
    )
    if not eligible:
        raise CleanupEligibilityChanged(reason)
    if get_attempt_by_source_attempt_id(
        conn,
        source_attempt_id=convertible_attempt.source_attempt_id,
    ) is not None:
        raise CleanupEligibilityChanged("converted_attempt_already_exists")

    delete_sessions_by_ids(conn, session_ids=session_ids)
    update_attempt_status(
        conn,
        attempt_id=convertible_attempt.source_attempt_id,
        status="converted_to_short",
        valid_for_export=False,
        blocked_reason="long_term_missed_day",
    )
    converted_attempt_id = create_attempt(
        conn,
        participant_id=convertible_attempt.participant_id,
        participant_type="short",
        condition=str(source_attempt["condition"]),
        subcondition=str(source_attempt["subcondition"]),
        topic_key=str(source_attempt["topic_key"]),
        error_type_id=str(source_attempt["error_type_id"]),
        target_days=1,
        status="completed",
        valid_for_export=True,
        source_attempt_id=convertible_attempt.source_attempt_id,
    )
    set_current_attempt(
        conn,
        participant_id=convertible_attempt.participant_id,
        attempt_id=converted_attempt_id,
    )
    _create_completed_converted_day(
        conn,
        participant_id=convertible_attempt.participant_id,
        source_attempt_id=convertible_attempt.source_attempt_id,
        converted_attempt_id=converted_attempt_id,
    )

    for relocation in relocations:
        for owner in relocation.owners:
            _update_owned_audio_path(
                conn,
                owner=owner,
                expected_path=owner.original_path,
                replacement_path=str(relocation.destination_path),
            )
        _set_cleanup_operation_state(
            conn,
            operation_id=relocation.operation_id,
            state="database_committed",
            worker_token=relocation.worker_token,
            expected_states=("staged",),
        )
    for operation in deletion_operations:
        if operation.state == "planned":
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="database_committed",
                worker_token=operation.worker_token,
                expected_states=("planned",),
            )

    return CleanupResult(
        deleted_session_ids=session_ids,
        audio_paths_to_delete=[operation.source_path for operation in deletion_operations],
        deleted_audio_paths=[],
        failed_audio_paths=[],
    )


def _update_owned_audio_path(
    conn: sqlite3.Connection,
    *,
    owner: AudioOwner,
    expected_path: str,
    replacement_path: str,
) -> None:
    if owner.owner_table not in {"conversation_turns", "asr_attempts"}:
        raise RuntimeError("Invalid cleanup owner table.")
    cursor = conn.execute(
        f"""
        UPDATE {owner.owner_table}
        SET user_audio_path = ?
        WHERE id = ?
          AND user_audio_path = ?
          AND user_audio_sha256 IS ?
        """,
        (
            replacement_path,
            owner.owner_row_id,
            expected_path,
            owner.original_sha256,
        ),
    )
    if cursor.rowcount != 1:
        raise CleanupEligibilityChanged("audio_owner_changed")


def _create_completed_converted_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    source_attempt_id: int,
    converted_attempt_id: int,
) -> None:
    source_day_one = conn.execute(
        """
        SELECT calendar_date, completed_at
        FROM participant_days
        WHERE attempt_id = ? AND day_index = 1
        """,
        (source_attempt_id,),
    ).fetchone()
    if source_day_one is None:
        raise LookupError("Source long attempt is missing Day 1.")
    start_date = date.fromisoformat(str(source_day_one["calendar_date"]))
    create_participant_days(
        conn,
        participant_id=participant_id,
        attempt_id=converted_attempt_id,
        target_days=1,
        start_date=start_date,
    )
    converted_day_one = conn.execute(
        "SELECT id FROM participant_days WHERE attempt_id = ? AND day_index = 1",
        (converted_attempt_id,),
    ).fetchone()
    if converted_day_one is None:
        raise LookupError("Failed to create converted short Day 1 row.")
    update_participant_day_status(
        conn,
        participant_day_id=int(converted_day_one["id"]),
        status="completed",
        completed_at=str(source_day_one["completed_at"]),
    )


def _prepare_audio_relocations(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    participant_id: int,
    source_attempt_id: int,
    worker_token: str,
) -> list[AudioOperation]:
    participant_row = conn.execute(
        "SELECT name, phone FROM participants WHERE id = ?",
        (participant_id,),
    ).fetchone()
    if participant_row is None:
        return []
    rows = conn.execute(
        """
        SELECT * FROM (
            SELECT 'conversation_turns' AS owner_table, t.id AS owner_row_id,
                   t.user_audio_path, t.user_audio_sha256, t.turn_index,
                   s.session_uuid, s.id AS session_id, 0 AS source_order
            FROM conversation_turns t
            JOIN experiment_sessions s ON s.id = t.session_id
            JOIN participant_days d ON d.id = s.participant_day_id
            WHERE s.participant_id = ? AND s.attempt_id = ? AND s.is_test = 0
              AND s.status = 'completed' AND d.day_index = 1
              AND d.status = 'completed' AND t.user_audio_path IS NOT NULL
              AND t.user_audio_path != ''
            UNION ALL
            SELECT 'asr_attempts' AS owner_table, a.id AS owner_row_id,
                   a.user_audio_path, a.user_audio_sha256, a.turn_index,
                   s.session_uuid, s.id AS session_id, 1 AS source_order
            FROM asr_attempts a
            JOIN experiment_sessions s ON s.id = a.session_id
            JOIN participant_days d ON d.id = s.participant_day_id
            WHERE s.participant_id = ? AND s.attempt_id = ? AND s.is_test = 0
              AND s.status = 'completed' AND d.day_index = 1
              AND d.status = 'completed' AND a.user_audio_path IS NOT NULL
              AND a.user_audio_path != ''
        ) ORDER BY session_id, turn_index, source_order, owner_row_id
        """,
        (participant_id, source_attempt_id, participant_id, source_attempt_id),
    ).fetchall()
    grouped = _group_audio_rows(rows)
    targets: dict[str, str] = {}
    reserved_targets: set[str] = set()
    operations: list[AudioOperation] = []

    with transaction(conn):
        for source_path, source_rows in grouped.items():
            expected_sha256 = _validated_persisted_hash(source_path, source_rows)
            actual_sha256, reason = _inspect_audio_file(
                data_dir=data_dir,
                relative_path=source_path,
            )
            if reason is not None:
                raise AudioRelocationError(source_path, _relocation_reason(reason))
            if actual_sha256 != expected_sha256:
                raise AudioRelocationError(source_path, "audio_hash_mismatch")

            first_row = source_rows[0]
            target = canonical_audio_relative_path(
                name=str(participant_row["name"]),
                phone=str(participant_row["phone"]),
                participant_type="short",
                day_index=1,
                turn_index=int(first_row["turn_index"]),
                session_id=str(first_row["session_uuid"]),
                suffix=Path(source_path).suffix,
            )
            targets[source_path] = _collision_safe_audio_target(
                data_dir=data_dir,
                source_relative_path=source_path,
                target_relative_path=target,
                reserved_targets=reserved_targets,
            )
            reserved_targets.add(targets[source_path])
            staging_path = str(
                Path(source_path).with_name(
                    f".{Path(source_path).name}.cleanup-{uuid.uuid4().hex}.stage"
                )
            )
            owners = tuple(
                AudioOwner(
                    owner_table=str(row["owner_table"]),
                    owner_row_id=int(row["owner_row_id"]),
                    owner_field="user_audio_path",
                    original_path=source_path,
                    destination_path=targets[source_path],
                    original_sha256=str(row["user_audio_sha256"]),
                )
                for row in source_rows
            )
            preserve_source = _count_audio_path_references(conn, source_path) > len(owners)
            operations.append(
                _insert_cleanup_operation(
                    conn,
                    attempt_id=source_attempt_id,
                    operation_kind="relocate",
                    source_path=source_path,
                    staging_path=staging_path,
                    destination_path=targets[source_path],
                    expected_sha256=expected_sha256,
                    preserve_source=preserve_source,
                    worker_token=worker_token,
                    state="planned",
                    last_error=None,
                    owners=owners,
                )
            )
    return operations


def _prepare_audio_deletions(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    source_attempt_id: int,
    worker_token: str,
) -> tuple[list[AudioOperation], list[int]]:
    session_rows = list_incomplete_formal_sessions_for_attempt(
        conn,
        attempt_id=source_attempt_id,
    )
    session_ids = [int(row["id"]) for row in session_rows]
    if not session_ids:
        return [], []
    placeholders = ",".join("?" for _ in session_ids)
    rows = conn.execute(
        f"""
        SELECT 'conversation_turns' AS owner_table, id AS owner_row_id,
               user_audio_path, user_audio_sha256
        FROM conversation_turns
        WHERE session_id IN ({placeholders})
          AND user_audio_path IS NOT NULL AND user_audio_path != ''
        UNION ALL
        SELECT 'asr_attempts' AS owner_table, id AS owner_row_id,
               user_audio_path, user_audio_sha256
        FROM asr_attempts
        WHERE session_id IN ({placeholders})
          AND user_audio_path IS NOT NULL AND user_audio_path != ''
        """,
        (*session_ids, *session_ids),
    ).fetchall()
    grouped = _group_audio_rows(rows)
    operations: list[AudioOperation] = []
    with transaction(conn):
        for source_path, source_rows in grouped.items():
            expected_sha256, hash_error = _persisted_hash_for_review(source_rows)
            actual_sha256, source_error = _inspect_audio_file(
                data_dir=data_dir,
                relative_path=source_path,
            )
            last_error = (
                f"delete_{source_error}" if source_error is not None else hash_error
            )
            if last_error is None and actual_sha256 != expected_sha256:
                last_error = "delete_hash_mismatch"
            owners = tuple(
                AudioOwner(
                    owner_table=str(row["owner_table"]),
                    owner_row_id=int(row["owner_row_id"]),
                    owner_field="user_audio_path",
                    original_path=source_path,
                    destination_path=None,
                    original_sha256=(
                        str(row["user_audio_sha256"])
                        if row["user_audio_sha256"] is not None
                        else None
                    ),
                )
                for row in source_rows
            )
            preserve_source = _count_audio_path_references(conn, source_path) > len(owners)
            staging_path = str(
                Path(source_path).with_name(
                    f".{Path(source_path).name}.delete-{uuid.uuid4().hex}.tombstone"
                )
            )
            operations.append(
                _insert_cleanup_operation(
                    conn,
                    attempt_id=source_attempt_id,
                    operation_kind="delete",
                    source_path=source_path,
                    staging_path=staging_path,
                    destination_path=None,
                    expected_sha256=expected_sha256,
                    preserve_source=preserve_source,
                    worker_token=(None if last_error else worker_token),
                    state="review_needed" if last_error else "planned",
                    last_error=last_error,
                    owners=owners,
                )
            )
    return operations, session_ids


def _group_audio_rows(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["user_audio_path"]), []).append(row)
    return grouped


def _validated_persisted_hash(path: str, rows: list[sqlite3.Row]) -> str:
    expected, error = _persisted_hash_for_review(rows)
    if error == "delete_hash_invalid":
        raise AudioRelocationError(path, "audio_hash_invalid")
    if error == "delete_hash_conflict":
        raise AudioRelocationError(path, "audio_hash_conflict")
    if expected is None:
        raise AudioRelocationError(path, "audio_hash_invalid")
    return expected


def _persisted_hash_for_review(
    rows: list[sqlite3.Row],
) -> tuple[str | None, str | None]:
    values = [str(row["user_audio_sha256"] or "").strip().lower() for row in rows]
    if not values or any(not _is_sha256(value) for value in values):
        return None, "delete_hash_invalid"
    unique_values = set(values)
    if len(unique_values) != 1:
        return None, "delete_hash_conflict"
    return values[0], None


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _insert_cleanup_operation(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    operation_kind: str,
    source_path: str,
    staging_path: str | None,
    destination_path: str | None,
    expected_sha256: str | None,
    preserve_source: bool,
    worker_token: str | None,
    state: str,
    last_error: str | None,
    owners: tuple[AudioOwner, ...],
) -> AudioOperation:
    operation_id = int(
        conn.execute(
            """
            INSERT INTO cleanup_operations (
                attempt_id, operation_kind, source_path, staging_path,
                destination_path, expected_sha256, preserve_source,
                worker_token, lease_expires_at, state, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?,
                      CASE WHEN ? IS NULL THEN NULL ELSE datetime('now', '+5 minutes') END,
                      ?, ?)
            """,
            (
                attempt_id,
                operation_kind,
                source_path,
                staging_path,
                destination_path,
                expected_sha256,
                int(preserve_source),
                worker_token,
                worker_token,
                state,
                last_error,
            ),
        ).lastrowid
    )
    conn.executemany(
        """
        INSERT INTO cleanup_operation_owners (
            operation_id, owner_table, owner_row_id, owner_field,
            original_path, destination_path, original_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                operation_id,
                owner.owner_table,
                owner.owner_row_id,
                owner.owner_field,
                owner.original_path,
                owner.destination_path,
                owner.original_sha256,
            )
            for owner in owners
        ],
    )
    return AudioOperation(
        operation_id=operation_id,
        attempt_id=attempt_id,
        operation_kind=operation_kind,
        source_path=source_path,
        staging_path=staging_path,
        destination_path=destination_path,
        expected_sha256=expected_sha256,
        preserve_source=preserve_source,
        worker_token=worker_token,
        lease_expires_at=None,
        state=state,
        last_error=last_error,
        owners=owners,
    )


def _collision_safe_audio_target(
    *,
    data_dir: Path,
    source_relative_path: str,
    target_relative_path: str,
    reserved_targets: set[str],
) -> str:
    source_path = _normalized_relative_path(source_relative_path)
    target_path = _normalized_relative_path(target_relative_path)
    if source_path == target_path:
        return target_relative_path
    if target_relative_path not in reserved_targets and not _safe_path_exists(
        data_dir=data_dir,
        relative_path=target_relative_path,
    ):
        return target_relative_path
    target_relative = Path(target_relative_path)
    base = target_relative.with_suffix("")
    suffix = target_relative.suffix
    retry_index = 2
    while True:
        candidate = str(
            base.with_name(f"{base.name}_retry_{retry_index}").with_suffix(suffix)
        )
        if candidate not in reserved_targets and not _safe_path_exists(
            data_dir=data_dir,
            relative_path=candidate,
        ):
            return candidate
        retry_index += 1


def _stage_audio_relocations(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    relocations: list[AudioOperation],
) -> str | None:
    staged: list[AudioOperation] = []
    for relocation in relocations:
        try:
            with transaction(conn):
                _assert_owned_operation(
                    conn,
                    operation=relocation,
                    expected_states=("planned",),
                )
                _stage_audio_file(
                    data_dir=data_dir,
                    source_path=relocation.source_path,
                    staging_path=str(relocation.staging_path),
                    expected_sha256=str(relocation.expected_sha256),
                    preserve_source=relocation.preserve_source,
                )
                _set_cleanup_operation_state(
                    conn,
                    operation_id=relocation.operation_id,
                    state="staged",
                    worker_token=relocation.worker_token,
                    expected_states=("planned",),
                )
            staged.append(relocation)
        except OSError as exc:
            with transaction(conn):
                _set_cleanup_operation_error(
                    conn,
                    operation_id=relocation.operation_id,
                    error=f"stage_{_error_slug(exc)}",
                    worker_token=relocation.worker_token,
                )
                _release_cleanup_claim(
                    conn,
                    operation_id=relocation.operation_id,
                    worker_token=relocation.worker_token,
                )
            _restore_staged_relocations(conn, data_dir=data_dir, relocations=staged)
            return relocation.source_path
    return None


def _stage_audio_file(
    *,
    data_dir: Path,
    source_path: str,
    staging_path: str,
    expected_sha256: str,
    preserve_source: bool,
) -> None:
    _link_audio_file(
        data_dir=data_dir,
        source_path=source_path,
        destination_path=staging_path,
        expected_sha256=expected_sha256,
        remove_source=not preserve_source,
    )


def _restore_staged_relocations(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    relocations: list[AudioOperation],
) -> None:
    for relocation in reversed(relocations):
        try:
            with transaction(conn):
                _assert_owned_operation(
                    conn,
                    operation=relocation,
                    expected_states=("staged", "planned"),
                )
                restored = _file_matches_hash(
                    data_dir=data_dir,
                    relative_path=relocation.source_path,
                    expected_sha256=str(relocation.expected_sha256),
                )
                if not restored and _file_matches_hash(
                    data_dir=data_dir,
                    relative_path=str(relocation.staging_path),
                    expected_sha256=str(relocation.expected_sha256),
                ):
                    _relocate_file(
                        data_dir=data_dir,
                        source_path=str(relocation.staging_path),
                        destination_path=relocation.source_path,
                        expected_sha256=str(relocation.expected_sha256),
                    )
                    restored = True
                if restored:
                    _remove_owned_staging_duplicate(
                        data_dir=data_dir,
                        staging_path=str(relocation.staging_path),
                        expected_sha256=str(relocation.expected_sha256),
                    )
                    _set_cleanup_operation_state(
                        conn,
                        operation_id=relocation.operation_id,
                        state="rolled_back",
                        worker_token=relocation.worker_token,
                        expected_states=("staged", "planned"),
                    )
                else:
                    _set_cleanup_operation_error(
                        conn,
                        operation_id=relocation.operation_id,
                        error="restore_source_and_staging_missing",
                        worker_token=relocation.worker_token,
                    )
                    _release_cleanup_claim(
                        conn,
                        operation_id=relocation.operation_id,
                        worker_token=relocation.worker_token,
                    )
        except OSError as exc:
            with transaction(conn):
                latest = _load_cleanup_operation(conn, relocation.operation_id)
                if latest is None or latest.worker_token != relocation.worker_token:
                    raise CleanupClaimLost(
                        f"Cleanup operation claim lost: {relocation.operation_id}"
                    ) from exc
                _set_cleanup_operation_error(
                    conn,
                    operation_id=relocation.operation_id,
                    error=f"restore_{_error_slug(exc)}",
                    worker_token=relocation.worker_token,
                )
                _release_cleanup_claim(
                    conn,
                    operation_id=relocation.operation_id,
                    worker_token=relocation.worker_token,
                )


def _roll_back_planned_deletions(
    conn: sqlite3.Connection,
    operations: list[AudioOperation],
) -> None:
    with transaction(conn):
        for operation in operations:
            if operation.state == "planned":
                _set_cleanup_operation_state(
                    conn,
                    operation_id=operation.operation_id,
                    state="rolled_back",
                    worker_token=operation.worker_token,
                    expected_states=("planned",),
                )


def _finalize_audio_relocations(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    relocations: list[AudioOperation],
    failed_audio_paths: list[str],
) -> None:
    for relocation in relocations:
        try:
            with transaction(conn):
                _assert_owned_operation(
                    conn,
                    operation=relocation,
                    expected_states=("database_committed",),
                )
                _finalize_audio_file(
                    data_dir=data_dir,
                    staging_path=str(relocation.staging_path),
                    destination_path=str(relocation.destination_path),
                    expected_sha256=str(relocation.expected_sha256),
                )
                _set_cleanup_operation_state(
                    conn,
                    operation_id=relocation.operation_id,
                    state="completed",
                    worker_token=relocation.worker_token,
                    expected_states=("database_committed",),
                )
        except OSError as exc:
            failed_audio_paths.append(str(relocation.destination_path))
            if _path_exists_but_hash_differs(
                data_dir=data_dir,
                relative_path=str(relocation.destination_path),
                expected_sha256=str(relocation.expected_sha256),
            ) and _rollback_committed_relocation(
                conn,
                data_dir=data_dir,
                operation=relocation,
                error="destination_hash_mismatch",
            ):
                continue
            with transaction(conn):
                _set_cleanup_operation_error(
                    conn,
                    operation_id=relocation.operation_id,
                    error=f"finalize_{_error_slug(exc)}",
                    worker_token=relocation.worker_token,
                )
                _release_cleanup_claim(
                    conn,
                    operation_id=relocation.operation_id,
                    worker_token=relocation.worker_token,
                )
            continue


def _finalize_audio_file(
    *,
    data_dir: Path,
    staging_path: str,
    destination_path: str,
    expected_sha256: str,
) -> None:
    _relocate_file(
        data_dir=data_dir,
        source_path=staging_path,
        destination_path=destination_path,
        expected_sha256=expected_sha256,
    )


def _rollback_committed_relocation(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    operation: AudioOperation,
    error: str,
) -> bool:
    try:
        with transaction(conn):
            _assert_owned_operation(
                conn,
                operation=operation,
                expected_states=("database_committed",),
            )
            expected_sha256 = str(operation.expected_sha256)
            source_ready = _file_matches_hash(
                data_dir=data_dir,
                relative_path=operation.source_path,
                expected_sha256=expected_sha256,
            )
            if not source_ready and _file_matches_hash(
                data_dir=data_dir,
                relative_path=str(operation.staging_path),
                expected_sha256=expected_sha256,
            ):
                _relocate_file(
                    data_dir=data_dir,
                    source_path=str(operation.staging_path),
                    destination_path=operation.source_path,
                    expected_sha256=expected_sha256,
                )
                source_ready = True
            if not source_ready:
                _set_cleanup_operation_error(
                    conn,
                    operation_id=operation.operation_id,
                    error="rollback_source_unavailable",
                    worker_token=operation.worker_token,
                )
                _release_cleanup_claim(
                    conn,
                    operation_id=operation.operation_id,
                    worker_token=operation.worker_token,
                )
                return False
            for owner in operation.owners:
                current = _get_owner_row(conn, owner)
                if current is None:
                    raise RuntimeError("cleanup_owner_missing")
                if str(current["user_audio_path"]) == owner.original_path:
                    continue
                _update_owned_audio_path(
                    conn,
                    owner=owner,
                    expected_path=str(operation.destination_path),
                    replacement_path=owner.original_path,
                )
            _remove_owned_staging_duplicate(
                data_dir=data_dir,
                staging_path=str(operation.staging_path),
                expected_sha256=expected_sha256,
            )
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="rolled_back",
                error=error,
                worker_token=operation.worker_token,
                expected_states=("database_committed",),
            )
        return True
    except CleanupClaimLost:
        raise
    except (OSError, CleanupEligibilityChanged, RuntimeError) as exc:
        with transaction(conn):
            latest = _load_cleanup_operation(conn, operation.operation_id)
            if latest is None or latest.worker_token != operation.worker_token:
                raise CleanupClaimLost(
                    f"Cleanup operation claim lost: {operation.operation_id}"
                ) from exc
            error_code = (
                f"rollback_{_error_slug(exc)}"
                if isinstance(exc, OSError)
                else "rollback_owner_changed"
            )
            _set_cleanup_operation_error(
                conn,
                operation_id=operation.operation_id,
                error=error_code,
                worker_token=operation.worker_token,
            )
            _release_cleanup_claim(
                conn,
                operation_id=operation.operation_id,
                worker_token=operation.worker_token,
            )
        return False


def _finalize_audio_deletions(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    operations: list[AudioOperation],
    deleted_audio_paths: list[str],
    failed_audio_paths: list[str],
) -> None:
    for operation in operations:
        current = _load_cleanup_operation(conn, operation.operation_id)
        if current is None or current.state not in {"database_committed", "staged"}:
            continue
        try:
            deleted = _finalize_audio_deletion(
                conn=conn,
                data_dir=data_dir,
                operation=current,
            )
        except OSError as exc:
            slug = _error_slug(exc)
            code = slug if slug.startswith("delete_") else f"delete_{slug}"
            failed_audio_paths.append(current.source_path)
            latest = _load_cleanup_operation(conn, current.operation_id) or current
            with transaction(conn):
                if code in _DELETION_REVIEW_ERRORS:
                    _set_cleanup_operation_state(
                        conn,
                        operation_id=latest.operation_id,
                        state="review_needed",
                        error=code,
                        worker_token=latest.worker_token,
                        expected_states=(latest.state,),
                    )
                else:
                    _set_cleanup_operation_error(
                        conn,
                        operation_id=latest.operation_id,
                        error=code,
                        worker_token=latest.worker_token,
                    )
                    _release_cleanup_claim(
                        conn,
                        operation_id=latest.operation_id,
                        worker_token=latest.worker_token,
                    )
            continue
        if deleted:
            deleted_audio_paths.append(current.source_path)


def _finalize_audio_deletion(
    *,
    conn: sqlite3.Connection,
    data_dir: Path,
    operation: AudioOperation,
) -> bool:
    if operation.preserve_source:
        with transaction(conn):
            _assert_owned_operation(
                conn,
                operation=operation,
                expected_states=(operation.state,),
            )
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="completed",
                error="shared_source_preserved",
                worker_token=operation.worker_token,
                expected_states=(operation.state,),
            )
        return False

    expected_sha256 = str(operation.expected_sha256)
    if operation.state == "database_committed":
        with transaction(conn):
            _assert_owned_operation(
                conn,
                operation=operation,
                expected_states=("database_committed",),
            )
            if not _file_matches_hash(
                data_dir=data_dir,
                relative_path=str(operation.staging_path),
                expected_sha256=expected_sha256,
            ):
                _, reason = _inspect_audio_file(
                    data_dir=data_dir,
                    relative_path=operation.source_path,
                )
                if reason == "source_missing":
                    _set_cleanup_operation_state(
                        conn,
                        operation_id=operation.operation_id,
                        state="completed",
                        error="delete_source_missing_after_commit",
                        worker_token=operation.worker_token,
                        expected_states=("database_committed",),
                    )
                    return False
                _stage_verified_deletion(
                    data_dir=data_dir,
                    source_path=operation.source_path,
                    staging_path=str(operation.staging_path),
                    expected_sha256=expected_sha256,
                )
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="staged",
                worker_token=operation.worker_token,
                expected_states=("database_committed",),
            )
        operation = _load_cleanup_operation(conn, operation.operation_id) or operation

    with transaction(conn):
        _assert_owned_operation(
            conn,
            operation=operation,
            expected_states=("staged",),
        )
        tombstone_digest, tombstone_reason = _inspect_audio_file(
            data_dir=data_dir,
            relative_path=str(operation.staging_path),
        )
        if tombstone_reason == "source_missing" and _missing_staged_delete_is_complete(
            conn,
            operation=operation,
        ):
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="completed",
                error="delete_tombstone_already_unlinked",
                worker_token=operation.worker_token,
                expected_states=("staged",),
            )
            return False
        if tombstone_reason is not None or tombstone_digest != expected_sha256:
            raise AudioFilesystemError("tombstone_missing_or_changed")
        _unlink_verified_regular_file(
            data_dir=data_dir,
            relative_path=str(operation.staging_path),
        )
        _set_cleanup_operation_state(
            conn,
            operation_id=operation.operation_id,
            state="completed",
            worker_token=operation.worker_token,
            expected_states=("staged",),
        )
    return True


def _missing_staged_delete_is_complete(
    conn: sqlite3.Connection,
    *,
    operation: AudioOperation,
) -> bool:
    return (
        operation.state == "staged"
        and not operation.preserve_source
        and bool(operation.owners)
        and operation.last_error not in _DELETION_REVIEW_ERRORS
        and not any(_get_owner_row(conn, owner) is not None for owner in operation.owners)
    )


def _before_delete_stage_rename(**_kwargs: object) -> None:
    return None


def _stage_verified_deletion(
    *,
    data_dir: Path,
    source_path: str,
    staging_path: str,
    expected_sha256: str,
) -> None:
    source_relative = _normalized_relative_path(source_path)
    staging_relative = _normalized_relative_path(staging_path)
    source_parent, source_descriptors = _open_parent_descriptors(
        data_dir=data_dir,
        relative_path=source_relative,
    )
    staging_parent, staging_descriptors = _open_parent_descriptors(
        data_dir=data_dir,
        relative_path=staging_relative,
    )
    file_descriptor: int | None = None
    try:
        source_stat = os.stat(
            source_relative.name,
            dir_fd=source_parent,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(source_stat.st_mode):
            raise AudioFilesystemError("source_nonregular")
        file_descriptor = os.open(
            source_relative.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=source_parent,
        )
        opened_stat = os.fstat(file_descriptor)
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            raise AudioFilesystemError("source_identity_changed")
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise AudioFilesystemError("hash_mismatch")

        _before_delete_stage_rename(
            source_path=source_path,
            staging_path=staging_path,
            expected_sha256=expected_sha256,
        )
        os.rename(
            source_relative.name,
            staging_relative.name,
            src_dir_fd=source_parent,
            dst_dir_fd=staging_parent,
        )
        staged_stat = os.stat(
            staging_relative.name,
            dir_fd=staging_parent,
            follow_symlinks=False,
        )
        if (staged_stat.st_dev, staged_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            try:
                os.stat(
                    source_relative.name,
                    dir_fd=source_parent,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                os.rename(
                    staging_relative.name,
                    source_relative.name,
                    src_dir_fd=staging_parent,
                    dst_dir_fd=source_parent,
                )
            raise AudioFilesystemError("source_identity_changed")
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        _close_descriptors(staging_descriptors)
        _close_descriptors(source_descriptors)


def reconcile_cleanup_operations(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
) -> None:
    if conn.in_transaction:
        raise RuntimeError("cleanup reconciliation must own its database transactions")
    operations = _load_cleanup_operations(
        conn,
        states=("planned", "staged", "database_committed"),
    )
    for operation in operations:
        claimed = _claim_operation_for_reconciliation(conn, operation)
        if claimed is None:
            continue
        operation = claimed
        if operation.operation_kind == "delete":
            _reconcile_deletion(conn, data_dir=data_dir, operation=operation)
        else:
            _reconcile_relocation(conn, data_dir=data_dir, operation=operation)
    unresolved = _unresolved_operation_diagnostics(conn)
    if unresolved:
        raise CleanupReconciliationError(unresolved)


def _claim_operation_for_reconciliation(
    conn: sqlite3.Connection,
    operation: AudioOperation,
) -> AudioOperation | None:
    reconcile_token = f"reconcile-{uuid.uuid4().hex}"
    with transaction(conn):
        cursor = conn.execute(
            """
            UPDATE cleanup_operations
            SET worker_token = ?, lease_expires_at = datetime('now', '+5 minutes'),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND state = ?
              AND (
                  worker_token IS NULL
                  OR lease_expires_at IS NULL
                  OR lease_expires_at <= CURRENT_TIMESTAMP
              )
            """,
            (reconcile_token, operation.operation_id, operation.state),
        )
    if cursor.rowcount != 1:
        return None
    return _load_cleanup_operation(conn, operation.operation_id)


def _reconcile_deletion(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    operation: AudioOperation,
) -> None:
    owner_exists = any(_get_owner_row(conn, owner) is not None for owner in operation.owners)
    if operation.state == "planned" and owner_exists:
        with transaction(conn):
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="rolled_back",
                worker_token=operation.worker_token,
                expected_states=("planned",),
            )
        return
    if operation.state == "planned":
        with transaction(conn):
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="database_committed",
                worker_token=operation.worker_token,
                expected_states=("planned",),
            )
    refreshed = _load_cleanup_operation(conn, operation.operation_id)
    if refreshed is None:
        return
    _finalize_audio_deletions(
        conn,
        data_dir=data_dir,
        operations=[refreshed],
        deleted_audio_paths=[],
        failed_audio_paths=[],
    )


def _reconcile_relocation(
    conn: sqlite3.Connection,
    *,
    data_dir: Path,
    operation: AudioOperation,
) -> None:
    owner_rows = [_get_owner_row(conn, owner) for owner in operation.owners]
    if any(row is None for row in owner_rows):
        with transaction(conn):
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="review_needed",
                error="relocation_owner_missing",
                worker_token=operation.worker_token,
                expected_states=(operation.state,),
            )
        return
    owner_paths = {str(row["user_audio_path"]) for row in owner_rows if row is not None}
    expected_sha256 = str(operation.expected_sha256)
    if owner_paths == {operation.source_path}:
        _restore_staged_relocations(conn, data_dir=data_dir, relocations=[operation])
        return
    if owner_paths != {str(operation.destination_path)}:
        _rollback_committed_relocation(
            conn,
            data_dir=data_dir,
            operation=operation,
            error="mixed_owner_paths",
        )
        return
    if _file_matches_hash(
        data_dir=data_dir,
        relative_path=str(operation.destination_path),
        expected_sha256=expected_sha256,
    ):
        with transaction(conn):
            _assert_owned_operation(
                conn,
                operation=operation,
                expected_states=(operation.state,),
            )
            _remove_owned_staging_duplicate(
                data_dir=data_dir,
                staging_path=str(operation.staging_path),
                expected_sha256=expected_sha256,
            )
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="completed",
                worker_token=operation.worker_token,
                expected_states=(operation.state,),
            )
        return
    if _path_exists_but_hash_differs(
        data_dir=data_dir,
        relative_path=str(operation.destination_path),
        expected_sha256=expected_sha256,
    ):
        _rollback_committed_relocation(
            conn,
            data_dir=data_dir,
            operation=operation,
            error="destination_hash_mismatch",
        )
        return
    staging_ready = _file_matches_hash(
        data_dir=data_dir,
        relative_path=str(operation.staging_path),
        expected_sha256=expected_sha256,
    )
    source_ready = _file_matches_hash(
        data_dir=data_dir,
        relative_path=operation.source_path,
        expected_sha256=expected_sha256,
    )
    if not staging_ready and not source_ready:
        with transaction(conn):
            _set_cleanup_operation_error(
                conn,
                operation_id=operation.operation_id,
                error="reconcile_source_and_staging_missing",
                worker_token=operation.worker_token,
            )
            _release_cleanup_claim(
                conn,
                operation_id=operation.operation_id,
                worker_token=operation.worker_token,
            )
        return
    candidate = str(operation.staging_path) if staging_ready else operation.source_path
    try:
        with transaction(conn):
            _assert_owned_operation(
                conn,
                operation=operation,
                expected_states=(operation.state,),
            )
            _link_audio_file(
                data_dir=data_dir,
                source_path=candidate,
                destination_path=str(operation.destination_path),
                expected_sha256=expected_sha256,
                remove_source=(candidate == str(operation.staging_path)),
            )
            _set_cleanup_operation_state(
                conn,
                operation_id=operation.operation_id,
                state="completed",
                worker_token=operation.worker_token,
                expected_states=(operation.state,),
            )
    except OSError as exc:
        with transaction(conn):
            _set_cleanup_operation_error(
                conn,
                operation_id=operation.operation_id,
                error=f"reconcile_{_error_slug(exc)}",
                worker_token=operation.worker_token,
            )
            _release_cleanup_claim(
                conn,
                operation_id=operation.operation_id,
                worker_token=operation.worker_token,
            )
        return


def _load_cleanup_operations(
    conn: sqlite3.Connection,
    *,
    states: tuple[str, ...],
) -> list[AudioOperation]:
    placeholders = ",".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT * FROM cleanup_operations WHERE state IN ({placeholders}) ORDER BY id",
        states,
    ).fetchall()
    return [_operation_from_row(conn, row) for row in rows]


def _load_cleanup_operation(
    conn: sqlite3.Connection,
    operation_id: int,
) -> AudioOperation | None:
    row = conn.execute(
        "SELECT * FROM cleanup_operations WHERE id = ?",
        (operation_id,),
    ).fetchone()
    return None if row is None else _operation_from_row(conn, row)


def _operation_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> AudioOperation:
    owner_rows = conn.execute(
        "SELECT * FROM cleanup_operation_owners WHERE operation_id = ? ORDER BY id",
        (int(row["id"]),),
    ).fetchall()
    owners = tuple(
        AudioOwner(
            owner_table=str(owner["owner_table"]),
            owner_row_id=int(owner["owner_row_id"]),
            owner_field=str(owner["owner_field"]),
            original_path=str(owner["original_path"]),
            destination_path=(
                str(owner["destination_path"])
                if owner["destination_path"] is not None
                else None
            ),
            original_sha256=(
                str(owner["original_sha256"])
                if owner["original_sha256"] is not None
                else None
            ),
        )
        for owner in owner_rows
    )
    return AudioOperation(
        operation_id=int(row["id"]),
        attempt_id=int(row["attempt_id"]),
        operation_kind=str(row["operation_kind"]),
        source_path=str(row["source_path"]),
        staging_path=str(row["staging_path"]) if row["staging_path"] is not None else None,
        destination_path=(
            str(row["destination_path"])
            if row["destination_path"] is not None
            else None
        ),
        expected_sha256=(
            str(row["expected_sha256"])
            if row["expected_sha256"] is not None
            else None
        ),
        preserve_source=bool(row["preserve_source"]),
        worker_token=(
            str(row["worker_token"]) if row["worker_token"] is not None else None
        ),
        lease_expires_at=(
            str(row["lease_expires_at"])
            if row["lease_expires_at"] is not None
            else None
        ),
        state=str(row["state"]),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        owners=owners,
    )


def _get_owner_row(
    conn: sqlite3.Connection,
    owner: AudioOwner,
) -> sqlite3.Row | None:
    if owner.owner_table not in {"conversation_turns", "asr_attempts"}:
        return None
    return conn.execute(
        f"SELECT id, user_audio_path, user_audio_sha256 FROM {owner.owner_table} WHERE id = ?",
        (owner.owner_row_id,),
    ).fetchone()


def _unresolved_operation_diagnostics(
    conn: sqlite3.Connection,
) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, operation_kind, state, last_error,
               source_path, staging_path, destination_path,
               worker_token, lease_expires_at,
               CASE
                   WHEN worker_token IS NOT NULL
                    AND lease_expires_at > CURRENT_TIMESTAMP
                   THEN 1 ELSE 0
               END AS active_lease
        FROM cleanup_operations
        WHERE state IN ('planned', 'staged', 'database_committed')
           OR (state = 'review_needed' AND operation_kind = 'relocate')
        ORDER BY id
        """
    ).fetchall()
    return [
        {
            "operation_id": int(row["id"]),
            "operation_kind": str(row["operation_kind"]),
            "state": str(row["state"]),
            "last_error": (
                "active_lease"
                if bool(row["active_lease"])
                else (
                    str(row["last_error"])
                    if row["last_error"] is not None
                    else "unresolved_cleanup_state"
                )
            ),
            "source_path": str(row["source_path"]),
            "staging_path": (
                str(row["staging_path"]) if row["staging_path"] is not None else None
            ),
            "destination_path": (
                str(row["destination_path"])
                if row["destination_path"] is not None
                else None
            ),
        }
        for row in rows[:10]
    ]


def _set_cleanup_operation_state(
    conn: sqlite3.Connection,
    *,
    operation_id: int,
    state: str,
    error: str | None = None,
    worker_token: str | None = None,
    expected_states: tuple[str, ...] | None = None,
) -> None:
    terminal = state in {"completed", "rolled_back", "review_needed"}
    if worker_token is None:
        conn.execute(
            """
            UPDATE cleanup_operations
            SET state = ?, last_error = ?,
                worker_token = CASE WHEN ? THEN NULL ELSE worker_token END,
                lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (state, error, int(terminal), int(terminal), operation_id),
        )
        return
    states = expected_states or ("planned", "staged", "database_committed")
    placeholders = ",".join("?" for _ in states)
    cursor = conn.execute(
        f"""
        UPDATE cleanup_operations
        SET state = ?, last_error = ?,
            worker_token = CASE WHEN ? THEN NULL ELSE worker_token END,
            lease_expires_at = CASE
                WHEN ? THEN NULL
                ELSE datetime('now', '+5 minutes')
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND worker_token = ? AND state IN ({placeholders})
        """,
        (
            state,
            error,
            int(terminal),
            int(terminal),
            operation_id,
            worker_token,
            *states,
        ),
    )
    if cursor.rowcount != 1:
        raise CleanupClaimLost(f"Cleanup operation claim lost: {operation_id}")


def _assert_owned_operation(
    conn: sqlite3.Connection,
    *,
    operation: AudioOperation,
    expected_states: tuple[str, ...],
) -> None:
    placeholders = ",".join("?" for _ in expected_states)
    row = conn.execute(
        f"""
        SELECT 1
        FROM cleanup_operations
        WHERE id = ? AND worker_token = ?
          AND state IN ({placeholders})
          AND lease_expires_at IS NOT NULL
          AND lease_expires_at > CURRENT_TIMESTAMP
        """,
        (operation.operation_id, operation.worker_token, *expected_states),
    ).fetchone()
    if row is None:
        raise CleanupClaimLost(
            f"Cleanup operation claim is stale or terminal: {operation.operation_id}"
        )


def _set_cleanup_operation_error(
    conn: sqlite3.Connection,
    *,
    operation_id: int,
    error: str,
    worker_token: str | None = None,
) -> None:
    if worker_token is None:
        conn.execute(
            """
            UPDATE cleanup_operations
            SET last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (error[:1000], operation_id),
        )
        return
    cursor = conn.execute(
        """
        UPDATE cleanup_operations
        SET last_error = ?, lease_expires_at = datetime('now', '+5 minutes'),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND worker_token = ?
          AND state IN ('planned', 'staged', 'database_committed')
        """,
        (error[:1000], operation_id, worker_token),
    )
    if cursor.rowcount != 1:
        raise CleanupClaimLost(f"Cleanup operation claim lost: {operation_id}")


def _release_cleanup_claim(
    conn: sqlite3.Connection,
    *,
    operation_id: int,
    worker_token: str | None,
) -> None:
    if worker_token is None:
        return
    conn.execute(
        """
        UPDATE cleanup_operations
        SET worker_token = NULL, lease_expires_at = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND worker_token = ?
          AND state IN ('planned', 'staged', 'database_committed')
        """,
        (operation_id, worker_token),
    )


def _count_audio_path_references(conn: sqlite3.Connection, path: str) -> int:
    return sum(
        int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table_name} WHERE user_audio_path = ?",
                (path,),
            ).fetchone()[0]
        )
        for table_name in ("conversation_turns", "asr_attempts")
    )


def _normalized_relative_path(value: str) -> Path:
    if not value or "\0" in value:
        raise ValueError("path_outside_root")
    relative_path = Path(value)
    if relative_path.is_absolute() or value != relative_path.as_posix():
        raise ValueError("path_outside_root")
    if (
        not relative_path.parts
        or relative_path.parts[0] != "audio"
        or any(component in {"", ".", ".."} for component in relative_path.parts)
    ):
        raise ValueError("path_outside_root")
    return relative_path


def _inspect_audio_file(
    *,
    data_dir: Path,
    relative_path: str,
) -> tuple[str | None, str | None]:
    try:
        normalized = _normalized_relative_path(relative_path)
    except ValueError:
        return None, "path_outside_root"
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        parent_descriptor, descriptors = _open_parent_descriptors(
            data_dir=data_dir,
            relative_path=normalized,
        )
        leaf_stat = os.stat(
            normalized.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(leaf_stat.st_mode):
            return None, "source_symlink"
        if not stat.S_ISREG(leaf_stat.st_mode):
            return None, "source_nonregular"
        file_descriptor = os.open(
            normalized.name,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_descriptor,
        )
        opened_stat = os.fstat(file_descriptor)
        if (opened_stat.st_dev, opened_stat.st_ino) != (leaf_stat.st_dev, leaf_stat.st_ino):
            return None, "source_changed"
        digest = hashlib.sha256()
        while chunk := os.read(file_descriptor, 1024 * 1024):
            digest.update(chunk)
        final_stat = os.fstat(file_descriptor)
        if final_stat.st_nlink == 0 or (
            final_stat.st_dev,
            final_stat.st_ino,
            final_stat.st_size,
        ) != (leaf_stat.st_dev, leaf_stat.st_ino, leaf_stat.st_size):
            return None, "source_changed"
        return digest.hexdigest(), None
    except FileNotFoundError:
        return None, "source_missing"
    except ValueError:
        return None, "path_symlink"
    except OSError:
        return None, "source_invalid"
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        _close_descriptors(descriptors)


def _relocation_reason(source_reason: str) -> str:
    return {
        "path_outside_root": "audio_path_outside_root",
        "source_missing": "audio_source_missing",
        "source_symlink": "audio_source_symlink",
        "source_nonregular": "audio_source_nonregular",
        "path_symlink": "audio_path_symlink",
    }.get(source_reason, "audio_source_invalid")


def _open_parent_descriptors(
    *,
    data_dir: Path,
    relative_path: Path,
) -> tuple[int, list[int]]:
    descriptors: list[int] = []
    try:
        root_stat = data_dir.lstat()
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("invalid data root")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = flags | getattr(os, "O_DIRECTORY", 0)
        root_descriptor = os.open(data_dir, directory_flags)
        descriptors.append(root_descriptor)
        opened_root = os.fstat(root_descriptor)
        if (opened_root.st_dev, opened_root.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise ValueError("data root changed")
        parent_descriptor = root_descriptor
        for component in relative_path.parts[:-1]:
            component_stat = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(component_stat.st_mode):
                raise ValueError("invalid audio parent")
            parent_descriptor = os.open(component, directory_flags, dir_fd=parent_descriptor)
            descriptors.append(parent_descriptor)
            opened_component = os.fstat(parent_descriptor)
            if (opened_component.st_dev, opened_component.st_ino) != (
                component_stat.st_dev,
                component_stat.st_ino,
            ):
                raise ValueError("audio parent changed")
        return parent_descriptor, descriptors
    except BaseException:
        _close_descriptors(descriptors)
        raise


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _safe_path_exists(*, data_dir: Path, relative_path: str) -> bool:
    _, reason = _inspect_audio_file(data_dir=data_dir, relative_path=relative_path)
    return reason != "source_missing"


def _file_matches_hash(
    *,
    data_dir: Path,
    relative_path: str,
    expected_sha256: str,
) -> bool:
    digest, reason = _inspect_audio_file(data_dir=data_dir, relative_path=relative_path)
    return reason is None and digest == expected_sha256


def _path_exists_but_hash_differs(
    *,
    data_dir: Path,
    relative_path: str,
    expected_sha256: str,
) -> bool:
    digest, reason = _inspect_audio_file(data_dir=data_dir, relative_path=relative_path)
    return reason is None and digest != expected_sha256


def _link_audio_file(
    *,
    data_dir: Path,
    source_path: str,
    destination_path: str,
    expected_sha256: str,
    remove_source: bool,
) -> None:
    if not _file_matches_hash(
        data_dir=data_dir,
        relative_path=source_path,
        expected_sha256=expected_sha256,
    ):
        raise AudioFilesystemError("hash_mismatch")
    source_relative = _normalized_relative_path(source_path)
    destination_relative = _normalized_relative_path(destination_path)
    source_parent, source_descriptors = _open_parent_descriptors(
        data_dir=data_dir,
        relative_path=source_relative,
    )
    destination_parent, destination_descriptors = _open_parent_descriptors(
        data_dir=data_dir,
        relative_path=destination_relative,
    )
    try:
        os.link(
            source_relative.name,
            destination_relative.name,
            src_dir_fd=source_parent,
            dst_dir_fd=destination_parent,
            follow_symlinks=False,
        )
        if not _file_matches_hash(
            data_dir=data_dir,
            relative_path=destination_path,
            expected_sha256=expected_sha256,
        ):
            raise AudioFilesystemError("hash_changed")
        if remove_source:
            os.unlink(source_relative.name, dir_fd=source_parent)
    finally:
        _close_descriptors(destination_descriptors)
        _close_descriptors(source_descriptors)


def _relocate_file(
    *,
    data_dir: Path,
    source_path: str,
    destination_path: str,
    expected_sha256: str,
) -> None:
    _link_audio_file(
        data_dir=data_dir,
        source_path=source_path,
        destination_path=destination_path,
        expected_sha256=expected_sha256,
        remove_source=True,
    )


def _remove_owned_staging_duplicate(
    *,
    data_dir: Path,
    staging_path: str,
    expected_sha256: str,
) -> None:
    if not _file_matches_hash(
        data_dir=data_dir,
        relative_path=staging_path,
        expected_sha256=expected_sha256,
    ):
        return
    _unlink_verified_regular_file(data_dir=data_dir, relative_path=staging_path)


def _unlink_verified_regular_file(*, data_dir: Path, relative_path: str) -> None:
    normalized = _normalized_relative_path(relative_path)
    parent_descriptor, descriptors = _open_parent_descriptors(
        data_dir=data_dir,
        relative_path=normalized,
    )
    try:
        leaf_stat = os.stat(
            normalized.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(leaf_stat.st_mode):
            raise AudioFilesystemError("source_nonregular")
        os.unlink(normalized.name, dir_fd=parent_descriptor)
    finally:
        _close_descriptors(descriptors)


def _error_slug(exc: OSError) -> str:
    if isinstance(exc, AudioFilesystemError):
        return exc.code
    value = str(exc).strip().lower()
    normalized = "_".join(part for part in value.replace("-", " ").split() if part)
    return normalized or exc.__class__.__name__.lower()
