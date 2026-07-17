from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any
from uuid import uuid4

from backend.app.models.domain import (
    ADMIN_ACTIONS,
    CLEAN_DATA_AUDIT_STATUSES,
    CONDITIONS,
    ERROR_TYPE_IDS,
    PARTICIPANT_TYPES,
    SUBCONDITIONS,
)
from backend.app.db import transaction
from backend.app.security import mask_phone
from backend.app.services.api_health import ApiHealthService
from backend.app.services.assignment import (
    COUNTED_ATTEMPT_STATUSES,
    COUNTED_ATTEMPT_STATUS_PLACEHOLDERS,
    INTERNAL_TEST_PHONE_HASH,
    preview_assignment_for_participant_type,
)
from backend.app.services.clean_data import (
    audit_participant_clean_data,
    persist_clean_data_audit,
)
from backend.app.services.export import INTERNAL_TEST_PARTICIPANT_NAME, create_v2_export
from backend.app.services.provider_testing import ProviderTestService
from backend.app.services.providers import normalize_provider_error_code
from backend.app.settings import Settings
from backend.app.time_utils import current_shanghai_date, shanghai_date_from_timestamp


@dataclass(frozen=True)
class _FormalDataPredicate:
    internal_phone_hash: str
    internal_participant_name: str

    @property
    def parameters(self) -> dict[str, str]:
        return {
            "internal_phone_hash": self.internal_phone_hash,
            "internal_participant_name": self.internal_participant_name,
        }

    def participant(self, alias: str = "p") -> str:
        return (
            f"{alias}.phone_hash != :internal_phone_hash "
            f"AND {alias}.name != :internal_participant_name"
        )

    def session(self, session_alias: str = "es", participant_alias: str = "p") -> str:
        return (
            f"{self.participant(participant_alias)} "
            f"AND {session_alias}.is_test = 0"
        )

    def api_call(
        self,
        log_alias: str = "acl",
        session_alias: str = "es",
        participant_alias: str = "p",
    ) -> str:
        return (
            f"{self.session(session_alias, participant_alias)} "
            f"AND COALESCE({log_alias}.is_test, {session_alias}.is_test) = 0"
        )


FORMAL_DATA = _FormalDataPredicate(
    internal_phone_hash=INTERNAL_TEST_PHONE_HASH,
    internal_participant_name=INTERNAL_TEST_PARTICIPANT_NAME,
)


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _count_matching_files(directory: Path) -> tuple[int, int]:
    if not directory.exists():
        return 0, 0
    total_size = 0
    total_files = 0
    for path in directory.rglob("*"):
        if path.is_file():
            total_files += 1
            total_size += path.stat().st_size
    return total_files, total_size


def _count_formal_export_files(
    conn: sqlite3.Connection,
    *,
    exports_dir: Path,
) -> tuple[int, int]:
    rows = conn.execute(
        """
        SELECT output_path
        FROM export_jobs
        WHERE include_test = 0
          AND status = 'succeeded'
          AND publication_state = 'published'
          AND output_path IS NOT NULL
        """
    ).fetchall()
    resolved_exports_dir = exports_dir.resolve()
    formal_paths: set[Path] = set()
    for row in rows:
        export_path = Path(str(row["output_path"])).resolve()
        if export_path.is_relative_to(resolved_exports_dir) and export_path.is_file():
            formal_paths.add(export_path)
    return len(formal_paths), sum(path.stat().st_size for path in formal_paths)


def _nullable_tuple_sort_key(values: tuple[Any, ...]) -> tuple[tuple[bool, str], ...]:
    return tuple((value is None, "" if value is None else str(value)) for value in values)


def _percent(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round((numerator / denominator) * 100, 1)


def _p95(latencies: list[int]) -> int | None:
    if not latencies:
        return None
    ordered = sorted(latencies)
    index = max(0, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _sqlite_now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _is_shanghai_reporting_date(value: str | None, expected_date: str) -> bool:
    if value is None:
        return False
    try:
        return shanghai_date_from_timestamp(value) == expected_date
    except ValueError:
        return False


MAX_ASSIGNMENT_BATCH_CELLS = (
    len(PARTICIPANT_TYPES) * len(CONDITIONS) * len(SUBCONDITIONS) * len(ERROR_TYPE_IDS)
)
ASSIGNMENT_FILTER_FIELDS = (
    "participant_type",
    "condition",
    "subcondition",
    "error_type_id",
    "enabled",
    "cap_status",
)


class AssignmentBatchConflictError(RuntimeError):
    pass


class AdminRepository:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings,
    ) -> None:
        self._conn = conn
        self._settings = settings

    def record_event(
        self,
        *,
        admin_user: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        if action not in ADMIN_ACTIONS:
            raise ValueError("Unsupported admin action.")
        cursor = self._conn.execute(
            """
            INSERT INTO admin_events (
                admin_user,
                action,
                target_type,
                target_id,
                payload_json
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                admin_user,
                action,
                target_type,
                target_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if payload is not None
                else None,
            ),
        )
        return int(cursor.lastrowid)

    def get_overview_metrics(self) -> dict[str, Any]:
        today = current_shanghai_date()
        total_participants = self._conn.execute(
            f"""
            SELECT COUNT(*) AS total_participants
            FROM participants p
            WHERE {FORMAL_DATA.participant()}
            """,
            FORMAL_DATA.parameters,
        ).fetchone()
        participant_counts = self._conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN pa.participant_type = 'short' AND pa.status = 'completed' THEN 1 ELSE 0 END) AS short_completed,
                SUM(CASE WHEN pa.participant_type = 'long' AND pa.status = 'completed' THEN 1 ELSE 0 END) AS long_completed
            FROM participants p
            JOIN participant_attempts pa ON pa.id = p.current_attempt_id
            WHERE {FORMAL_DATA.participant()}
            """,
            FORMAL_DATA.parameters,
        ).fetchone()
        session_counts = self._conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN es.status = 'completed' THEN 1 ELSE 0 END) AS completed_sessions,
                SUM(CASE WHEN es.status = 'started' THEN 1 ELSE 0 END) AS active_sessions
            FROM experiment_sessions es
            JOIN participants p ON p.id = es.participant_id
            WHERE {FORMAL_DATA.session()}
            """,
            FORMAL_DATA.parameters,
        ).fetchone()
        day_rows = self._conn.execute(
            f"""
            SELECT pd.started_at, pd.completed_at
            FROM participant_days pd
            JOIN participants p ON p.id = pd.participant_id
            WHERE {FORMAL_DATA.participant()}
            """,
            FORMAL_DATA.parameters,
        ).fetchall()
        risk_count_row = self._conn.execute(
            f"""
            SELECT COUNT(DISTINCT srf.session_id) AS risk_sessions
            FROM session_risk_flags srf
            JOIN experiment_sessions es ON es.id = srf.session_id
            JOIN participants p ON p.id = es.participant_id
            WHERE {FORMAL_DATA.session()}
            """,
            FORMAL_DATA.parameters,
        ).fetchone()
        api_failure_row = self._conn.execute(
            f"""
            SELECT
                SUM(CASE WHEN acl.status != 'success' THEN 1 ELSE 0 END) AS api_failures,
                SUM(CASE WHEN acl.route = 'asr' AND acl.status != 'success' THEN 1 ELSE 0 END) AS asr_failures
            FROM api_call_logs acl
            JOIN experiment_sessions es ON es.id = acl.session_id
            JOIN participants p ON p.id = es.participant_id
            WHERE {FORMAL_DATA.api_call()}
            """,
            FORMAL_DATA.parameters,
        ).fetchone()
        cell_counts = {
            (row["condition"], row["subcondition"]): row["participant_count"]
            for row in self._conn.execute(
                f"""
                SELECT pa.condition, pa.subcondition, COUNT(*) AS participant_count
                FROM participants p
                JOIN participant_attempts pa ON pa.id = p.current_attempt_id
                WHERE {FORMAL_DATA.participant()}
                GROUP BY pa.condition, pa.subcondition
                """,
                FORMAL_DATA.parameters,
            ).fetchall()
        }
        matrix_rows: list[dict[str, Any]] = []
        for condition in CONDITIONS:
            cells: list[dict[str, Any]] = []
            for subcondition in SUBCONDITIONS:
                cells.append(
                    {
                        "condition": condition,
                        "subcondition": subcondition,
                        "count": int(cell_counts.get((condition, subcondition), 0)),
                    }
                )
            matrix_rows.append({"condition": condition, "cells": cells})
        return {
            "today_started": sum(
                _is_shanghai_reporting_date(row["started_at"], today)
                for row in day_rows
            ),
            "today_completed": sum(
                _is_shanghai_reporting_date(row["completed_at"], today)
                for row in day_rows
            ),
            "total_participants": int(total_participants["total_participants"] or 0),
            "completed_sessions": int(session_counts["completed_sessions"] or 0),
            "active_sessions": int(session_counts["active_sessions"] or 0),
            "risk_sessions": int(risk_count_row["risk_sessions"] or 0),
            "completion_by_type": {
                "short": int(participant_counts["short_completed"] or 0),
                "long": int(participant_counts["long_completed"] or 0),
            },
            "assignment_matrix": matrix_rows,
            "api_failures": int(api_failure_row["api_failures"] or 0),
            "asr_failures": int(api_failure_row["asr_failures"] or 0),
        }

    def get_system_metrics(self) -> dict[str, Any]:
        overview = self.get_overview_metrics()
        db_path = Path(self._settings.database_url.replace("sqlite:///", "", 1))
        db_size = db_path.stat().st_size if db_path.exists() else 0
        audio_dir = self._settings.data_dir / "audio"
        exports_dir = self._settings.data_dir / "exports"
        audio_files, audio_size = _count_matching_files(audio_dir)
        export_files, export_size = _count_formal_export_files(
            self._conn,
            exports_dir=exports_dir,
        )
        disk_usage = shutil.disk_usage(self._settings.data_dir)
        return {
            "generated_at": _sqlite_now(),
            "service": {
                "status": "ok",
                "label": "FastAPI application is responding",
            },
            "database": {
                "path": db_path.name or "sqlite",
                "size_bytes": db_size,
            },
            "data_directory": {
                "path": self._settings.data_dir.name or "data",
                "disk_usage": {
                    "total_bytes": disk_usage.total,
                    "used_bytes": disk_usage.used,
                    "free_bytes": disk_usage.free,
                },
            },
            "audio_directory": {
                "path": str(audio_dir.relative_to(self._settings.data_dir))
                if audio_dir.exists()
                else "audio",
                "files": audio_files,
                "size_bytes": audio_size,
            },
            "exports_directory": {
                "path": str(exports_dir.relative_to(self._settings.data_dir)),
                "files": export_files,
                "size_bytes": export_size,
            },
            "experiment": {
                "today_started": overview["today_started"],
                "today_completed": overview["today_completed"],
                "risk_sessions": overview["risk_sessions"],
                "api_failures": overview["api_failures"],
                "asr_failures": overview["asr_failures"],
            },
            "host_metrics": {
                "cpu": {"supported": False, "reason": "psutil is not installed."},
                "memory": {"supported": False, "reason": "psutil is not installed."},
                "network": {"supported": False, "reason": "psutil is not installed."},
                "security_events": {
                    "supported": False,
                    "reason": "systemd journal parsing is not enabled in this deployment.",
                },
            },
        }

    def get_data_monitor_summary(self) -> dict[str, Any]:
        overview = self.get_overview_metrics()
        clean_rows = self._conn.execute(
            f"""
            SELECT status, COUNT(*) AS count
            FROM clean_data_audits cda
            JOIN participants p ON p.id = cda.participant_id
            WHERE {FORMAL_DATA.participant()}
            GROUP BY status
            """,
            FORMAL_DATA.parameters,
        ).fetchall()
        clean_counts = {
            "eligible": 0,
            "review_needed": 0,
            "excluded": 0,
        }
        clean_counts.update({row["status"]: int(row["count"]) for row in clean_rows})
        incomplete_rows = self._conn.execute(
            f"""
            SELECT
                p.id AS participant_id,
                p.name,
                p.phone,
                p.phone_hash,
                pa.id AS attempt_id,
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id,
                pa.status AS attempt_status,
                es.session_uuid,
                d.day_index,
                es.status AS session_status,
                es.started_at,
                es.completed_at,
                es.updated_at
            FROM experiment_sessions es
            JOIN participant_days d ON d.id = es.participant_day_id
            JOIN participants p ON p.id = es.participant_id
            LEFT JOIN participant_attempts pa ON pa.id = es.attempt_id
            WHERE es.status != 'completed'
              AND {FORMAL_DATA.session()}
            ORDER BY es.updated_at DESC, es.id DESC
            LIMIT 50
            """,
            FORMAL_DATA.parameters,
        ).fetchall()
        recent_rows = self._conn.execute(
            f"""
            SELECT
                p.id AS participant_id,
                p.name,
                p.phone,
                p.phone_hash,
                pa.id AS attempt_id,
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id,
                pa.status AS attempt_status,
                es.session_uuid,
                d.day_index,
                es.status AS session_status,
                es.started_at,
                es.completed_at,
                es.updated_at
            FROM experiment_sessions es
            JOIN participant_days d ON d.id = es.participant_day_id
            JOIN participants p ON p.id = es.participant_id
            LEFT JOIN participant_attempts pa ON pa.id = es.attempt_id
            WHERE {FORMAL_DATA.session()}
            ORDER BY es.updated_at DESC, es.id DESC
            LIMIT 20
            """,
            FORMAL_DATA.parameters,
        ).fetchall()
        risk_rows = self._conn.execute(
            f"""
            SELECT
                p.id AS participant_id,
                p.name,
                p.phone,
                p.phone_hash,
                pa.id AS attempt_id,
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id,
                pa.status AS attempt_status,
                es.session_uuid,
                d.day_index,
                es.status AS session_status,
                es.started_at,
                es.completed_at,
                es.updated_at,
                GROUP_CONCAT(srf.flag, ',') AS risk_flags
            FROM session_risk_flags srf
            JOIN experiment_sessions es ON es.id = srf.session_id
            JOIN participant_days d ON d.id = es.participant_day_id
            JOIN participants p ON p.id = es.participant_id
            LEFT JOIN participant_attempts pa ON pa.id = es.attempt_id
            WHERE {FORMAL_DATA.session()}
            GROUP BY es.id
            ORDER BY MAX(srf.created_at) DESC
            LIMIT 50
            """,
            FORMAL_DATA.parameters,
        ).fetchall()
        return {
            "generated_at": _sqlite_now(),
            "metrics": {
                **overview,
                "short_completed": overview["completion_by_type"]["short"],
                "long_completed": overview["completion_by_type"]["long"],
                "clean_data_eligible": clean_counts["eligible"],
                "clean_data_review_needed": clean_counts["review_needed"],
                "clean_data_excluded": clean_counts["excluded"],
            },
            "incomplete_sessions": [
                self._monitor_session_row(row)
                for row in incomplete_rows
            ],
            "recent_sessions": [
                self._monitor_session_row(row)
                for row in recent_rows
            ],
            "risk_sessions": [
                {
                    **self._monitor_session_row(row),
                    "risk_flags": [
                        flag
                        for flag in str(row["risk_flags"] or "").split(",")
                        if flag
                    ],
                }
                for row in risk_rows
            ],
            "notes": [
                "Online participant heartbeat is not available in interface_v2; recent and incomplete sessions are shown instead.",
            ],
        }

    def search_participants(self, *, query: str | None = "", limit: int = 100) -> dict[str, Any]:
        normalized_query = (query or "").strip()
        if not normalized_query:
            return {"query": "", "count": 0, "items": []}

        clauses: list[str] = []
        params: list[Any] = []
        clauses.append(
            """
            (
                CAST(p.id AS TEXT) = ?
                OR p.name LIKE ?
                OR substr(p.phone, -4) = ?
            )
            """
        )
        params.extend(
            [
                normalized_query,
                f"%{normalized_query}%",
                normalized_query[-4:],
            ]
        )
        clauses.append("p.phone_hash != ?")
        params.append(INTERNAL_TEST_PHONE_HASH)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"""
            SELECT
                p.id,
                p.name,
                p.phone,
                p.phone_hash,
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id,
                pa.status AS current_status,
                p.created_at
            FROM participants p
            JOIN participant_attempts pa ON pa.id = p.current_attempt_id
            {where_sql}
            ORDER BY p.id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        items = [
            {
                "participant_id": int(row["id"]),
                "name": row["name"],
                "masked_phone": mask_phone(row["phone"]),
                "phone_hash": row["phone_hash"],
                "participant_type": row["participant_type"],
                "condition": row["condition"],
                "subcondition": row["subcondition"],
                "topic_key": row["topic_key"],
                "error_type_id": row["error_type_id"],
                "current_status": row["current_status"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]
        return {"query": normalized_query, "count": len(items), "items": items}

    def list_clean_data_audits(
        self,
        status: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        normalized_status = status.strip()
        if normalized_status and normalized_status not in CLEAN_DATA_AUDIT_STATUSES:
            raise ValueError("Unsupported clean data audit status.")

        clauses = ["p.phone_hash != ?"]
        params: list[Any] = [INTERNAL_TEST_PHONE_HASH]
        if normalized_status:
            clauses.append("cda.status = ?")
            params.append(normalized_status)
        rows = self._conn.execute(
            f"""
            SELECT
                cda.participant_id,
                cda.attempt_id,
                p.name,
                p.phone_hash,
                pa.participant_type,
                pa.condition,
                pa.subcondition,
                pa.topic_key,
                pa.error_type_id,
                cda.status,
                cda.reasons_json,
                cda.reviewer_note,
                cda.reviewed_by,
                cda.reviewed_at,
                cda.computed_at
            FROM clean_data_audits cda
            JOIN participants p ON p.id = cda.participant_id
            LEFT JOIN participant_attempts pa ON pa.id = cda.attempt_id
            WHERE {' AND '.join(clauses)}
            ORDER BY cda.computed_at DESC, cda.id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        items = [self._clean_data_audit_row(row) for row in rows]
        last_updated_at = self._latest_clean_data_audit_updated_at()
        return {
            "status": normalized_status or "all",
            "count": len(items),
            "last_updated_at": last_updated_at,
            "items": items,
        }

    def recompute_clean_data_audits(self, *, admin_user: str) -> dict[str, Any]:
        participant_rows = self._conn.execute(
            """
            SELECT id
            FROM participants
            WHERE phone_hash != ?
            ORDER BY id ASC
            """,
            (INTERNAL_TEST_PHONE_HASH,),
        ).fetchall()
        status_counts = {status: 0 for status in CLEAN_DATA_AUDIT_STATUSES}

        persisted_participant_ids: list[int] = []
        for row in participant_rows:
            participant_id = int(row["id"])
            for _ in range(3):
                data_version = int(
                    self._conn.execute("PRAGMA data_version").fetchone()[0]
                )
                result = audit_participant_clean_data(
                    self._conn,
                    settings=self._settings,
                    participant_id=participant_id,
                )
                persisted = False
                with transaction(self._conn):
                    current_data_version = int(
                        self._conn.execute("PRAGMA data_version").fetchone()[0]
                    )
                    if current_data_version == data_version:
                        persist_clean_data_audit(
                            self._conn,
                            participant_id=participant_id,
                            result=result,
                        )
                        persisted = True
                if persisted:
                    persisted_participant_ids.append(participant_id)
                    status_counts[result.status] = status_counts.get(result.status, 0) + 1
                    break

        with transaction(self._conn):
            self.record_event(
                admin_user=admin_user,
                action="export_data",
                target_type="clean_data_audit_recompute",
                target_id="all",
                payload={
                    "operation": "recompute_clean_data_audits",
                    "scanned": len(participant_rows),
                    "persisted": len(persisted_participant_ids),
                    "status_counts": status_counts,
                },
            )

        items: list[dict[str, Any]] = []
        if persisted_participant_ids:
            participant_ids = persisted_participant_ids
            placeholders = ",".join("?" for _ in participant_ids)
            rows = self._conn.execute(
                f"""
                SELECT
                    cda.participant_id,
                    cda.attempt_id,
                    p.name,
                    p.phone_hash,
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.topic_key,
                    pa.error_type_id,
                    cda.status,
                    cda.reasons_json,
                    cda.reviewer_note,
                    cda.reviewed_by,
                    cda.reviewed_at,
                    cda.computed_at
                FROM clean_data_audits cda
                JOIN participants p ON p.id = cda.participant_id
                LEFT JOIN participant_attempts pa ON pa.id = cda.attempt_id
                WHERE cda.participant_id IN ({placeholders})
                  AND p.phone_hash != ?
                ORDER BY cda.computed_at DESC, cda.id DESC
                """,
                (*participant_ids, INTERNAL_TEST_PHONE_HASH),
            ).fetchall()
            items = [self._clean_data_audit_row(row) for row in rows]
        return {
            "summary": {
                "scanned": len(participant_rows),
                "persisted": len(persisted_participant_ids),
                "status_counts": status_counts,
            },
            "last_updated_at": self._latest_clean_data_audit_updated_at(),
            "count": len(items),
            "items": items,
        }

    def _latest_clean_data_audit_updated_at(self) -> str | None:
        row = self._conn.execute(
            "SELECT MAX(computed_at) AS last_updated_at FROM clean_data_audits"
        ).fetchone()
        if row is None:
            return None
        value = row["last_updated_at"]
        return str(value) if value else None

    def get_assignment_control_summary(self) -> dict[str, Any]:
        counts = {
            (
                row["participant_type"],
                row["condition"],
                row["subcondition"],
                row["error_type_id"],
            ): int(
                row["participant_count"]
            )
            for row in self._conn.execute(
                f"""
                SELECT
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.error_type_id,
                    COUNT(*) AS participant_count
                FROM participants p
                JOIN participant_attempts pa ON pa.id = p.current_attempt_id
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
        active_assignment_counts = {
            (
                row["participant_type"],
                row["condition"],
                row["subcondition"],
                row["error_type_id"],
            ): int(row["participant_count"])
            for row in self._conn.execute(
                """
                SELECT
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.error_type_id,
                    COUNT(*) AS participant_count
                FROM participants p
                JOIN participant_attempts pa ON pa.id = p.current_attempt_id
                WHERE
                    pa.valid_for_export = 1
                    AND pa.status = 'active'
                    AND p.phone_hash != ?
                GROUP BY
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.error_type_id
                """,
                (INTERNAL_TEST_PHONE_HASH,),
            ).fetchall()
        }
        clean_export_counts = {
            (
                row["participant_type"],
                row["condition"],
                row["subcondition"],
                row["error_type_id"],
            ): int(row["participant_count"])
            for row in self._conn.execute(
                """
                SELECT
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.error_type_id,
                    COUNT(DISTINCT pa.id) AS participant_count
                FROM clean_data_audits cda
                JOIN participant_attempts pa ON pa.id = cda.attempt_id
                JOIN participants p ON p.id = cda.participant_id
                WHERE
                    cda.status = 'eligible'
                    AND p.phone_hash != ?
                GROUP BY
                    pa.participant_type,
                    pa.condition,
                    pa.subcondition,
                    pa.error_type_id
                """,
                (INTERNAL_TEST_PHONE_HASH,),
            ).fetchall()
        }
        configured_cells = {
            (
                row["participant_type"],
                row["condition"],
                row["subcondition"],
                row["error_type_id"],
            ): row
            for row in self._conn.execute(
                """
                SELECT
                    participant_type,
                    condition,
                    subcondition,
                    error_type_id,
                    cap,
                    enabled,
                    updated_at
                FROM admin_assignment_units
                """
            ).fetchall()
        }
        participant_types: dict[str, dict[str, Any]] = {}
        next_assignment_preview: dict[str, Any] = {}
        for participant_type in PARTICIPANT_TYPES:
            cells: list[dict[str, Any]] = []
            for condition in CONDITIONS:
                for subcondition in SUBCONDITIONS:
                    for error_type_id in ERROR_TYPE_IDS:
                        configured = configured_cells.get(
                            (participant_type, condition, subcondition, error_type_id)
                        )
                        cap = (
                            int(configured["cap"])
                            if configured is not None and configured["cap"] is not None
                            else None
                        )
                        enabled = (
                            bool(int(configured["enabled"]))
                            if configured is not None
                            else True
                        )
                        count = counts.get(
                            (participant_type, condition, subcondition, error_type_id),
                            0,
                        )
                        active_assignment_count = active_assignment_counts.get(
                            (participant_type, condition, subcondition, error_type_id),
                            0,
                        )
                        complete_no_external_error_count = clean_export_counts.get(
                            (participant_type, condition, subcondition, error_type_id),
                            0,
                        )
                        cells.append(
                            {
                                "participant_type": participant_type,
                                "condition": condition,
                                "subcondition": subcondition,
                                "error_type_id": error_type_id,
                                "count": count,
                                "active_assignment_count": active_assignment_count,
                                "complete_no_external_error_count": complete_no_external_error_count,
                                "cap": cap,
                                "enabled": enabled,
                                "remaining": None if cap is None else max(cap - count, 0),
                                "updated_at": (
                                    configured["updated_at"] if configured is not None else None
                                ),
                            }
                        )
            participant_types[participant_type] = {
                "participant_type": participant_type,
                "cells": cells,
            }
            next_assignment_preview[participant_type] = self._assignment_preview(
                participant_type=participant_type
            )

        return {
            "participant_types": participant_types,
            "current_flags": {
                "test_channel_enabled": self._settings.test_channel_enabled,
            },
            "next_assignment_preview": next_assignment_preview,
            "notes": [
                "Assignment controls are persisted in SQLite and affect new formal participant assignment.",
            ],
        }

    def _assignment_control_cell_states(self) -> list[dict[str, Any]]:
        configured_cells = {
            (
                row["participant_type"],
                row["condition"],
                row["subcondition"],
                row["error_type_id"],
            ): row
            for row in self._conn.execute(
                """
                SELECT
                    participant_type,
                    condition,
                    subcondition,
                    error_type_id,
                    cap,
                    enabled
                FROM admin_assignment_units
                """
            ).fetchall()
        }
        states: list[dict[str, Any]] = []
        for participant_type in PARTICIPANT_TYPES:
            for condition in CONDITIONS:
                for subcondition in SUBCONDITIONS:
                    for error_type_id in ERROR_TYPE_IDS:
                        cell_id = (
                            participant_type,
                            condition,
                            subcondition,
                            error_type_id,
                        )
                        configured = configured_cells.get(cell_id)
                        states.append(
                            {
                                "participant_type": participant_type,
                                "condition": condition,
                                "subcondition": subcondition,
                                "error_type_id": error_type_id,
                                "cap": (
                                    int(configured["cap"])
                                    if configured is not None
                                    and configured["cap"] is not None
                                    else None
                                ),
                                "enabled": (
                                    bool(int(configured["enabled"]))
                                    if configured is not None
                                    else True
                                ),
                            }
                        )
        return states

    @staticmethod
    def _validate_assignment_cell_identifier(cell: Any) -> dict[str, str]:
        if not isinstance(cell, dict):
            raise ValueError("Assignment batch cells must be complete identifiers.")
        required_fields = {
            "participant_type",
            "condition",
            "subcondition",
            "error_type_id",
        }
        if set(cell) != required_fields:
            raise ValueError("Assignment batch cells must be complete identifiers.")
        if cell["participant_type"] not in PARTICIPANT_TYPES:
            raise ValueError("Unsupported participant_type.")
        if cell["condition"] not in CONDITIONS:
            raise ValueError("Invalid assignment cell condition.")
        if cell["subcondition"] not in SUBCONDITIONS:
            raise ValueError("Invalid assignment cell subcondition.")
        if cell["error_type_id"] not in ERROR_TYPE_IDS:
            raise ValueError("Invalid assignment unit error_type_id.")
        return {
            field: str(cell[field])
            for field in (
                "participant_type",
                "condition",
                "subcondition",
                "error_type_id",
            )
        }

    def _resolve_assignment_batch_scope(
        self,
        *,
        scope: Any,
        allow_empty_filter: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
        if not isinstance(scope, dict) or set(scope) not in ({"cells"}, {"filter"}):
            raise ValueError(
                "Assignment batch scope must provide cells or a bounded filter."
            )
        all_states = self._assignment_control_cell_states()
        states_by_id = {
            (
                state["participant_type"],
                state["condition"],
                state["subcondition"],
                state["error_type_id"],
            ): state
            for state in all_states
        }

        if "cells" in scope:
            requested_cells = scope["cells"]
            if not isinstance(requested_cells, list) or not requested_cells:
                raise ValueError("Assignment batch cell scope cannot be empty.")
            if len(requested_cells) > MAX_ASSIGNMENT_BATCH_CELLS:
                raise ValueError("Assignment batch cell scope exceeds the bounded limit.")
            identifiers = [
                self._validate_assignment_cell_identifier(cell)
                for cell in requested_cells
            ]
            identifier_tuples = [
                (
                    cell["participant_type"],
                    cell["condition"],
                    cell["subcondition"],
                    cell["error_type_id"],
                )
                for cell in identifiers
            ]
            if len(set(identifier_tuples)) != len(identifier_tuples):
                raise ValueError("Assignment batch cell scope contains duplicates.")
            selected_states = [states_by_id[cell_id] for cell_id in identifier_tuples]
            selected_states.sort(
                key=lambda cell: (
                    cell["participant_type"],
                    cell["condition"],
                    cell["subcondition"],
                    cell["error_type_id"],
                )
            )
            selected_cells = [
                {
                    key: cell[key]
                    for key in (
                        "participant_type",
                        "condition",
                        "subcondition",
                        "error_type_id",
                    )
                }
                for cell in selected_states
            ]
            return (
                selected_states,
                {
                    "kind": "explicit_cells",
                    "description": f"{len(selected_states)} explicit assignment cells",
                    "selected_cells": selected_cells,
                },
                {"cells": selected_cells},
            )

        filters = scope["filter"]
        if not isinstance(filters, dict) or not filters:
            raise ValueError("Assignment batch filter must be bounded and non-empty.")
        unknown_fields = set(filters) - set(ASSIGNMENT_FILTER_FIELDS)
        if unknown_fields:
            raise ValueError("Assignment batch filter contains unsupported fields.")
        if "participant_type" in filters and filters["participant_type"] not in PARTICIPANT_TYPES:
            raise ValueError("Unsupported participant_type.")
        if "condition" in filters and filters["condition"] not in CONDITIONS:
            raise ValueError("Invalid assignment cell condition.")
        if "subcondition" in filters and filters["subcondition"] not in SUBCONDITIONS:
            raise ValueError("Invalid assignment cell subcondition.")
        if "error_type_id" in filters and filters["error_type_id"] not in ERROR_TYPE_IDS:
            raise ValueError("Invalid assignment unit error_type_id.")
        if "enabled" in filters and not isinstance(filters["enabled"], bool):
            raise ValueError("Assignment batch enabled filter must be boolean.")
        if "cap_status" in filters and filters["cap_status"] not in {
            "capped",
            "uncapped",
            "reached",
        }:
            raise ValueError("Unsupported assignment cap status filter.")

        normalized_filters = {
            field: filters[field]
            for field in ASSIGNMENT_FILTER_FIELDS
            if field in filters
        }
        counted_assignments: dict[tuple[str, str, str, str], int] = {}
        if normalized_filters.get("cap_status") == "reached":
            counted_assignments = {
                (
                    row["participant_type"],
                    row["condition"],
                    row["subcondition"],
                    row["error_type_id"],
                ): int(row["participant_count"])
                for row in self._conn.execute(
                    f"""
                    SELECT
                        pa.participant_type,
                        pa.condition,
                        pa.subcondition,
                        pa.error_type_id,
                        COUNT(*) AS participant_count
                    FROM participants p
                    JOIN participant_attempts pa ON pa.id = p.current_attempt_id
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

        def matches_filter(cell: dict[str, Any]) -> bool:
            for field in (
                "participant_type",
                "condition",
                "subcondition",
                "error_type_id",
                "enabled",
            ):
                if field in normalized_filters and cell[field] != normalized_filters[field]:
                    return False
            cap_status = normalized_filters.get("cap_status")
            if cap_status == "capped" and cell["cap"] is None:
                return False
            if cap_status == "uncapped" and cell["cap"] is not None:
                return False
            if cap_status == "reached":
                cell_id = (
                    cell["participant_type"],
                    cell["condition"],
                    cell["subcondition"],
                    cell["error_type_id"],
                )
                return (
                    cell["cap"] is not None
                    and counted_assignments.get(cell_id, 0) >= cell["cap"]
                )
            return True

        selected_states = [cell for cell in all_states if matches_filter(cell)]
        selected_states.sort(
            key=lambda cell: (
                cell["participant_type"],
                cell["condition"],
                cell["subcondition"],
                cell["error_type_id"],
            )
        )
        if not selected_states and not allow_empty_filter:
            raise ValueError("Assignment batch filter matches no cells.")
        description = ", ".join(
            f"{field}={str(normalized_filters[field]).lower() if isinstance(normalized_filters[field], bool) else normalized_filters[field]}"
            for field in ASSIGNMENT_FILTER_FIELDS
            if field in normalized_filters
        )
        return (
            selected_states,
            {
                "kind": "filter_snapshot",
                "description": description,
                "selected_cells": [
                    {
                        key: cell[key]
                        for key in (
                            "participant_type",
                            "condition",
                            "subcondition",
                            "error_type_id",
                        )
                    }
                    for cell in selected_states
                ],
            },
            {"filter": normalized_filters},
        )

    def _prepare_assignment_control_batch(
        self,
        *,
        scope: Any,
        changes: Any,
        cap_is_set: bool,
        cell_updates: Any = None,
        allow_empty_filter: bool = False,
        classify_filter_membership_drift: bool = False,
    ) -> tuple[
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, Any],
        list[dict[str, Any]] | None,
    ]:
        if not isinstance(changes, dict):
            raise ValueError("Assignment batch changes must be an object.")
        if set(changes) - {"cap", "enabled"}:
            raise ValueError("Assignment batch changes contain unsupported fields.")
        if "enabled" in changes and not isinstance(changes["enabled"], bool):
            raise ValueError("Assignment batch enabled change must be boolean.")
        if cap_is_set:
            cap = changes.get("cap")
            if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
                raise ValueError("Assignment cap must be null or >= 0.")
        has_common_changes = cap_is_set or "enabled" in changes
        normalized_changes: dict[str, Any] = {}
        if cap_is_set:
            normalized_changes["cap"] = changes.get("cap")
        if "enabled" in changes:
            normalized_changes["enabled"] = changes["enabled"]

        normalized_cell_updates: list[dict[str, Any]] | None = None
        if cell_updates is not None:
            if has_common_changes:
                raise ValueError(
                    "Assignment batch cannot mix common changes and cell updates."
                )
            if not isinstance(cell_updates, list) or not cell_updates:
                raise ValueError("Assignment batch cell updates cannot be empty.")
            normalized_cell_updates = []
            required_fields = {
                "participant_type",
                "condition",
                "subcondition",
                "error_type_id",
            }
            for update in cell_updates:
                if not isinstance(update, dict):
                    raise ValueError("Assignment batch cell updates must be objects.")
                update_fields = set(update)
                if (
                    not required_fields.issubset(update_fields)
                    or update_fields - required_fields - {"cap", "enabled"}
                    or not update_fields.intersection({"cap", "enabled"})
                ):
                    raise ValueError(
                        "Assignment batch cell updates require a complete identifier and changes."
                    )
                identifier = self._validate_assignment_cell_identifier(
                    {field: update[field] for field in required_fields}
                )
                normalized_update: dict[str, Any] = dict(identifier)
                if "cap" in update:
                    update_cap = update["cap"]
                    if update_cap is not None and (
                        isinstance(update_cap, bool)
                        or not isinstance(update_cap, int)
                        or update_cap < 0
                    ):
                        raise ValueError("Assignment cap must be null or >= 0.")
                    normalized_update["cap"] = update_cap
                if "enabled" in update:
                    if not isinstance(update["enabled"], bool):
                        raise ValueError(
                            "Assignment batch enabled change must be boolean."
                        )
                    normalized_update["enabled"] = update["enabled"]
                normalized_cell_updates.append(normalized_update)
        elif not has_common_changes:
            raise ValueError("Assignment batch must include cap or enabled changes.")

        selected_states, scope_summary, normalized_scope = (
            self._resolve_assignment_batch_scope(
                scope=scope,
                allow_empty_filter=allow_empty_filter,
            )
        )
        if normalized_cell_updates is not None and selected_states:
            selected_ids = {
                (
                    cell["participant_type"],
                    cell["condition"],
                    cell["subcondition"],
                    cell["error_type_id"],
                )
                for cell in selected_states
            }
            update_ids = [
                (
                    update["participant_type"],
                    update["condition"],
                    update["subcondition"],
                    update["error_type_id"],
                )
                for update in normalized_cell_updates
            ]
            if len(set(update_ids)) != len(update_ids) or set(update_ids) != selected_ids:
                if (
                    classify_filter_membership_drift
                    and scope_summary["kind"] == "filter_snapshot"
                ):
                    raise AssignmentBatchConflictError(
                        "Assignment batch preview is stale; refresh and confirm the scope again."
                    )
                raise ValueError(
                    "Assignment batch cell updates must match the complete selected scope."
                )
            normalized_cell_updates.sort(
                key=lambda update: (
                    update["participant_type"],
                    update["condition"],
                    update["subcondition"],
                    update["error_type_id"],
                )
            )
        version_payload = {
            "scope": normalized_scope,
            "cells": selected_states,
            "changes": normalized_changes,
            "cell_updates": normalized_cell_updates,
        }
        scope_version = "sha256:" + hashlib.sha256(
            json.dumps(
                version_payload,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        preview = {
            "scope_version": scope_version,
            "affected_count": len(selected_states),
            "scope": scope_summary,
            "changes": normalized_changes,
        }
        if normalized_cell_updates is not None:
            preview["cell_updates"] = normalized_cell_updates
        return (
            preview,
            selected_states,
            normalized_changes,
            normalized_cell_updates,
        )

    def preview_assignment_control_batch(
        self,
        *,
        scope: Any,
        changes: Any,
        cap_is_set: bool,
        cell_updates: Any = None,
    ) -> dict[str, Any]:
        preview, _, _, _ = self._prepare_assignment_control_batch(
            scope=scope,
            changes=changes,
            cap_is_set=cap_is_set,
            cell_updates=cell_updates,
        )
        return preview

    def apply_assignment_control_batch(
        self,
        *,
        admin_user: str,
        scope: Any,
        changes: Any,
        cap_is_set: bool,
        scope_version: str,
        cell_updates: Any = None,
    ) -> dict[str, Any]:
        (
            preview,
            selected_states,
            normalized_changes,
            normalized_cell_updates,
        ) = self._prepare_assignment_control_batch(
            scope=scope,
            changes=changes,
            cap_is_set=cap_is_set,
            cell_updates=cell_updates,
            allow_empty_filter=True,
            classify_filter_membership_drift=True,
        )
        if not selected_states:
            raise AssignmentBatchConflictError(
                "Assignment batch preview is stale; refresh and confirm the scope again."
            )
        if scope_version != preview["scope_version"]:
            raise AssignmentBatchConflictError(
                "Assignment batch preview is stale; refresh and confirm the scope again."
            )

        updates_by_id = {
            (
                update["participant_type"],
                update["condition"],
                update["subcondition"],
                update["error_type_id"],
            ): update
            for update in normalized_cell_updates or []
        }
        for cell in selected_states:
            cell_id = (
                cell["participant_type"],
                cell["condition"],
                cell["subcondition"],
                cell["error_type_id"],
            )
            cell_changes = updates_by_id.get(cell_id, normalized_changes)
            next_cap = cell_changes.get("cap", cell["cap"])
            next_enabled = cell_changes.get("enabled", cell["enabled"])
            self._conn.execute(
                """
                INSERT INTO admin_assignment_units (
                    participant_type,
                    condition,
                    subcondition,
                    error_type_id,
                    cap,
                    enabled,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(participant_type, condition, subcondition, error_type_id)
                DO UPDATE SET
                    cap = excluded.cap,
                    enabled = excluded.enabled,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    cell["participant_type"],
                    cell["condition"],
                    cell["subcondition"],
                    cell["error_type_id"],
                    next_cap,
                    int(next_enabled),
                ),
            )

        result = {"updated_cells": len(selected_states)}
        self.record_event(
            admin_user=admin_user,
            action="update_assignment_cap",
            target_type="assignment_batch",
            target_id=scope_version,
            payload={
                "operation": "batch",
                "scope_version": scope_version,
                "affected_count": len(selected_states),
                "scope": preview["scope"],
                "changes": (
                    normalized_changes
                    if normalized_cell_updates is None
                    else {"cell_updates": normalized_cell_updates}
                ),
                "result": result,
            },
        )
        return {
            **preview,
            "result": result,
            "assignment_control": self.get_assignment_control_summary(),
        }

    def update_assignment_controls(
        self,
        *,
        admin_user: str,
        operation: str | None = None,
        participant_type: str | None = None,
        condition: str | None = None,
        subcondition: str | None = None,
        error_type_id: str | None = None,
        cap: int | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        has_any_cell_field = any(
            value is not None
            for value in (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled,
            )
        )
        if operation not in {None, "cell"}:
            raise ValueError("Unsupported assignment control operation.")
        if not has_any_cell_field:
            raise ValueError("Assignment control update requires cell changes.")
        if participant_type not in PARTICIPANT_TYPES:
            raise ValueError("Unsupported participant_type.")
        if condition not in CONDITIONS or subcondition not in SUBCONDITIONS:
            raise ValueError("Invalid assignment cell.")
        if error_type_id not in ERROR_TYPE_IDS:
            raise ValueError("Invalid assignment unit error_type_id.")
        if enabled is None:
            raise ValueError("Assignment cell updates must include enabled.")
        if cap is not None and cap < 0:
            raise ValueError("Assignment cap must be null or >= 0.")

        self._conn.execute(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(participant_type, condition, subcondition, error_type_id)
            DO UPDATE SET
                cap = excluded.cap,
                enabled = excluded.enabled,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                int(enabled),
            ),
        )
        self.record_event(
            admin_user=admin_user,
            action="update_assignment_cap",
            target_type="assignment_unit",
            target_id=(
                f"{participant_type}:{condition}:{subcondition}:{error_type_id}"
            ),
            payload={
                "operation": "cell",
                "participant_type": participant_type,
                "condition": condition,
                "subcondition": subcondition,
                "error_type_id": error_type_id,
                "cap": cap,
                "enabled": enabled,
            },
        )
        return self.get_assignment_control_summary()

    def get_api_health_summary(self) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT route, provider, model, status, latency_ms, error_code, error_message_summary, cooldown_applied, created_at
            FROM api_call_logs
            ORDER BY created_at DESC
            LIMIT 500
            """
        ).fetchall()
        grouped: dict[tuple[str, str, str | None], list[sqlite3.Row]] = defaultdict(list)
        failure_reasons = Counter()
        for row in rows:
            grouped[(row["route"], row["provider"], row["model"])].append(row)
            if row["status"] != "success":
                failure_reasons[
                    (
                        row["route"],
                        row["status"],
                        normalize_provider_error_code(
                            status=row["status"],
                            error_code=row["error_code"],
                        )
                        or "unknown",
                    )
                ] += 1
        route_summaries: list[dict[str, Any]] = []
        for (route, provider, model), group_rows in sorted(
            grouped.items(),
            key=lambda item: _nullable_tuple_sort_key(item[0]),
        ):
            total = len(group_rows)
            successes = sum(1 for row in group_rows if row["status"] == "success")
            latency_values = [
                int(row["latency_ms"])
                for row in group_rows
                if row["latency_ms"] is not None
            ]
            route_summaries.append(
                {
                    "route": route,
                    "provider": provider,
                    "model": model,
                    "total": total,
                    "successes": successes,
                    "success_rate": _percent(successes, total),
                    "avg_latency_ms": round(sum(latency_values) / len(latency_values), 1)
                    if latency_values
                    else None,
                    "p95_latency_ms": _p95(latency_values),
                    "cooldown_applied_count": sum(
                        1 for row in group_rows if int(row["cooldown_applied"]) == 1
                    ),
                }
            )
        cooldown_rows = self._conn.execute(
            """
            SELECT route, provider, model, cooldown_until
            FROM provider_cooldowns
            ORDER BY route, provider, model
            """
        ).fetchall()
        manual_test_rows = self._conn.execute(
            """
            SELECT payload_json, created_at
            FROM admin_events
            WHERE action = 'test_agent'
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
        return {
            "routes": route_summaries,
            "cooldowns": [
                {
                    "route": row["route"],
                    "provider": row["provider"],
                    "model": row["model"],
                    "cooldown_until": row["cooldown_until"],
                }
                for row in cooldown_rows
            ],
            "failure_reasons": [
                {
                    "route": route,
                    "status": status,
                    "error_code": error_code,
                    "count": count,
                }
                for (route, status, error_code), count in failure_reasons.most_common(20)
            ],
            "evaluator_success_rate": self._route_success_summary(route="evaluator"),
            "asr_success_rate": self._route_success_summary(route="asr"),
            "manual_test_runs": [
                self._sanitize_admin_test_event(
                    payload=_json_loads(row["payload_json"]),
                    created_at=row["created_at"],
                )
                for row in manual_test_rows
            ],
            "notes": [
                "Manual provider tests target the configured DeepSeek route only.",
            ],
        }

    def get_provider_model_usage(self) -> dict[str, Any]:
        rows = self._conn.execute(
            """
            SELECT
                route,
                provider,
                model,
                status,
                http_status,
                error_code,
                error_message_summary,
                latency_ms,
                cooldown_applied,
                created_at
            FROM api_call_logs
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
        cutoff_row = self._conn.execute(
            "SELECT datetime('now', '-24 hours') AS cutoff"
        ).fetchone()
        cutoff = str(cutoff_row["cutoff"])
        return {
            "generated_at": _sqlite_now(),
            "deepseek_configuration": {
                "status": (
                    "configured"
                    if self._settings.deepseek_api_key
                    and self._settings.deepseek_api_key.strip()
                    else "not_configured"
                ),
                "provider": "deepseek",
                "model": self._settings.deepseek_model,
                "base_url": self._settings.deepseek_base_url,
                "timeout_seconds": self._settings.deepseek_timeout_seconds,
            },
            "windows": [
                self._provider_model_usage_window(
                    rows,
                    window="all_time",
                    label="全部累计",
                    since=None,
                ),
                self._provider_model_usage_window(
                    [
                        row
                        for row in rows
                        if str(row["created_at"]) >= cutoff
                    ],
                    window="last_24h",
                    label="最近 24 小时",
                    since=cutoff,
                ),
            ],
            "notes": [
                "Success means api_call_logs.status == 'success'. Other statuses are counted as failures.",
                "Last 24 hours is computed on the server from the current SQLite timestamp.",
            ],
        }

    def _route_success_summary(self, *, route: str) -> dict[str, Any]:
        row = self._conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS successes
            FROM api_call_logs
            WHERE route = ?
            """,
            (route,),
        ).fetchone()
        total = int(row["total"] or 0)
        successes = int(row["successes"] or 0)
        return {
            "route": route,
            "total": total,
            "successes": successes,
            "success_rate": _percent(successes, total),
        }

    def test_deepseek(self, *, admin_user: str) -> dict[str, object]:
        health_service = ApiHealthService(self._conn, is_test=True)
        result = ProviderTestService(
            settings=self._settings,
            health_service=health_service,
        ).test_deepseek(request_id=f"admin-deepseek-test-{uuid4().hex}")
        payload = asdict(result)
        with transaction(self._conn):
            health_service.flush()
            self.record_event(
                admin_user=admin_user,
                action="test_agent",
                target_type="provider",
                target_id="deepseek",
                payload=payload,
            )
        return payload

    def export_sanitized_data(
        self,
        *,
        admin_user: str,
        include_test: bool = False,
    ) -> dict[str, Any]:
        export_dir = self._settings.data_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        export_path = export_dir / f"full_export_{timestamp}.zip"
        export_result = create_v2_export(
            self._conn,
            self._settings,
            export_path,
            include_test=include_test,
        )
        with transaction(self._conn):
            self.record_event(
                admin_user=admin_user,
                action="export_data",
                target_type="file",
                target_id=export_path.name,
                payload={
                    "export_type": "experiment_data",
                    "path": str(export_path.relative_to(self._settings.data_dir)),
                    "include_test": include_test,
                    "row_counts": export_result.row_counts,
                },
            )
        return {
            "path": str(export_path),
            "relative_path": str(export_path.relative_to(self._settings.data_dir)),
            "include_test": include_test,
            "generated_at": export_result.generated_at,
            "row_counts": export_result.row_counts,
            "notes": [
                "Structured participant identifiers are pseudonymized; validated raw audio remains controlled sensitive research data. Secret values, environment files, and raw log dumps are excluded.",
            ],
        }

    def get_system_logs_summary(self) -> dict[str, Any]:
        api_rows = self._conn.execute(
            """
            SELECT route, status, COUNT(*) AS count
            FROM api_call_logs
            GROUP BY route, status
            ORDER BY route, status
            """
        ).fetchall()
        backend_rows = self._conn.execute(
            """
            SELECT pa.status AS current_status, COUNT(*) AS count
            FROM participants p
            JOIN participant_attempts pa ON pa.id = p.current_attempt_id
            WHERE p.phone_hash != ?
            GROUP BY pa.status
            ORDER BY pa.status
            """,
            (INTERNAL_TEST_PHONE_HASH,),
        ).fetchall()
        asr_rows = self._conn.execute(
            """
            SELECT asr_status, COUNT(*) AS count
            FROM conversation_turns
            GROUP BY asr_status
            ORDER BY asr_status
            """
        ).fetchall()
        db_path = Path(self._settings.database_url.replace("sqlite:///", "", 1))
        db_size = db_path.stat().st_size if db_path.exists() else 0
        audio_dir = self._settings.data_dir / "audio"
        exports_dir = self._settings.data_dir / "exports"
        audio_files, audio_size = _count_matching_files(audio_dir)
        disk_usage = shutil.disk_usage(self._settings.data_dir)
        exports_dir.mkdir(parents=True, exist_ok=True)
        package_path = (
            exports_dir
            / f"sanitized-system-summary-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
        )
        package_payload = {
            "backend_status_counts": {row["current_status"]: int(row["count"]) for row in backend_rows},
            "api_log_counts": [
                {"route": row["route"], "status": row["status"], "count": int(row["count"])}
                for row in api_rows
            ],
            "asr_status_counts": {row["asr_status"]: int(row["count"]) for row in asr_rows},
            "database_size_bytes": db_size,
            "disk_usage": {
                "total_bytes": disk_usage.total,
                "used_bytes": disk_usage.used,
                "free_bytes": disk_usage.free,
            },
            "audio_directory": {
                "path": str(audio_dir.relative_to(self._settings.data_dir))
                if audio_dir.exists()
                else "audio",
                "files": audio_files,
                "size_bytes": audio_size,
            },
        }
        package_path.write_text(
            json.dumps(package_payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return {
            "backend_status_counts": package_payload["backend_status_counts"],
            "api_log_counts": package_payload["api_log_counts"],
            "asr_status_counts": package_payload["asr_status_counts"],
            "database_size_bytes": db_size,
            "disk_usage": package_payload["disk_usage"],
            "audio_directory": package_payload["audio_directory"],
            "exports_directory": str(exports_dir.relative_to(self._settings.data_dir)),
            "sanitized_package_path": str(package_path),
            "notes": [
                "Summary package is sanitized and excludes raw phone values, secret settings, and audio blobs.",
            ],
        }

    def _assignment_preview(self, *, participant_type: str) -> dict[str, Any]:
        try:
            preview = preview_assignment_for_participant_type(
                self._conn,
                participant_type=participant_type,
            )
        except ValueError as exc:
            return {"available": False, "reason": str(exc)}
        return {
            "available": True,
            "participant_type": preview.participant_type,
            "condition": preview.condition,
            "subcondition": preview.subcondition,
            "topic_key": preview.topic_key,
            "error_type_id": preview.error_type_id,
            "target_days": preview.target_days,
        }

    def _sanitize_admin_test_event(
        self,
        *,
        payload: Any,
        created_at: str,
    ) -> dict[str, Any]:
        source = payload if isinstance(payload, dict) else {}
        sanitized = {
            "status": source.get("status"),
            "provider": source.get("provider"),
            "model": source.get("model"),
            "latency_ms": source.get("latency_ms"),
            "error_code": normalize_provider_error_code(
                status=source.get("status"),
                error_code=source.get("error_code"),
            ),
        }
        sanitized["created_at"] = created_at
        return sanitized

    def _safe_provider_error_summary(self, row: sqlite3.Row) -> str | None:
        status = str(row["status"])
        if status == "success":
            return None
        parts = [status]
        if row["http_status"] is not None:
            parts.append(str(row["http_status"]))
        error_code = normalize_provider_error_code(
            status=status,
            error_code=row["error_code"],
        )
        if error_code:
            parts.append(error_code)
        return ":".join(parts)

    def _monitor_session_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "participant_id": int(row["participant_id"]),
            "attempt_id": int(row["attempt_id"]) if row["attempt_id"] is not None else None,
            "name": row["name"],
            "masked_phone": mask_phone(row["phone"]),
            "phone_hash": row["phone_hash"],
            "participant_type": row["participant_type"],
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "topic_key": row["topic_key"],
            "error_type_id": row["error_type_id"],
            "attempt_status": row["attempt_status"],
            "session_id": row["session_uuid"],
            "day_index": row["day_index"],
            "session_status": row["session_status"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "updated_at": row["updated_at"],
        }

    def _provider_model_usage_window(
        self,
        rows: list[sqlite3.Row],
        *,
        window: str,
        label: str,
        since: str | None,
    ) -> dict[str, Any]:
        provider_groups: dict[tuple[str, str | None], list[sqlite3.Row]] = defaultdict(list)
        route_groups: dict[tuple[str, str, str | None], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            provider_groups[(row["provider"], row["model"])].append(row)
            route_groups[(row["route"], row["provider"], row["model"])].append(row)

        total_calls = len(rows)
        total_successes = sum(1 for row in rows if row["status"] == "success")
        return {
            "window": window,
            "label": label,
            "since": since,
            "total_calls": total_calls,
            "total_successes": total_successes,
            "total_failures": total_calls - total_successes,
            "provider_model_rows": [
                self._usage_group_row(provider=provider, model=model, rows=group_rows)
                for (provider, model), group_rows in sorted(
                    provider_groups.items(),
                    key=lambda item: _nullable_tuple_sort_key(item[0]),
                )
            ],
            "route_rows": [
                {
                    "route": route,
                    **self._usage_group_row(
                        provider=provider,
                        model=model,
                        rows=group_rows,
                    ),
                }
                for (route, provider, model), group_rows in sorted(
                    route_groups.items(),
                    key=lambda item: _nullable_tuple_sort_key(item[0]),
                )
            ],
        }

    def _usage_group_row(
        self,
        *,
        provider: str,
        model: str | None,
        rows: list[sqlite3.Row],
    ) -> dict[str, Any]:
        calls = len(rows)
        successes = sum(1 for row in rows if row["status"] == "success")
        latencies = [
            int(row["latency_ms"])
            for row in rows
            if row["latency_ms"] is not None
        ]
        failure_rows = [
            row
            for row in rows
            if row["status"] != "success"
        ]
        last_failure = failure_rows[0] if failure_rows else None
        return {
            "provider": provider,
            "model": model,
            "calls": calls,
            "successes": successes,
            "failures": calls - successes,
            "timeout_count": sum(1 for row in rows if row["status"] == "timeout"),
            "success_rate": _percent(successes, calls),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1)
            if latencies
            else None,
            "p95_latency_ms": _p95(latencies),
            "cooldown_applied_count": sum(
                1
                for row in rows
                if int(row["cooldown_applied"]) == 1
            ),
            "last_called_at": rows[0]["created_at"] if rows else None,
            "last_failure_summary": self._safe_provider_error_summary(last_failure)
            if last_failure is not None
            else None,
            "last_failure_code": normalize_provider_error_code(
                status=last_failure["status"],
                error_code=last_failure["error_code"],
            )
            if last_failure is not None
            else None,
        }

    def _clean_data_audit_row(self, row: sqlite3.Row) -> dict[str, Any]:
        reasons = _json_loads(row["reasons_json"])
        return {
            "participant_id": int(row["participant_id"]),
            "attempt_id": int(row["attempt_id"]) if row["attempt_id"] is not None else None,
            "name": row["name"],
            "phone_hash": row["phone_hash"],
            "participant_type": row["participant_type"],
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "topic_key": row["topic_key"],
            "error_type_id": row["error_type_id"],
            "status": row["status"],
            "reasons": [
                str(reason)
                for reason in reasons
            ]
            if isinstance(reasons, list)
            else [],
            "reviewer_note": row["reviewer_note"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "computed_at": row["computed_at"],
        }
