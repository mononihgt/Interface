from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from fastapi import HTTPException, status

from backend.app.db import get_connection, transaction
from backend.app.models.domain import EXPORT_TYPES
from backend.app.services.export import (
    ExportEvidenceError,
    create_clean_data_export,
    create_reimbursement_export,
    create_v2_export,
    normalize_export_filters,
)
from backend.app.services.file_naming import safe_filename_component
from backend.app.settings import Settings


EXPORT_JOB_MAX_ATTEMPTS = 3
EXPORT_JOB_LEASE_SECONDS = 120
EXPORT_JOB_HEARTBEAT_SECONDS = 30
EXPORT_JOB_HEARTBEAT_RETRY_SECONDS = 0.25
EXPORT_JOB_RECOVERY_POLL_SECONDS = 5
EXPORT_JOB_ERROR_MESSAGE_LIMIT = 300
_WORKER_ID_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")

_EXPORT_JOB_COLUMNS = """
    job_uuid,
    export_type,
    filters_json,
    include_test,
    status,
    progress_message,
    output_path,
    created_by,
    created_at,
    started_at,
    completed_at,
    error_message,
    lease_owner,
    lease_token,
    lease_expires_at,
    heartbeat_at,
    attempt_count,
    failure_kind,
    publication_state,
    publication_token,
    staging_path,
    canonical_path,
    archive_sha256
"""


class ExportJobOwnershipLost(RuntimeError):
    pass


class _ExportJobHeartbeat:
    def __init__(self, *, settings: Settings, job_uuid: str, lease_token: str) -> None:
        self._settings = settings
        self._job_uuid = job_uuid
        self._lease_token = lease_token
        self._stop_event = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"export-job-heartbeat-{safe_filename_component(job_uuid)}",
            daemon=True,
        )

    @property
    def ownership_lost(self) -> bool:
        return self._ownership_lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join()

    def _run(self) -> None:
        lease_deadline = time.monotonic() + EXPORT_JOB_LEASE_SECONDS
        wait_seconds = EXPORT_JOB_HEARTBEAT_SECONDS
        while not self._stop_event.wait(wait_seconds):
            try:
                conn = get_connection(self._settings)
                try:
                    renewed = renew_export_job_lease(
                        conn,
                        job_uuid=self._job_uuid,
                        lease_token=self._lease_token,
                    )
                finally:
                    conn.close()
            except Exception:
                remaining_seconds = lease_deadline - time.monotonic()
                if remaining_seconds <= 0:
                    self._ownership_lost.set()
                    return
                wait_seconds = min(
                    EXPORT_JOB_HEARTBEAT_RETRY_SECONDS,
                    remaining_seconds,
                )
                continue
            if not renewed:
                self._ownership_lost.set()
                return
            lease_deadline = time.monotonic() + EXPORT_JOB_LEASE_SECONDS
            wait_seconds = EXPORT_JOB_HEARTBEAT_SECONDS


class ExportJobRecoverySupervisor:
    def __init__(
        self,
        *,
        settings: Settings,
        poll_seconds: float = EXPORT_JOB_RECOVERY_POLL_SECONDS,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("Export recovery poll interval must be positive.")
        self._settings = settings
        self._poll_seconds = poll_seconds
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="export-job-recovery-supervisor",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def join(self) -> None:
        self._thread.join()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                drain_recoverable_export_jobs(
                    self._settings,
                    stop_event=self._stop_event,
                )
            except Exception:
                pass
            if self._stop_event.is_set():
                return
            self._wake_event.wait(self._poll_seconds)
            self._wake_event.clear()


def create_export_job(
    conn: sqlite3.Connection,
    *,
    export_type: str,
    filters: dict[str, object],
    include_test: bool,
    created_by: str,
) -> dict[str, object]:
    if export_type not in EXPORT_TYPES:
        raise ValueError("Unsupported export type.")
    if export_type == "reimbursement" and include_test:
        raise ValueError("reimbursement exports do not support include_test.")

    job_uuid = str(uuid4())
    conn.execute(
        """
        INSERT INTO export_jobs (
            job_uuid,
            export_type,
            filters_json,
            include_test,
            status,
            created_by
        ) VALUES (?, ?, ?, ?, 'queued', ?)
        """,
        (
            job_uuid,
            export_type,
            json.dumps(filters, ensure_ascii=False, sort_keys=True),
            1 if include_test else 0,
            created_by,
        ),
    )
    return get_export_job(conn, job_uuid=job_uuid)


def get_export_job(conn: sqlite3.Connection, job_uuid: str) -> dict[str, object]:
    row = conn.execute(
        f"SELECT {_EXPORT_JOB_COLUMNS} FROM export_jobs WHERE job_uuid = ?",
        (job_uuid,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Export job not found.",
        )
    return _row_to_export_job(row)


def list_export_jobs(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        f"SELECT {_EXPORT_JOB_COLUMNS} FROM export_jobs ORDER BY id DESC"
    ).fetchall()
    return [_row_to_export_job(row) for row in rows]


def delete_export_job(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    job_uuid: str,
) -> dict[str, object]:
    job = get_export_job(conn, job_uuid=job_uuid)
    if job["status"] in {"queued", "running"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Export job is queued or running and cannot be deleted.",
        )

    output_path_value = job.get("output_path")
    deleted_file = False
    if output_path_value:
        output_path = Path(str(output_path_value)).resolve()
        exports_dir = (settings.data_dir / "exports").resolve()
        if exports_dir not in output_path.parents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid export path.",
            )
        if output_path.exists():
            if not output_path.is_file():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid export path.",
                )
            output_path.unlink()
            deleted_file = True

    conn.execute("DELETE FROM export_jobs WHERE job_uuid = ?", (job_uuid,))
    return {
        "ok": True,
        "job_uuid": job_uuid,
        "deleted_file": deleted_file,
    }


def recover_stale_export_jobs(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    job_uuid: str | None = None,
) -> list[str]:
    parameters: tuple[object, ...] = ()
    job_filter = ""
    if job_uuid is not None:
        job_filter = "AND job_uuid = ?"
        parameters = (job_uuid,)
    stale_rows = conn.execute(
        f"""
        SELECT {_EXPORT_JOB_COLUMNS}
        FROM export_jobs
        WHERE status = 'running'
          AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
          {job_filter}
        ORDER BY id
        """,
        parameters,
    ).fetchall()
    recovered_jobs: list[str] = []
    for row in stale_rows:
        job = _row_to_export_job(row)
        publication_complete = _published_archive_matches(job, settings=settings)
        with transaction(conn):
            if publication_complete:
                cursor = conn.execute(
                    """
                    UPDATE export_jobs
                    SET status = 'succeeded',
                        progress_message = 'Export completed.',
                        output_path = canonical_path,
                        completed_at = CURRENT_TIMESTAMP,
                        error_message = NULL,
                        failure_kind = NULL,
                        publication_state = 'published',
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        staging_path = NULL
                    WHERE job_uuid = ?
                      AND status = 'running'
                      AND lease_token IS ?
                      AND publication_state = 'publishing'
                      AND publication_token IS ?
                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    """,
                    (
                        job["job_uuid"],
                        job["lease_token"],
                        job["publication_token"],
                    ),
                )
            elif int(job["attempt_count"]) >= EXPORT_JOB_MAX_ATTEMPTS:
                cursor = conn.execute(
                    """
                    UPDATE export_jobs
                    SET status = 'failed',
                        progress_message = 'Export failed.',
                        output_path = NULL,
                        completed_at = CURRENT_TIMESTAMP,
                        error_message = 'Export retry limit exhausted.',
                        failure_kind = 'terminal',
                        publication_state = 'unpublished',
                        publication_token = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL
                    WHERE job_uuid = ?
                      AND status = 'running'
                      AND lease_token IS ?
                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    """,
                    (job["job_uuid"], job["lease_token"]),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE export_jobs
                    SET status = 'queued',
                        progress_message = 'Recovered expired export lease.',
                        output_path = NULL,
                        completed_at = NULL,
                        error_message = NULL,
                        failure_kind = 'recoverable',
                        publication_state = 'unpublished',
                        publication_token = NULL,
                        archive_sha256 = NULL,
                        lease_owner = NULL,
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        heartbeat_at = NULL,
                        staging_path = NULL
                    WHERE job_uuid = ?
                      AND status = 'running'
                      AND lease_token IS ?
                      AND (lease_expires_at IS NULL OR lease_expires_at <= CURRENT_TIMESTAMP)
                    """,
                    (job["job_uuid"], job["lease_token"]),
                )
        if cursor.rowcount == 1:
            recovered_jobs.append(str(job["job_uuid"]))

    exhausted_filter = ""
    exhausted_parameters: tuple[object, ...] = (EXPORT_JOB_MAX_ATTEMPTS,)
    if job_uuid is not None:
        exhausted_filter = "AND job_uuid = ?"
        exhausted_parameters = (EXPORT_JOB_MAX_ATTEMPTS, job_uuid)
    with transaction(conn):
        conn.execute(
            f"""
            UPDATE export_jobs
            SET status = 'failed',
                progress_message = 'Export failed.',
                completed_at = CURRENT_TIMESTAMP,
                error_message = 'Export retry limit exhausted.',
                failure_kind = 'terminal'
            WHERE status = 'queued'
              AND attempt_count >= ?
              {exhausted_filter}
            """,
            exhausted_parameters,
        )
    return recovered_jobs


def claim_export_job(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    job_uuid: str | None = None,
    worker_id: str | None = None,
) -> dict[str, object] | None:
    recover_stale_export_jobs(conn, settings=settings, job_uuid=job_uuid)
    resolved_worker_id = _normalize_worker_id(worker_id or f"worker-{uuid4().hex}")
    lease_token = uuid4().hex
    with transaction(conn):
        parameters: tuple[object, ...] = ()
        job_filter = ""
        if job_uuid is not None:
            job_filter = "AND job_uuid = ?"
            parameters = (job_uuid,)
        queued_row = conn.execute(
            f"""
            SELECT id, job_uuid, export_type
            FROM export_jobs
            WHERE status = 'queued'
              AND attempt_count < {EXPORT_JOB_MAX_ATTEMPTS}
              {job_filter}
            ORDER BY id
            LIMIT 1
            """,
            parameters,
        ).fetchone()
        if queued_row is None:
            return None
        exports_dir = settings.data_dir / "exports"
        job_component = safe_filename_component(queued_row["job_uuid"], fallback="export")
        type_component = safe_filename_component(queued_row["export_type"], fallback="data")
        canonical_path = exports_dir / f"{job_component}_{type_component}.zip"
        staging_path = exports_dir / (
            f".{job_component}_{type_component}.{lease_token}.staging.zip"
        )
        claimed_row = conn.execute(
            f"""
            UPDATE export_jobs
            SET status = 'running',
                progress_message = 'Generating export archive.',
                started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                completed_at = NULL,
                output_path = NULL,
                error_message = NULL,
                lease_owner = ?,
                lease_token = ?,
                lease_expires_at = datetime('now', '+{EXPORT_JOB_LEASE_SECONDS} seconds'),
                heartbeat_at = CURRENT_TIMESTAMP,
                attempt_count = attempt_count + 1,
                publication_state = 'unpublished',
                publication_token = NULL,
                staging_path = ?,
                canonical_path = ?,
                archive_sha256 = NULL
            WHERE id = ?
              AND status = 'queued'
              AND attempt_count < {EXPORT_JOB_MAX_ATTEMPTS}
            RETURNING {_EXPORT_JOB_COLUMNS}
            """,
            (
                resolved_worker_id,
                lease_token,
                str(staging_path),
                str(canonical_path),
                int(queued_row["id"]),
            ),
        ).fetchone()
    if claimed_row is None:
        return None
    return _row_to_export_job(claimed_row)


def renew_export_job_lease(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    lease_token: str,
) -> bool:
    cursor = conn.execute(
        f"""
        UPDATE export_jobs
        SET heartbeat_at = CURRENT_TIMESTAMP,
            lease_expires_at = datetime('now', '+{EXPORT_JOB_LEASE_SECONDS} seconds')
        WHERE job_uuid = ?
          AND status = 'running'
          AND lease_token = ?
          AND lease_expires_at > CURRENT_TIMESTAMP
        """,
        (job_uuid, lease_token),
    )
    return cursor.rowcount == 1


def finalize_export_job_failure(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    lease_token: str,
    error_message: str,
) -> bool:
    cursor = conn.execute(
        """
        UPDATE export_jobs
        SET status = 'failed',
            progress_message = 'Export failed.',
            completed_at = CURRENT_TIMESTAMP,
            error_message = ?,
            output_path = NULL,
            failure_kind = 'terminal',
            publication_state = 'unpublished',
            publication_token = NULL,
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL
        WHERE job_uuid = ?
          AND status = 'running'
          AND lease_token = ?
          AND lease_expires_at > CURRENT_TIMESTAMP
        """,
        (_sanitize_error_message(error_message), job_uuid, lease_token),
    )
    return cursor.rowcount == 1


def run_export_job(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    job_uuid: str,
) -> dict[str, object]:
    claim = claim_export_job(
        conn,
        settings=settings,
        job_uuid=job_uuid,
    )
    if claim is None:
        job = get_export_job(conn, job_uuid=job_uuid)
        if job["status"] == "running":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Export job is already running.",
            )
        return job

    lease_token = str(claim["lease_token"])
    staging_path = Path(str(claim["staging_path"]))
    canonical_path = Path(str(claim["canonical_path"]))
    staging_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat = _ExportJobHeartbeat(
        settings=settings,
        job_uuid=job_uuid,
        lease_token=lease_token,
    )
    heartbeat.start()
    try:
        _generate_staging_archive(
            conn,
            settings=settings,
            claim=claim,
            staging_path=staging_path,
        )
        archive_sha256 = _hash_file(staging_path)
        with transaction(conn):
            prepared = conn.execute(
                """
                UPDATE export_jobs
                SET progress_message = 'Publishing export archive.',
                    publication_state = 'publishing',
                    publication_token = lease_token,
                    archive_sha256 = ?
                WHERE job_uuid = ?
                  AND status = 'running'
                  AND lease_token = ?
                  AND lease_expires_at > CURRENT_TIMESTAMP
                  AND staging_path = ?
                """,
                (archive_sha256, job_uuid, lease_token, str(staging_path)),
            ).rowcount == 1
        if not prepared or heartbeat.ownership_lost:
            raise ExportJobOwnershipLost("Export job lease ownership was lost.")
        _publish_staging_archive(
            conn,
            job_uuid=job_uuid,
            lease_token=lease_token,
            staging_path=staging_path,
            canonical_path=canonical_path,
        )
    except Exception as exc:
        return _reconcile_export_job_after_worker_error(
            settings=settings,
            job_uuid=job_uuid,
            lease_token=lease_token,
            canonical_path=canonical_path,
            error_message=_operator_error_message(exc),
        )
    finally:
        heartbeat.stop()
    return get_export_job(conn, job_uuid=job_uuid)


def run_export_job_background(
    *,
    settings: Settings,
    job_uuid: str,
) -> None:
    conn = get_connection(settings)
    try:
        try:
            run_export_job(conn, settings=settings, job_uuid=job_uuid)
        except HTTPException as exc:
            if exc.status_code != status.HTTP_409_CONFLICT:
                raise
    finally:
        conn.close()


def drain_recoverable_export_jobs(
    settings: Settings,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    while stop_event is None or not stop_event.is_set():
        conn = get_connection(settings)
        try:
            recover_stale_export_jobs(conn, settings=settings)
            row = conn.execute(
                "SELECT job_uuid FROM export_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return
            try:
                run_export_job(
                    conn,
                    settings=settings,
                    job_uuid=str(row["job_uuid"]),
                )
            except HTTPException as exc:
                if exc.status_code != status.HTTP_409_CONFLICT:
                    raise
        finally:
            conn.close()


def start_export_job_recovery(
    settings: Settings,
    *,
    poll_seconds: float = EXPORT_JOB_RECOVERY_POLL_SECONDS,
) -> ExportJobRecoverySupervisor:
    supervisor = ExportJobRecoverySupervisor(
        settings=settings,
        poll_seconds=poll_seconds,
    )
    supervisor.start()
    return supervisor


def _generate_staging_archive(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    claim: dict[str, object],
    staging_path: Path,
) -> None:
    date_range = normalize_export_filters(claim["filters"])
    export_type = claim["export_type"]
    if export_type == "experiment_data":
        create_v2_export(
            conn,
            settings,
            staging_path,
            include_test=bool(claim["include_test"]),
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
    elif export_type == "complete_no_external_error_data":
        create_clean_data_export(
            conn,
            settings,
            staging_path,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
    elif export_type == "reimbursement":
        create_reimbursement_export(
            conn,
            settings,
            staging_path,
            start_date=date_range.start_date,
            end_date=date_range.end_date,
        )
    else:
        raise ValueError("Unsupported export type.")


def _publish_staging_archive(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    lease_token: str,
    staging_path: Path,
    canonical_path: Path,
) -> None:
    _fsync_file(staging_path)
    with _immediate_transaction(conn):
        ownership = conn.execute(
            """
            SELECT 1
            FROM export_jobs
            WHERE job_uuid = ?
              AND status = 'running'
              AND lease_token = ?
              AND lease_expires_at > CURRENT_TIMESTAMP
              AND publication_state = 'publishing'
              AND publication_token = ?
              AND staging_path = ?
              AND canonical_path = ?
            """,
            (
                job_uuid,
                lease_token,
                lease_token,
                str(staging_path),
                str(canonical_path),
            ),
        ).fetchone()
        if ownership is None:
            raise ExportJobOwnershipLost("Export job lease ownership was lost.")
        os.replace(staging_path, canonical_path)
        _fsync_directory(canonical_path.parent)
        _mark_export_job_succeeded(
            conn,
            job_uuid=job_uuid,
            lease_token=lease_token,
            output_path=canonical_path,
        )


def _mark_export_job_succeeded(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    lease_token: str,
    output_path: Path,
) -> None:
    cursor = conn.execute(
        """
        UPDATE export_jobs
        SET status = 'succeeded',
            progress_message = 'Export completed.',
            output_path = ?,
            completed_at = CURRENT_TIMESTAMP,
            error_message = NULL,
            failure_kind = NULL,
            publication_state = 'published',
            lease_owner = NULL,
            lease_token = NULL,
            lease_expires_at = NULL,
            heartbeat_at = NULL,
            staging_path = NULL
        WHERE job_uuid = ?
          AND status = 'running'
          AND lease_token = ?
          AND lease_expires_at > CURRENT_TIMESTAMP
          AND publication_state = 'publishing'
          AND publication_token = ?
        """,
        (str(output_path), job_uuid, lease_token, lease_token),
    )
    if cursor.rowcount != 1:
        raise ExportJobOwnershipLost("Export job lease ownership was lost.")


def _reconcile_export_job_after_worker_error(
    *,
    settings: Settings,
    job_uuid: str,
    lease_token: str,
    canonical_path: Path,
    error_message: str,
) -> dict[str, object]:
    conn = get_connection(settings)
    try:
        current_job = get_export_job(conn, job_uuid=job_uuid)
        if _published_archive_matches(current_job, settings=settings):
            try:
                _reconcile_claimed_publication(
                    conn,
                    job_uuid=job_uuid,
                    lease_token=lease_token,
                    output_path=canonical_path,
                )
            except Exception:
                pass
        else:
            finalize_export_job_failure(
                conn,
                job_uuid=job_uuid,
                lease_token=lease_token,
                error_message=error_message,
            )
        return get_export_job(conn, job_uuid=job_uuid)
    finally:
        conn.close()


def _reconcile_claimed_publication(
    conn: sqlite3.Connection,
    *,
    job_uuid: str,
    lease_token: str,
    output_path: Path,
) -> None:
    with _immediate_transaction(conn):
        ownership = conn.execute(
            """
            SELECT 1
            FROM export_jobs
            WHERE job_uuid = ?
              AND status = 'running'
              AND lease_token = ?
              AND lease_expires_at > CURRENT_TIMESTAMP
              AND publication_state = 'publishing'
              AND publication_token = ?
              AND canonical_path = ?
            """,
            (job_uuid, lease_token, lease_token, str(output_path)),
        ).fetchone()
        if ownership is None:
            raise ExportJobOwnershipLost("Export job lease ownership was lost.")
        _mark_export_job_succeeded(
            conn,
            job_uuid=job_uuid,
            lease_token=lease_token,
            output_path=output_path,
        )


def _published_archive_matches(job: dict[str, object], *, settings: Settings) -> bool:
    if job.get("publication_state") != "publishing":
        return False
    if not job.get("publication_token") or not job.get("archive_sha256"):
        return False
    canonical_value = job.get("canonical_path")
    if not canonical_value:
        return False
    canonical_path = Path(str(canonical_value)).resolve()
    exports_dir = (settings.data_dir / "exports").resolve()
    if exports_dir not in canonical_path.parents or not canonical_path.is_file():
        return False
    try:
        return _hash_file(canonical_path) == job["archive_sha256"]
    except OSError:
        return False


def _row_to_export_job(row: sqlite3.Row) -> dict[str, object]:
    export_type = row["export_type"]
    return {
        "job_uuid": row["job_uuid"],
        "export_type": export_type,
        "filters": _json_loads(row["filters_json"]) or {},
        "include_test": False if export_type == "reimbursement" else bool(row["include_test"]),
        "status": row["status"],
        "progress_message": row["progress_message"],
        "output_path": row["output_path"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_message": row["error_message"],
        "lease_owner": row["lease_owner"],
        "lease_token": row["lease_token"],
        "lease_expires_at": row["lease_expires_at"],
        "heartbeat_at": row["heartbeat_at"],
        "attempt_count": int(row["attempt_count"]),
        "failure_kind": row["failure_kind"],
        "publication_state": row["publication_state"],
        "publication_token": row["publication_token"],
        "staging_path": row["staging_path"],
        "canonical_path": row["canonical_path"],
        "archive_sha256": row["archive_sha256"],
    }


def _normalize_worker_id(value: str) -> str:
    normalized = _WORKER_ID_PATTERN.sub("-", value.strip()).strip("-._")
    return (normalized or "worker")[:96]


def _sanitize_error_message(value: str) -> str:
    normalized = " ".join(value.replace("\x00", "").split())
    return (normalized or "Export generation failed.")[:EXPORT_JOB_ERROR_MESSAGE_LIMIT]


def _operator_error_message(exc: Exception) -> str:
    if isinstance(exc, (ExportEvidenceError, ValueError)):
        return _sanitize_error_message(str(exc))
    return _sanitize_error_message(
        f"Export generation failed ({exc.__class__.__name__})."
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _immediate_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.rollback()
        raise
    else:
        try:
            conn.commit()
        except BaseException:
            conn.rollback()
            raise


def _json_loads(value: str | None) -> object | None:
    if value is None:
        return None
    return json.loads(value)
