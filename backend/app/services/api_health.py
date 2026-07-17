from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Callable

from backend.app.repositories.health import HealthRepository
from backend.app.services.records import to_json


@dataclass(frozen=True)
class LoggedProviderAttempt:
    route: str
    provider: str
    model: str | None
    status: str
    http_status: int | None = None
    error_code: str | None = None
    error_message_summary: str | None = None
    latency_ms: int | None = None
    cooldown_applied: bool = False


@dataclass(frozen=True)
class PendingCooldown:
    route: str
    provider: str
    model: str
    cooldown_until: datetime


class ApiHealthService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now_fn: Callable[[], datetime] | None = None,
        session_id: int | None = None,
        turn_index: int | None = None,
        is_test: bool | None = None,
    ) -> None:
        self._repository = HealthRepository(conn)
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._session_id = session_id
        self._turn_index = turn_index
        self._is_test = is_test
        self._pending_attempts: list[tuple[str, LoggedProviderAttempt]] = []
        self._pending_cooldowns: dict[tuple[str, str, str], PendingCooldown] = {}

    def is_on_cooldown(
        self,
        *,
        route: str,
        provider: str,
        model: str,
        at: datetime | None = None,
    ) -> bool:
        current_time = at or self._now_fn()
        pending_key = (route, provider, model)
        pending = self._pending_cooldowns.get(pending_key)
        if pending is not None:
            if current_time >= pending.cooldown_until:
                self._pending_cooldowns.pop(pending_key, None)
                return False
            return True
        until = self._repository.get_provider_cooldown_until(
            route=route,
            provider=provider,
            model=model,
        )
        if until is None:
            return False
        if current_time >= until:
            self._repository.delete_provider_cooldown(
                route=route,
                provider=provider,
                model=model,
            )
            return False
        return True

    def apply_cooldown(
        self,
        *,
        route: str,
        provider: str,
        model: str,
        seconds: int,
    ) -> None:
        cooldown_until = self._now_fn() + timedelta(seconds=seconds)
        self._pending_cooldowns[(route, provider, model)] = PendingCooldown(
            route=route,
            provider=provider,
            model=model,
            cooldown_until=cooldown_until,
        )

    def log_attempt(self, *, request_id: str, attempt: LoggedProviderAttempt) -> None:
        self._pending_attempts.append((request_id, attempt))

    def summarize_error(
        self,
        *,
        status: str,
        http_status: int | None = None,
        error_code: str | None = None,
    ) -> str | None:
        if status == "success":
            return None
        parts = [status]
        if http_status is not None:
            parts.append(str(http_status))
        if error_code:
            parts.append(error_code)
        return ":".join(parts)

    def add_session_risk_flag(
        self,
        *,
        session_id: int,
        flag: str,
        detail: dict[str, object] | None = None,
    ) -> None:
        self._repository.insert_session_risk_flag(
            session_id=session_id,
            flag=flag,
            detail_json=to_json(detail) if detail is not None else None,
        )

    def flush(self) -> None:
        for request_id, attempt in self._pending_attempts:
            self._repository.insert_api_call_log(
                request_id=request_id,
                session_id=self._session_id,
                turn_index=self._turn_index,
                is_test=self._is_test,
                route=attempt.route,
                provider=attempt.provider,
                model=attempt.model,
                status=attempt.status,
                http_status=attempt.http_status,
                error_code=attempt.error_code,
                error_message_summary=self.summarize_error(
                    status=attempt.status,
                    http_status=attempt.http_status,
                    error_code=attempt.error_code,
                ),
                latency_ms=attempt.latency_ms,
                cooldown_applied=attempt.cooldown_applied,
            )
        for pending in self._pending_cooldowns.values():
            self._repository.upsert_provider_cooldown(
                route=pending.route,
                provider=pending.provider,
                model=pending.model,
                cooldown_until=pending.cooldown_until,
            )
        self._pending_attempts.clear()
        self._pending_cooldowns.clear()

    def discard_pending(self) -> None:
        self._pending_attempts.clear()
        self._pending_cooldowns.clear()
