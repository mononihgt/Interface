from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, status

from backend.app.repositories.external_operations import (
    get_external_operation,
    insert_external_operation,
)
from backend.app.services.records import from_json, to_json


@dataclass(frozen=True)
class ExternalOperationClaim:
    row_id: int | None
    operation_id: str
    result_entity_id: int | None = None
    replay_metadata: dict[str, Any] | None = None


def request_fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(to_json(payload).encode("utf-8")).hexdigest()


def normalized_operation_id(value: str | None) -> str:
    normalized = (value or "").strip()
    return normalized or str(uuid4())


def resolve_external_operation(
    conn,
    *,
    operation_id: str,
    fingerprint: str,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    kind: str,
    turn_index: int,
) -> ExternalOperationClaim | None:
    existing = get_external_operation(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_id=session_id,
        kind=kind,
        turn_index=turn_index,
        operation_id=operation_id,
    )
    if existing is None:
        return None
    if str(existing["request_fingerprint"]) != fingerprint:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "idempotency_key_reused",
                "status": str(existing["status"]),
                "operation_id": operation_id,
            },
        )
    existing_status = str(existing["status"])
    if existing_status == "succeeded":
        if existing["result_entity_id"] is None:
            raise RuntimeError("Succeeded external operation is missing its result reference.")
        metadata = from_json(existing["result_json"], {})
        if not isinstance(metadata, dict):
            raise RuntimeError("Succeeded external operation has invalid result metadata.")
        return ExternalOperationClaim(
            row_id=int(existing["id"]),
            operation_id=operation_id,
            result_entity_id=int(existing["result_entity_id"]),
            replay_metadata=metadata,
        )
    if existing_status == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "external_operation_pending",
                "status": "pending",
                "operation_id": operation_id,
                "retryable": True,
                "retry_after_ms": 250,
            },
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": "external_operation_failed",
            "status": "failed",
            "operation_id": operation_id,
            "retryable": False,
        },
    )


def claim_external_operation(
    conn,
    *,
    operation_id: str,
    fingerprint: str,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    kind: str,
    turn_index: int,
) -> ExternalOperationClaim:
    existing = resolve_external_operation(
        conn,
        operation_id=operation_id,
        fingerprint=fingerprint,
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_id=session_id,
        kind=kind,
        turn_index=turn_index,
    )
    if existing is not None:
        return existing
    row_id = insert_external_operation(
        conn,
        operation_id=operation_id,
        request_fingerprint=fingerprint,
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_id=session_id,
        kind=kind,
        turn_index=turn_index,
    )
    return ExternalOperationClaim(row_id=row_id, operation_id=operation_id)
