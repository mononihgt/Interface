from __future__ import annotations

from datetime import datetime
import sqlite3
from typing import Sequence


class HealthRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def ping(self) -> bool:
        return self._conn.execute("SELECT 1").fetchone()[0] == 1

    def insert_api_call_log(
        self,
        *,
        request_id: str,
        session_id: int | None,
        turn_index: int | None,
        is_test: bool | None,
        route: str,
        provider: str,
        model: str | None,
        status: str,
        http_status: int | None,
        error_code: str | None,
        error_message_summary: str | None,
        latency_ms: int | None,
        cooldown_applied: bool,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO api_call_logs (
                request_id,
                session_id,
                turn_index,
                is_test,
                route,
                provider,
                model,
                status,
                http_status,
                error_code,
                error_message_summary,
                latency_ms,
                cooldown_applied
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                request_id,
                session_id,
                turn_index,
                int(is_test) if is_test is not None else None,
                route,
                provider,
                model,
                status,
                http_status,
                error_code,
                error_message_summary,
                latency_ms,
                int(cooldown_applied),
            ),
        )
        return int(cursor.lastrowid)

    def insert_session_risk_flag(
        self,
        *,
        session_id: int,
        flag: str,
        detail_json: str | None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO session_risk_flags (
                session_id,
                flag,
                detail_json
            ) VALUES (?, ?, ?)
            """,
            (session_id, flag, detail_json),
        )
        return int(cursor.lastrowid)

    def has_failed_attempt_for_sessions(self, *, session_ids: Sequence[int]) -> bool:
        if not session_ids:
            return False
        placeholders = ", ".join("?" for _ in session_ids)
        row = self._conn.execute(
            f"""
            SELECT 1
            FROM api_call_logs
            WHERE session_id IN ({placeholders})
              AND route IN ('chat', 'evaluator', 'asr')
              AND status != 'success'
            LIMIT 1
            """,
            tuple(session_ids),
        ).fetchone()
        return row is not None

    def get_provider_cooldown_until(
        self,
        *,
        route: str,
        provider: str,
        model: str,
    ) -> datetime | None:
        row = self._conn.execute(
            """
            SELECT cooldown_until
            FROM provider_cooldowns
            WHERE route = ? AND provider = ? AND model = ?
            """,
            (route, provider, model),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(str(row["cooldown_until"]))

    def upsert_provider_cooldown(
        self,
        *,
        route: str,
        provider: str,
        model: str,
        cooldown_until: datetime,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO provider_cooldowns (
                route,
                provider,
                model,
                cooldown_until
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(route, provider, model)
            DO UPDATE SET
                cooldown_until = excluded.cooldown_until,
                updated_at = CURRENT_TIMESTAMP
            """,
            (route, provider, model, cooldown_until.isoformat()),
        )

    def delete_provider_cooldown(
        self,
        *,
        route: str,
        provider: str,
        model: str,
    ) -> None:
        self._conn.execute(
            """
            DELETE FROM provider_cooldowns
            WHERE route = ? AND provider = ? AND model = ?
            """,
            (route, provider, model),
        )
