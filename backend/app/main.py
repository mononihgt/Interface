from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from typing import Any, AsyncIterator, Literal, Optional
from urllib.parse import unquote, urlsplit

from fastapi import BackgroundTasks, Cookie, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from backend.app.admin import auth as admin_auth
from backend.app.agents.candidates import (
    ResponseCandidate,
    project_participant_artifact,
    validate_candidate_artifact,
)
from backend.app.admin.auth import (
    admin_username_throttle_key,
    admin_password_needs_migration,
    claim_admin_password_hash,
    fail_admin_login_attempt,
    get_persisted_admin_password_hash,
    hash_admin_password,
    is_admin_auth_configured,
    issue_admin_session_token,
    read_admin_session,
    release_admin_login_attempt,
    renew_admin_login_attempt,
    reserve_admin_login_attempt,
    require_admin_session,
    verify_admin_credentials,
)
from backend.app.db import (
    get_connection,
    migration_state_is_current,
    probe_database_read_write,
    read_transaction,
    run_migrations,
    transaction,
)
from backend.app.models.api import (
    ClientTimingSubmitRequest,
    ClientTimingView,
    RatingSubmitRequest,
    RatingView,
    LoginRequest,
    ParticipantPublicView,
    ParticipantView,
    PretestResponseView,
    PretestSubmissionRequest,
    AsrView,
    SessionPublicView,
    SessionStartRequest,
    SessionView,
    TurnSubmitRequest,
    TurnPublicView,
    TurnView,
)
from backend.app.middleware import AsrRequestBodyLimitMiddleware
from backend.app.repositories.admin import (
    AdminRepository,
    AssignmentBatchConflictError,
)
from backend.app.repositories.external_operations import (
    mark_external_operation_failed,
    mark_external_operation_succeeded,
    release_pending_external_operation,
)
from backend.app.repositories.sessions import get_session_by_uuid
from backend.app.repositories.turns import (
    ClientTimingConflictError,
    get_asr_attempt_by_id,
    get_turn_by_id,
    list_turns_for_session,
    save_client_timing,
)
from backend.app.security import read_signed_session, sign_session_payload
from backend.app.settings import Settings, get_settings
from backend.app.models.domain import TOPIC_LABELS
from backend.app.services.export_jobs import (
    create_export_job,
    delete_export_job,
    get_export_job,
    list_export_jobs,
    run_export_job_background,
    run_export_job,
    start_export_job_recovery,
)
from backend.app.services.participants import (
    get_participant_view_by_id,
    login_participant,
)
from backend.app.services.participant_days import ParticipantDayScheduleError
from backend.app.services.questionnaires import (
    PretestSubmissionConflictError,
    PretestValidationError,
    get_current_pretest_response,
    save_pretest_draft,
    submit_pretest_final,
)
from backend.app.services.api_health import ApiHealthService
from backend.app.services.audio_metadata import (
    AudioDurationError,
    read_audio_duration_seconds,
)
from backend.app.services.asr_tencent import get_asr_client
from backend.app.services.cleanup_attempts import reconcile_cleanup_operations
from backend.app.services.external_operations import (
    ExternalOperationClaim,
    claim_external_operation,
    normalized_operation_id,
    request_fingerprint,
    resolve_external_operation,
)
from backend.app.services.records import to_json
from backend.app.services.providers import ProviderRoutesExhausted
from backend.app.services.recruitment import (
    RecruitmentClosedError,
    recruitment_status,
    set_recruitment_status,
)
from backend.app.services.sessions import (
    MISSING_RATING_COMPLETE_DETAIL,
    complete_session,
    get_session,
    finalize_asr_submission,
    prepare_asr_submission,
    prepare_turn_submission,
    run_asr_submission,
    run_turn_submission,
    start_session,
    start_test_session_without_participant,
    submit_rating,
    submit_turn,
    TEST_CHANNEL_NAME,
    TEST_CHANNEL_PHONE,
    TEST_CHANNEL_PHONE_HASH,
)
from backend.app.time_utils import current_shanghai_date


ASR_UPLOAD_CHUNK_BYTES = 64 * 1024


class _AdminLoginReservationHeartbeat:
    def __init__(self, *, settings: Settings, reservation_token: str) -> None:
        self._settings = settings
        self._reservation_token = reservation_token
        self._stop_event = threading.Event()
        self._ownership_lost = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="admin-login-reservation-heartbeat",
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
        while not self._stop_event.wait(
            admin_auth.LOGIN_RESERVATION_HEARTBEAT_SECONDS
        ):
            try:
                conn = get_connection(self._settings)
                try:
                    with transaction(conn):
                        renewed = renew_admin_login_attempt(
                            conn,
                            reservation_token=self._reservation_token,
                            reservation_ttl_seconds=(
                                admin_auth.LOGIN_RESERVATION_TTL_SECONDS
                            ),
                        )
                finally:
                    conn.close()
            except Exception:
                self._ownership_lost.set()
                return
            if not renewed:
                self._ownership_lost.set()
                return


class AdminLoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    username: str = Field(min_length=1, max_length=128)
    password: str


class AdminRecruitmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    open: bool


class AdminAssignmentControlUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    operation: Optional[Literal["cell"]] = None
    participant_type: Optional[str] = None
    condition: Optional[str] = None
    subcondition: Optional[str] = None
    error_type_id: Optional[str] = None
    cap: Optional[int] = None
    enabled: Optional[bool] = None


class AdminAssignmentCellIdentifier(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_type: str
    condition: str
    subcondition: str
    error_type_id: str


class AdminAssignmentBatchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    participant_type: Optional[str] = None
    condition: Optional[str] = None
    subcondition: Optional[str] = None
    error_type_id: Optional[str] = None
    enabled: Optional[bool] = None
    cap_status: Optional[Literal["capped", "uncapped", "reached"]] = None


class AdminAssignmentBatchScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cells: Optional[list[AdminAssignmentCellIdentifier]] = None
    filter: Optional[AdminAssignmentBatchFilter] = None


class AdminAssignmentBatchChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cap: Optional[int] = Field(default=None, ge=0, strict=True)
    enabled: Optional[bool] = None


class AdminAssignmentCellUpdate(AdminAssignmentCellIdentifier):
    cap: Optional[int] = Field(default=None, ge=0, strict=True)
    enabled: Optional[bool] = None


class AdminAssignmentBatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: AdminAssignmentBatchScope
    changes: AdminAssignmentBatchChanges
    cell_updates: Optional[list[AdminAssignmentCellUpdate]] = None


class AdminAssignmentBatchMutationRequest(AdminAssignmentBatchPreviewRequest):
    scope_version: str = Field(min_length=1, max_length=128)


class AdminExportRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    include_test: bool = False


class AdminExportJobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    export_type: str
    filters: dict[str, Any] = Field(default_factory=dict)
    include_test: bool = False


FRONTEND_RESERVED_PATH_PREFIXES = ("/api", "/admin", "/docs", "/redoc", "/assets")
FRONTEND_RESERVED_EXACT_PATHS = {"/openapi.json"}
INTERNAL_ARTIFACT_KEYS = {
    "errorInjected",
    "error_injected",
    "errorTypeId",
    "error_type_id",
    "originalValue",
    "mutatedValue",
    "mutatedField",
    "original",
    "mutated",
    "plannedErrorTurn",
    "planned_error_turn",
    "scenarioId",
    "scenario_id",
    "condition",
    "subcondition",
    "topicKey",
    "topic_key",
    "provider",
    "providerName",
    "provider_name",
    "providerModel",
    "provider_model",
    "llmProvider",
    "llm_provider",
    "llmModel",
    "llm_model",
    "evaluator",
    "evaluatorResult",
    "evaluator_result",
    "prompt",
    "systemPrompt",
    "system_prompt",
    "validationError",
    "validation_error",
    "validationReason",
    "validation_reason",
    "failureReason",
    "failure_reason",
    "apiKey",
    "api_key",
    "authorization",
    "headers",
    "requestId",
    "request_id",
    "targetKind",
    "target_kind",
    "targetPath",
    "target_path",
    "centrality",
    "operation",
    "magnitude",
    "semanticEvidence",
    "semantic_evidence",
    "errorMutation",
    "error_mutation",
    "errorAttempts",
    "error_attempts",
    "errorSemanticAttemptCount",
    "error_semantic_attempt_count",
}
INTERNAL_ARTIFACT_KEY_TOKENS = frozenset(
    re.sub(r"[^a-z0-9]", "", key.casefold())
    for key in INTERNAL_ARTIFACT_KEYS
)
FRONTEND_INDEX_MAX_BYTES = 4 * 1024 * 1024


def public_participant_view(participant: ParticipantView) -> ParticipantPublicView:
    return ParticipantPublicView.model_validate(participant.model_dump(mode="json"))


def sanitize_participant_artifact_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [sanitize_participant_artifact_payload(item) for item in value]
    if isinstance(value, dict):
        return {
            key: sanitize_participant_artifact_payload(nested_value)
            for key, nested_value in value.items()
            if re.sub(r"[^a-z0-9]", "", str(key).casefold())
            not in INTERNAL_ARTIFACT_KEY_TOKENS
        }
    return value


def participant_safe_artifact_payload(
    artifact_type: str | None,
    value: Any,
) -> Any:
    sanitized = sanitize_participant_artifact_payload(value)
    if artifact_type is None or sanitized is None:
        return None
    try:
        candidate = ResponseCandidate(
            assistant_text="Participant artifact validation.",
            artifact_type=artifact_type,
            artifact_payload=sanitized,
        )
        validate_candidate_artifact(candidate)
        return project_participant_artifact(
            artifact_type=artifact_type,
            payload=sanitized,
            assistant_text=candidate.assistant_text,
        )
    except (TypeError, ValueError):
        return None


def public_turn_view(turn: TurnView) -> TurnPublicView:
    artifact_payload = participant_safe_artifact_payload(
        turn.artifact_type,
        turn.artifact_payload,
    )
    return TurnPublicView(
        turn_id=turn.turn_id,
        turn_index=turn.turn_index,
        user_text=turn.user_text,
        user_input_mode=turn.user_input_mode,
        assistant_text=turn.assistant_text,
        artifact_type=turn.artifact_type if artifact_payload is not None else None,
        artifact_payload=artifact_payload,
        rating=turn.rating,
    )


def public_session_view(session: SessionView) -> SessionPublicView:
    topic = TOPIC_LABELS.get(
        session.topic_key,
        {
            "title": "实验任务",
            "description": "请按照页面提示完成本次实验。",
        },
    )
    artifact_payload = participant_safe_artifact_payload(
        session.artifact_type,
        session.artifact_payload,
    )
    return SessionPublicView(
        session_id=session.session_id,
        day_index=session.day_index,
        status=session.status,
        topic_title=str(topic["title"]),
        topic_description=str(topic["description"]),
        started_at=session.started_at,
        completed_at=session.completed_at,
        is_test=session.is_test,
        expected_turn_index=session.expected_turn_index,
        presentation_mode=session.presentation_mode,
        artifact_kind=session.artifact_kind,
        artifact_status=session.artifact_status,
        artifact_type=session.artifact_type if artifact_payload is not None else None,
        artifact_payload=artifact_payload,
        turns=[public_turn_view(turn) for turn in session.turns],
    )


def participant_safe_session_response(session: SessionView) -> SessionView | SessionPublicView:
    if session.is_test:
        return session
    return public_session_view(session)


def participant_safe_turn_response(turn: TurnView) -> TurnView | TurnPublicView:
    if turn.session_is_test:
        return turn
    return public_turn_view(turn)


def get_frontend_dist_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _ready_component(reason: str | None = None) -> dict[str, str | None]:
    return {
        "status": "ready" if reason is None else "not_ready",
        "reason": reason,
    }


def _storage_directories_ready(settings: Settings) -> bool:
    directories = (
        settings.data_dir,
        settings.data_dir / "audio",
        settings.data_dir / "exports",
        settings.data_dir / "logs",
    )
    for directory in directories:
        probe_path: Path | None = None
        try:
            directory_stat = directory.stat()
            if not stat.S_ISDIR(directory_stat.st_mode):
                return False
            if directory_stat.st_mode & 0o222 == 0:
                return False
            descriptor, raw_probe_path = tempfile.mkstemp(
                prefix=".readiness-",
                dir=directory,
            )
            os.close(descriptor)
            probe_path = Path(raw_probe_path)
            probe_path.unlink()
            probe_path = None
        except (OSError, ValueError):
            return False
        finally:
            if probe_path is not None:
                try:
                    probe_path.unlink(missing_ok=True)
                except OSError:
                    pass
    return True


class _FrontendAssetReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.bundle_references: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.lower()
        attribute_name = "src" if normalized_tag == "script" else "href"
        if normalized_tag not in {"script", "link"}:
            return
        attributes = {
            name.lower(): value
            for name, value in attrs
            if value is not None
        }
        for name, value in attrs:
            if name.lower() == attribute_name and value:
                self.references.append(value)
                if normalized_tag == "script" or "stylesheet" in (
                    attributes.get("rel", "").lower().split()
                ):
                    self.bundle_references.append(value)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)


def _readable_regular_file(
    path: Path,
    *,
    require_nonempty: bool,
    max_bytes: int | None,
) -> bytes | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        path_stat = os.fstat(descriptor)
        if not stat.S_ISREG(path_stat.st_mode):
            return None
        if path_stat.st_mode & 0o444 == 0:
            return None
        if require_nonempty and path_stat.st_size == 0:
            return b""
        if max_bytes is not None and path_stat.st_size > max_bytes:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(1 if max_bytes is None else max_bytes + 1)
        if max_bytes is not None and len(payload) > max_bytes:
            return None
        return payload
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _frontend_asset_reason_at(
    assets_descriptor: int,
    parts: tuple[str, ...],
) -> str | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptors: list[int] = []
    try:
        current_descriptor = os.dup(assets_descriptor)
        descriptors.append(current_descriptor)
        for part in parts[:-1]:
            current_descriptor = os.open(
                part,
                directory_flags,
                dir_fd=current_descriptor,
            )
            descriptors.append(current_descriptor)
        leaf_descriptor = os.open(
            parts[-1],
            file_flags,
            dir_fd=current_descriptor,
        )
        descriptors.append(leaf_descriptor)
        leaf_stat = os.fstat(leaf_descriptor)
        if not stat.S_ISREG(leaf_stat.st_mode) or leaf_stat.st_mode & 0o444 == 0:
            return "frontend_asset_unreadable"
        if leaf_stat.st_size == 0:
            return "frontend_asset_empty"
        if not os.read(leaf_descriptor, 1):
            return "frontend_asset_empty"
        return None
    except FileNotFoundError:
        return "frontend_asset_missing"
    except OSError:
        return "frontend_asset_unreadable"
    finally:
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _directory_has_readable_nonempty_file(
    directory_descriptor: int,
    *,
    remaining_entries: list[int],
) -> bool:
    entry_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    for name in os.listdir(directory_descriptor):
        remaining_entries[0] -= 1
        if remaining_entries[0] < 0:
            return False
        try:
            entry_descriptor = os.open(
                name,
                entry_flags,
                dir_fd=directory_descriptor,
            )
        except OSError:
            continue
        try:
            entry_stat = os.fstat(entry_descriptor)
            if (
                stat.S_ISREG(entry_stat.st_mode)
                and entry_stat.st_mode & 0o444 != 0
                and entry_stat.st_size > 0
            ):
                return True
            if stat.S_ISDIR(entry_stat.st_mode) and _directory_has_readable_nonempty_file(
                entry_descriptor,
                remaining_entries=remaining_entries,
            ):
                return True
        finally:
            os.close(entry_descriptor)
    return False


def _frontend_assets_reason(
    assets_dir: Path,
    *,
    references: list[str],
    bundle_references: list[str],
) -> str | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        assets_descriptor = os.open(assets_dir, directory_flags)
    except OSError:
        return "frontend_assets_missing"
    try:
        opened_assets = os.fstat(assets_descriptor)
        if (
            not stat.S_ISDIR(opened_assets.st_mode)
            or opened_assets.st_mode & 0o555 == 0
        ):
            return "frontend_assets_missing"
        if not os.listdir(assets_descriptor):
            return "frontend_assets_empty"
        has_readable_asset = _directory_has_readable_nonempty_file(
            assets_descriptor,
            remaining_entries=[100_000],
        )

        reason: str | None = None
        valid_local_bundles = 0
        bundle_reference_set = set(bundle_references)
        for reference in references:
            parsed = urlsplit(reference)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            decoded_path = unquote(parsed.path)
            raw_parts = PurePosixPath(decoded_path.lstrip("/")).parts
            if (
                "\x00" in decoded_path
                or "\\" in decoded_path
                or not raw_parts
                or raw_parts[0] != "assets"
                or any(part in ("", ".", "..") for part in raw_parts)
                or len(raw_parts) == 1
            ):
                reason = "frontend_asset_reference_outside_assets"
                break
            reason = _frontend_asset_reason_at(
                assets_descriptor,
                raw_parts[1:],
            )
            if reason is not None:
                break
            if reference in bundle_reference_set:
                valid_local_bundles += 1

        if reason is None and valid_local_bundles == 0:
            reason = "frontend_bundle_reference_missing"
        elif reason is None and not has_readable_asset:
            reason = "frontend_assets_empty"

        try:
            current_assets = assets_dir.lstat()
        except OSError:
            return "frontend_asset_unreadable"
        if (
            not stat.S_ISDIR(current_assets.st_mode)
            or (current_assets.st_dev, current_assets.st_ino)
            != (opened_assets.st_dev, opened_assets.st_ino)
        ):
            return "frontend_asset_unreadable"
        return reason
    finally:
        os.close(assets_descriptor)


def _frontend_readiness_reason(frontend_dist: Path) -> str | None:
    index_file = frontend_dist / "index.html"
    assets_dir = frontend_dist / "assets"
    if not index_file.exists():
        return "frontend_entrypoint_missing"
    index_payload = _readable_regular_file(
        index_file,
        require_nonempty=True,
        max_bytes=FRONTEND_INDEX_MAX_BYTES,
    )
    if index_payload is None:
        return "frontend_entrypoint_unreadable"
    if not index_payload:
        return "frontend_entrypoint_empty"
    try:
        index_html = index_payload.decode("utf-8")
    except UnicodeDecodeError:
        return "frontend_entrypoint_unreadable"

    parser = _FrontendAssetReferenceParser()
    parser.feed(index_html)
    return _frontend_assets_reason(
        assets_dir,
        references=parser.references,
        bundle_references=parser.bundle_references,
    )


def _readiness_components(
    settings: Settings,
    *,
    frontend_static_configured: bool,
) -> dict[str, dict[str, str | None]]:
    components = {
        "database": _ready_component("database_unavailable"),
        "migrations": _ready_component("migration_state_unavailable"),
        "storage": _ready_component(),
        "frontend": _ready_component(),
        "providers": _ready_component(),
    }

    conn = None
    try:
        conn = get_connection(settings)
        probe_database_read_write(conn)
        components["database"] = _ready_component()
        components["migrations"] = _ready_component(
            None if migration_state_is_current(conn) else "migration_state_mismatch"
        )
    except Exception:
        components["database"] = _ready_component("database_unavailable")
        components["migrations"] = _ready_component("migration_state_unavailable")
    finally:
        if conn is not None:
            conn.close()

    if not _storage_directories_ready(settings):
        components["storage"] = _ready_component("storage_not_writable")

    frontend_reason = _frontend_readiness_reason(get_frontend_dist_dir())
    if frontend_reason is None and not frontend_static_configured:
        frontend_reason = "frontend_static_routes_unavailable"
    if frontend_reason is not None:
        components["frontend"] = _ready_component(frontend_reason)

    if not settings.has_required_production_provider_settings():
        components["providers"] = _ready_component(
            "required_provider_settings_missing"
        )
    return components


def is_frontend_route_candidate(path: str) -> bool:
    if path in FRONTEND_RESERVED_EXACT_PATHS:
        return False
    if any(path == prefix or path.startswith(f"{prefix}/") for prefix in FRONTEND_RESERVED_PATH_PREFIXES):
        return False
    return "." not in path.rsplit("/", maxsplit=1)[-1]


def configure_production_static_serving(app: FastAPI) -> FastAPI:
    dist_dir = get_frontend_dist_dir()
    index_file = dist_dir / "index.html"
    assets_dir = dist_dir / "assets"
    app.state.production_static_configured = False

    if _frontend_readiness_reason(dist_dir) is not None:
        return app

    app.mount("/assets", StaticFiles(directory=assets_dir), name="frontend-assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{frontend_path:path}", include_in_schema=False)
    async def serve_frontend(frontend_path: str = ""):
        request_path = f"/{frontend_path}" if frontend_path else "/"
        if not is_frontend_route_candidate(request_path):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return FileResponse(index_file)

    app.state.production_static_configured = True
    return app


def require_session_secret(settings: Settings) -> str:
    if settings.app_secret_key and settings.app_secret_key.strip():
        return settings.app_secret_key

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Server configuration error: participant sessions require app_secret_key.",
    )


def require_authenticated_session(
    *,
    session_token: Optional[str],
    settings: Settings,
) -> tuple[int, int, str]:
    session_secret = require_session_secret(settings)
    if not session_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Login required.",
        )

    payload = read_signed_session(session_token, session_secret)
    if (
        payload is None
        or "participant_id" not in payload
        or "attempt_id" not in payload
        or "phone_hash" not in payload
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        )

    try:
        participant_id = int(payload["participant_id"])
        attempt_id = int(payload["attempt_id"])
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        ) from exc
    return participant_id, attempt_id, str(payload["phone_hash"])


def require_matching_participant_session(
    conn,
    *,
    participant_id: int,
    attempt_id: int,
    phone_hash: str,
) -> ParticipantView:
    participant = get_participant_view_by_id(
        conn,
        participant_id=participant_id,
    )
    if (
        participant is None
        or participant.phone_hash != phone_hash
        or participant.attempt_id != attempt_id
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        )
    return participant


def require_matching_participant_phone(
    conn,
    *,
    participant_id: int,
    phone_hash: str,
) -> ParticipantView:
    participant = get_participant_view_by_id(
        conn,
        participant_id=participant_id,
    )
    if participant is None or participant.phone_hash != phone_hash:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        )
    return participant


def require_non_internal_formal_participant(*, phone_hash: str) -> None:
    if phone_hash == TEST_CHANNEL_PHONE_HASH:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Internal test participant cannot access formal experiment flow.",
        )


def require_session_route_access(
    conn,
    *,
    session_uuid: str,
    participant_session_token: Optional[str],
    admin_session_token: Optional[str],
    settings: Settings,
) -> tuple[int, int, str]:
    session_row = get_session_by_uuid(conn, session_uuid=session_uuid)
    if session_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found.",
        )
    if bool(session_row["is_test"]):
        require_admin_session(session_token=admin_session_token, settings=settings)
        return int(session_row["participant_id"]), 0, TEST_CHANNEL_PHONE_HASH
    participant_id, attempt_id, phone_hash = require_authenticated_session(
        session_token=participant_session_token,
        settings=settings,
    )
    if (
        participant_id != int(session_row["participant_id"])
        or attempt_id != int(session_row["attempt_id"])
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session.",
        )
    return participant_id, attempt_id, phone_hash


def _record_external_operation_failure(
    conn,
    *,
    operation_row_id: int,
    health_service: ApiHealthService,
    error: Exception,
) -> bool:
    try:
        operation_row = conn.execute(
            "SELECT status FROM external_operations WHERE id = ?",
            (operation_row_id,),
        ).fetchone()
        if operation_row is None:
            return False
        operation_status = str(operation_row["status"])
        if operation_status == "failed":
            return True
        if operation_status != "pending":
            return False
        with transaction(conn):
            if (
                isinstance(error, HTTPException)
                and error.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
                and isinstance(error.__cause__, ProviderRoutesExhausted)
            ):
                release_pending_external_operation(
                    conn,
                    operation_row_id=operation_row_id,
                )
            else:
                mark_external_operation_failed(
                    conn,
                    operation_row_id=operation_row_id,
                    error_json=to_json({"type": type(error).__name__}),
                )
            health_service.flush()
        return True
    except Exception as cleanup_error:
        error.add_note(
            "Failed to persist external operation failure: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
        return False


def _remove_unreferenced_audio(
    *,
    settings: Settings,
    relative_audio_path: str,
    error: Exception,
) -> None:
    try:
        (settings.data_dir / relative_audio_path).unlink(missing_ok=True)
    except OSError as cleanup_error:
        error.add_note(
            "Failed to remove unreferenced ASR audio: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )


def _normalized_media_type(content_type: str | None) -> str:
    return (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()


def _allowed_asr_media_types(settings: Settings) -> set[str]:
    return {
        media_type.strip().lower()
        for media_type in settings.asr_allowed_media_types.split(",")
        if media_type.strip()
    }


def _stage_audio_upload(audio: UploadFile, *, settings: Settings) -> tuple[Path, str]:
    media_type = _normalized_media_type(audio.content_type)
    if media_type not in _allowed_asr_media_types(settings):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported audio media type: {media_type}.",
        )

    staging_dir = settings.data_dir / ".asr-uploads"
    staging_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, raw_path = tempfile.mkstemp(
        prefix="asr-",
        suffix=".upload",
        dir=staging_dir,
    )
    staged_path = Path(raw_path)
    total_bytes = 0
    audio_hash = hashlib.sha256()
    try:
        with os.fdopen(file_descriptor, "wb") as staged_file:
            while chunk := audio.file.read(ASR_UPLOAD_CHUNK_BYTES):
                total_bytes += len(chunk)
                if total_bytes > settings.asr_max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        detail=(
                            "Audio upload exceeds the "
                            f"{settings.asr_max_upload_bytes} byte limit."
                        ),
                    )
                staged_file.write(chunk)
                audio_hash.update(chunk)
        if total_bytes == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Audio upload must not be empty.",
            )
        return staged_path, audio_hash.hexdigest()
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise


def _replay_turn_response(
    conn,
    *,
    claim: ExternalOperationClaim,
    participant_id: int,
    attempt_id: int | None,
    session_uuid: str,
) -> dict[str, Any]:
    if claim.result_entity_id is None:
        raise RuntimeError("Succeeded turn operation is missing its result reference.")
    session_view = get_session(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        session_uuid=session_uuid,
    )
    turn_view = next(
        (turn for turn in session_view.turns if turn.turn_id == claim.result_entity_id),
        None,
    )
    if turn_view is None:
        raise RuntimeError("Succeeded turn operation references a missing turn.")
    return participant_safe_turn_response(turn_view).model_dump(mode="json")


def _replay_asr_response(
    conn,
    *,
    claim: ExternalOperationClaim,
    session_uuid: str,
    settings: Settings,
) -> AsrView:
    if claim.result_entity_id is None:
        raise RuntimeError("Succeeded ASR operation is missing its result reference.")
    asr_row = get_asr_attempt_by_id(conn, asr_attempt_id=claim.result_entity_id)
    session_row = get_session_by_uuid(conn, session_uuid=session_uuid)
    if (
        asr_row is None
        or session_row is None
        or int(asr_row["session_id"]) != int(session_row["id"])
    ):
        raise RuntimeError("Succeeded ASR operation references a missing ASR attempt.")
    metadata = claim.replay_metadata or {}
    return AsrView(
        asr_result_id=str(asr_row["result_ref"]),
        asr_status=str(asr_row["asr_status"]),
        asr_text=asr_row["asr_text"],
        retry_count=int(metadata.get("retry_count", 0)),
        max_retry_per_turn=int(
            metadata.get("max_retry_per_turn", settings.asr_max_retry_per_turn)
        ),
    )


def require_formal_session_route_participant(
    conn,
    *,
    participant_id: int,
    attempt_id: int,
    phone_hash: str,
) -> None:
    if phone_hash == TEST_CHANNEL_PHONE_HASH:
        return
    require_matching_participant_session(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        phone_hash=phone_hash,
    )


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        conn = get_connection(app_settings)
        export_recovery_supervisor = None
        try:
            run_migrations(conn)
            reconcile_cleanup_operations(conn, data_dir=app_settings.data_dir)
            export_recovery_supervisor = start_export_job_recovery(app_settings)
            app.state.export_job_recovery_supervisor = export_recovery_supervisor
            yield
        finally:
            if export_recovery_supervisor is not None:
                export_recovery_supervisor.stop()
                export_recovery_supervisor.join()
            conn.close()

    app = FastAPI(
        title=app_settings.app_name,
        debug=app_settings.debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        AsrRequestBodyLimitMiddleware,
        max_bytes=app_settings.asr_max_request_bytes,
    )

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def admin_dashboard():
        index_file = get_frontend_dist_dir() / "index.html"
        if not index_file.exists():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Frontend build not found. Run `cd frontend && npm run build`.",
            )
        return FileResponse(index_file)

    @app.get("/admin/console", include_in_schema=False)
    @app.get("/admin/console/", include_in_schema=False)
    def admin_console_redirect():
        return RedirectResponse(
            url="/admin",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "app": app_settings.app_name,
            "env": app_settings.app_env,
            "database": {
                "reachable": None,
                "status": "not_checked",
            },
            "date": current_shanghai_date(),
        }

    @app.get("/api/readiness")
    def readiness() -> JSONResponse:
        components = _readiness_components(
            app_settings,
            frontend_static_configured=bool(
                getattr(app.state, "production_static_configured", False)
            ),
        )
        is_ready = all(
            component["status"] == "ready" for component in components.values()
        )
        return JSONResponse(
            status_code=(
                status.HTTP_200_OK
                if is_ready
                else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
            content={
                "status": "ready" if is_ready else "not_ready",
                "components": components,
            },
        )

    @app.get("/api/runtime-config")
    def runtime_config() -> dict[str, int]:
        return {
            "asr_max_duration_seconds": app_settings.asr_max_duration_seconds,
        }

    @app.get("/api/recruitment-status")
    def get_recruitment_status() -> dict[str, object]:
        conn = get_connection(app_settings)
        try:
            return recruitment_status(conn, settings=app_settings)
        finally:
            conn.close()

    @app.get("/manifest.json", include_in_schema=False)
    def web_manifest() -> JSONResponse:
        return JSONResponse(
            content={
                "name": app_settings.app_name,
                "short_name": app_settings.app_name,
                "start_url": "/",
                "display": "standalone",
                "background_color": "#ffffff",
                "theme_color": "#111827",
                "icons": [],
            },
            media_type="application/manifest+json",
        )

    @app.post("/api/auth/login", response_model=ParticipantPublicView)
    def auth_login(request: LoginRequest, response: Response) -> ParticipantPublicView:
        session_secret = require_session_secret(app_settings)
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                participant = login_participant(
                    conn,
                    name=request.name,
                    phone=request.phone,
                    data_dir=app_settings.data_dir,
                    settings=app_settings,
                )
        except RecruitmentClosedError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "recruitment_closed",
                    "message": "正式实验招募暂未开放，请稍后再试。",
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

        response.set_cookie(
            key=app_settings.session_cookie_name,
            value=sign_session_payload(
                {
                    "participant_id": participant.participant_id,
                    "attempt_id": participant.attempt_id,
                    "phone_hash": participant.phone_hash,
                },
                session_secret,
            ),
            httponly=True,
            max_age=app_settings.session_ttl_seconds,
            samesite="lax",
            secure=app_settings.app_base_url.startswith("https://"),
        )
        return public_participant_view(participant)

    @app.post("/api/admin/login")
    def admin_login(
        request: AdminLoginRequest,
        response: Response,
        http_request: Request,
    ) -> dict[str, object]:
        username_key = admin_username_throttle_key(request.username)
        client_address = (
            http_request.client.host if http_request.client is not None else "unknown"
        )
        admin_user = app_settings.admin_user.strip()

        conn = get_connection(app_settings)
        try:
            persisted_password_hash = get_persisted_admin_password_hash(
                conn,
                admin_user=admin_user,
            )
            if not is_admin_auth_configured(
                app_settings,
                persisted_password_hash=persisted_password_hash,
            ):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Admin authentication is not configured.",
                )
        finally:
            conn.close()

        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                reservation_token = reserve_admin_login_attempt(
                    conn,
                    username_key=username_key,
                    client_address=client_address,
                    max_failures=app_settings.admin_login_max_failures,
                    reservation_ttl_seconds=(
                        admin_auth.LOGIN_RESERVATION_TTL_SECONDS
                    ),
                )
                if reservation_token is None:
                    AdminRepository(conn, settings=app_settings).record_event(
                        admin_user=admin_user,
                        action="login",
                        payload={
                            "client_address": client_address,
                            "result": "throttled",
                        },
                    )
        finally:
            conn.close()

        if reservation_token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin credentials.",
            )

        heartbeat = _AdminLoginReservationHeartbeat(
            settings=app_settings,
            reservation_token=reservation_token,
        )
        heartbeat.start()
        try:
            credentials_valid = verify_admin_credentials(
                username=request.username,
                password=request.password,
                settings=app_settings,
                persisted_password_hash=persisted_password_hash,
            )
            migrated_password_hash = None
            if credentials_valid and admin_password_needs_migration(
                settings=app_settings,
                persisted_password_hash=persisted_password_hash,
            ):
                migrated_password_hash = hash_admin_password(password=request.password)

            if migrated_password_hash is not None and not heartbeat.ownership_lost:
                conn = get_connection(app_settings)
                try:
                    with transaction(conn):
                        migration_claimed = claim_admin_password_hash(
                            conn,
                            admin_user=admin_user,
                            password_hash=migrated_password_hash,
                        )
                finally:
                    conn.close()

                if not migration_claimed and not heartbeat.ownership_lost:
                    conn = get_connection(app_settings)
                    try:
                        claimed_password_hash = get_persisted_admin_password_hash(
                            conn,
                            admin_user=admin_user,
                        )
                    finally:
                        conn.close()
                    credentials_valid = bool(
                        claimed_password_hash
                        and verify_admin_credentials(
                            username=request.username,
                            password=request.password,
                            settings=app_settings,
                            persisted_password_hash=claimed_password_hash,
                        )
                    )
        finally:
            heartbeat.stop()

        ownership_lost = heartbeat.ownership_lost
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                if credentials_valid and not ownership_lost:
                    reservation_finalized = release_admin_login_attempt(
                        conn,
                        reservation_token=reservation_token,
                    )
                else:
                    reservation_finalized = fail_admin_login_attempt(
                        conn,
                        reservation_token=reservation_token,
                        window_seconds=app_settings.admin_login_window_seconds,
                    )
                login_succeeded = bool(
                    credentials_valid
                    and not ownership_lost
                    and reservation_finalized
                )
                result = "success" if login_succeeded else "failure"
                AdminRepository(conn, settings=app_settings).record_event(
                    admin_user=admin_user,
                    action="login",
                    payload={
                        "client_address": client_address,
                        "result": result,
                    },
                )
        finally:
            conn.close()

        if not login_succeeded:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid admin credentials.",
            )

        response.set_cookie(
            key=app_settings.admin_session_cookie,
            value=issue_admin_session_token(settings=app_settings),
            httponly=True,
            max_age=app_settings.session_ttl_seconds,
            samesite="lax",
            secure=app_settings.app_base_url.startswith("https://"),
        )
        return {"admin_user": admin_user, "ok": True}

    @app.get("/api/admin/session")
    def admin_session(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = read_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        return {
            "authenticated": admin_user is not None,
            "admin_user": admin_user,
        }

    @app.post("/api/admin/logout")
    def admin_logout(response: Response) -> dict[str, object]:
        response.delete_cookie(
            key=app_settings.admin_session_cookie,
            path="/",
            samesite="lax",
            secure=app_settings.app_base_url.startswith("https://"),
        )
        return {"ok": True}

    @app.get("/api/admin/overview")
    def admin_overview(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_overview_metrics()
        finally:
            conn.close()

    @app.get("/api/admin/system-metrics")
    def admin_system_metrics(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_system_metrics()
        finally:
            conn.close()

    @app.get("/api/admin/data-metrics")
    def admin_data_metrics(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_data_monitor_summary()
        finally:
            conn.close()

    @app.get("/api/admin/provider-model-usage")
    def admin_provider_model_usage(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_provider_model_usage()
        finally:
            conn.close()

    @app.get("/api/admin/participants")
    def admin_participants(
        query: str = "",
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).search_participants(
                query=query
            )
        finally:
            conn.close()

    @app.get("/api/admin/assignment-control")
    def admin_assignment_control(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(
                conn,
                settings=app_settings,
            ).get_assignment_control_summary()
        finally:
            conn.close()

    @app.post("/api/admin/assignment-control/batch/preview")
    def admin_preview_assignment_control_batch(
        request: AdminAssignmentBatchPreviewRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            with read_transaction(conn):
                return AdminRepository(
                    conn,
                    settings=app_settings,
                ).preview_assignment_control_batch(
                    scope=request.scope.model_dump(exclude_none=True),
                    changes=request.changes.model_dump(exclude_unset=True),
                    cap_is_set="cap" in request.changes.model_fields_set,
                    cell_updates=(
                        [
                            update.model_dump(exclude_unset=True)
                            for update in request.cell_updates
                        ]
                        if request.cell_updates is not None
                        else None
                    ),
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.post("/api/admin/assignment-control/batch")
    def admin_apply_assignment_control_batch(
        request: AdminAssignmentBatchMutationRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                return AdminRepository(
                    conn,
                    settings=app_settings,
                ).apply_assignment_control_batch(
                    admin_user=admin_user,
                    scope=request.scope.model_dump(exclude_none=True),
                    changes=request.changes.model_dump(exclude_unset=True),
                    cap_is_set="cap" in request.changes.model_fields_set,
                    scope_version=request.scope_version,
                    cell_updates=(
                        [
                            update.model_dump(exclude_unset=True)
                            for update in request.cell_updates
                        ]
                        if request.cell_updates is not None
                        else None
                    ),
                )
        except AssignmentBatchConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.get("/api/admin/clean-data-audits")
    def admin_clean_data_audits(
        audit_status: str = Query(default="", alias="status"),
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).list_clean_data_audits(
                status=audit_status
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.post("/api/admin/clean-data-audits/recompute")
    def admin_recompute_clean_data_audits(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).recompute_clean_data_audits(
                admin_user=admin_user
            )
        finally:
            conn.close()

    @app.post("/api/admin/assignment-control")
    def admin_update_assignment_control(
        request: AdminAssignmentControlUpdateRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                return AdminRepository(
                    conn,
                    settings=app_settings,
                ).update_assignment_controls(
                    admin_user=admin_user,
                    operation=request.operation,
                    participant_type=request.participant_type,
                    condition=request.condition,
                    subcondition=request.subcondition,
                    error_type_id=request.error_type_id,
                    cap=request.cap,
                    enabled=request.enabled,
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.get("/api/admin/api-health")
    def admin_api_health(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_api_health_summary()
        finally:
            conn.close()

    @app.post("/api/admin/providers/deepseek/test")
    def admin_test_deepseek(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).test_deepseek(
                admin_user=admin_user
            )
        finally:
            conn.close()

    @app.post("/api/admin/recruitment")
    def admin_set_recruitment(
        request: AdminRecruitmentRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                changed = set_recruitment_status(
                    conn,
                    admin_user=admin_user,
                    is_open=request.open,
                )
                if changed:
                    AdminRepository(conn, settings=app_settings).record_event(
                        admin_user=admin_user,
                        action="set_recruitment",
                        target_type="recruitment",
                        target_id="formal",
                        payload={"status": "open" if request.open else "closed"},
                    )
            return recruitment_status(conn, settings=app_settings)
        finally:
            conn.close()

    @app.post("/api/admin/export-jobs")
    def admin_create_export_job(
        request: AdminExportJobCreateRequest,
        background_tasks: BackgroundTasks,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            job = create_export_job(
                conn,
                export_type=request.export_type,
                filters=request.filters,
                include_test=request.include_test,
                created_by=admin_user,
            )
            background_tasks.add_task(
                run_export_job_background,
                settings=app_settings,
                job_uuid=str(job["job_uuid"]),
            )
            return job
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.get("/api/admin/export-jobs")
    def admin_list_export_jobs(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return {"items": list_export_jobs(conn)}
        finally:
            conn.close()

    @app.get("/api/admin/export-jobs/{job_uuid}")
    def admin_get_export_job(
        job_uuid: str,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return get_export_job(conn, job_uuid=job_uuid)
        finally:
            conn.close()

    @app.delete("/api/admin/export-jobs/{job_uuid}")
    def admin_delete_export_job(
        job_uuid: str,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return delete_export_job(
                conn,
                settings=app_settings,
                job_uuid=job_uuid,
            )
        finally:
            conn.close()

    @app.get("/api/admin/export-jobs/{job_uuid}/download")
    def admin_download_export_job(
        job_uuid: str,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> FileResponse:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            job = get_export_job(conn, job_uuid=job_uuid)
        finally:
            conn.close()

        if job["status"] != "succeeded" or not job.get("output_path"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Export job is not ready for download.",
            )

        output_path = Path(str(job["output_path"])).resolve()
        exports_dir = (app_settings.data_dir / "exports").resolve()
        if exports_dir not in output_path.parents:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid export path.",
            )
        if not output_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Export file not found.",
            )

        return FileResponse(output_path, filename=output_path.name)

    @app.post("/api/admin/export")
    def admin_export(
        request: Optional[AdminExportRequest] = None,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        admin_user = require_admin_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).export_sanitized_data(
                admin_user=admin_user,
                include_test=False if request is None else request.include_test,
            )
        finally:
            conn.close()

    @app.get("/api/admin/system-logs")
    def admin_system_logs(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> dict[str, object]:
        require_admin_session(session_token=session_token, settings=app_settings)
        conn = get_connection(app_settings)
        try:
            return AdminRepository(conn, settings=app_settings).get_system_logs_summary()
        finally:
            conn.close()

    @app.get("/api/me", response_model=ParticipantPublicView)
    def read_me(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
    ) -> ParticipantPublicView:
        participant_id, attempt_id, expected_phone_hash = require_authenticated_session(
            session_token=session_token,
            settings=app_settings,
        )

        conn = get_connection(app_settings)
        try:
            participant = require_matching_participant_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
        finally:
            conn.close()

        return public_participant_view(participant)

    @app.get("/api/pretest/current", response_model=PretestResponseView | None)
    def get_pretest_current(
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
    ) -> PretestResponseView | None:
        participant_id, attempt_id, expected_phone_hash = require_authenticated_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            participant = require_matching_participant_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            require_non_internal_formal_participant(phone_hash=participant.phone_hash)
            return get_current_pretest_response(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
            )
        finally:
            conn.close()

    @app.post("/api/pretest/draft", response_model=PretestResponseView)
    def post_pretest_draft(
        request: PretestSubmissionRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
    ) -> PretestResponseView:
        participant_id, attempt_id, expected_phone_hash = require_authenticated_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            participant = require_matching_participant_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            require_non_internal_formal_participant(phone_hash=participant.phone_hash)
            with transaction(conn):
                return save_pretest_draft(
                    conn,
                    participant_id=participant_id,
                    attempt_id=attempt_id,
                    request=request,
                )
        except ParticipantDayScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except PretestSubmissionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except PretestValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exc.detail(),
            ) from exc
        finally:
            conn.close()

    @app.post("/api/pretest/final", response_model=PretestResponseView)
    def post_pretest_final(
        request: PretestSubmissionRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
    ) -> PretestResponseView:
        participant_id, attempt_id, expected_phone_hash = require_authenticated_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            participant = require_matching_participant_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            require_non_internal_formal_participant(phone_hash=participant.phone_hash)
            with transaction(conn):
                return submit_pretest_final(
                    conn,
                    participant_id=participant_id,
                    attempt_id=attempt_id,
                    request=request,
                )
        except ParticipantDayScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except PretestSubmissionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except PretestValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=exc.detail(),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.post("/api/sessions/start", response_model=SessionPublicView)
    def post_session_start(
        request: SessionStartRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
    ) -> SessionPublicView:
        participant_id, attempt_id, expected_phone_hash = require_authenticated_session(
            session_token=session_token,
            settings=app_settings,
        )
        conn = get_connection(app_settings)
        try:
            participant = require_matching_participant_session(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            if request.is_test:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Formal session start cannot create test sessions.",
                )
            require_non_internal_formal_participant(phone_hash=participant.phone_hash)
            with transaction(conn):
                session_view = start_session(
                    conn,
                    participant_id=participant_id,
                    attempt_id=attempt_id,
                    request=request,
                    settings=app_settings,
                )
                return public_session_view(session_view)
        except ParticipantDayScheduleError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        finally:
            conn.close()

    @app.post("/api/test/sessions/start", response_model=SessionView)
    def post_test_session_start(
        request: SessionStartRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> SessionView:
        require_admin_session(session_token=session_token, settings=app_settings)
        if not app_settings.test_channel_enabled:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Test channel is disabled.",
            )
        conn = get_connection(app_settings)
        try:
            with transaction(conn):
                session_view = start_test_session_without_participant(
                    conn,
                    request=request,
                    settings=app_settings,
                )
        finally:
            conn.close()
        return session_view

    @app.get("/api/sessions/{session_id}")
    def read_session(
        session_id: str,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> Any:
        conn = get_connection(app_settings)
        try:
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                conn,
                session_uuid=session_id,
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            session_view = get_session(
                conn,
                participant_id=participant_id,
                session_uuid=session_id,
                attempt_id=None
                if expected_phone_hash == TEST_CHANNEL_PHONE_HASH
                else attempt_id,
            )
            return participant_safe_session_response(session_view)
        finally:
            conn.close()

    @app.post("/api/turns")
    def post_turn(
        request: TurnSubmitRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> Any:
        operation_id = normalized_operation_id(request.operation_id)
        prepare_conn = get_connection(app_settings)
        try:
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                prepare_conn,
                session_uuid=request.session_id,
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                prepare_conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            attempt_scope_id = (
                None if expected_phone_hash == TEST_CHANNEL_PHONE_HASH else attempt_id
            )
            with transaction(prepare_conn):
                session_view = get_session(
                    prepare_conn,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_uuid=request.session_id,
                )
                operation_turn_index = request.turn_index or session_view.expected_turn_index
                if operation_turn_index is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Session is not active: {session_view.status}."
                            if session_view.status != "started"
                            else "Session already has the maximum 5 turns."
                        ),
                    )
                session_row = get_session_by_uuid(
                    prepare_conn,
                    session_uuid=request.session_id,
                )
                if session_row is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Session not found.",
                    )
                fingerprint = request_fingerprint(
                    {
                        "session_id": request.session_id,
                        "turn_index": operation_turn_index,
                        "input_mode": request.input_mode,
                        "user_text": request.user_text,
                        "asr_result_id": request.asr_result_id,
                    }
                )
                existing = resolve_external_operation(
                    prepare_conn,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_id=int(session_row["id"]),
                    kind="turn",
                    turn_index=operation_turn_index,
                )
                if existing is not None and existing.result_entity_id is not None:
                    return _replay_turn_response(
                        prepare_conn,
                        claim=existing,
                        participant_id=participant_id,
                        attempt_id=attempt_scope_id,
                        session_uuid=request.session_id,
                    )
                prepared = prepare_turn_submission(
                    prepare_conn,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    request=request,
                )
                if prepared.turn_index != operation_turn_index:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Turn operation is stale for the current session state.",
                    )
                claim = claim_external_operation(
                    prepare_conn,
                    operation_id=operation_id,
                    fingerprint=fingerprint,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_id=int(session_row["id"]),
                    kind="turn",
                    turn_index=operation_turn_index,
                )
                if claim.row_id is None:
                    raise RuntimeError("Turn operation was not claimed.")
                operation_row_id = claim.row_id
        finally:
            prepare_conn.close()

        execution_conn = get_connection(app_settings)
        health_service = ApiHealthService(
            execution_conn,
            session_id=int(prepared.session_row["id"]),
            turn_index=prepared.turn_index,
            is_test=bool(prepared.session_row["is_test"]),
        )
        try:
            executed = run_turn_submission(
                execution_conn,
                prepared=prepared,
                settings=app_settings,
                health_service=health_service,
            )
            with transaction(execution_conn):
                turn_view = submit_turn(
                    execution_conn,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    request=request,
                    settings=app_settings,
                    health_service=health_service,
                    expected_turn_index=prepared.turn_index,
                    provider_result_override=executed.provider_result,
                    turn_result_override=executed.turn_result,
                )
                safe_turn = participant_safe_turn_response(turn_view)
                result_payload = safe_turn.model_dump(mode="json")
                mark_external_operation_succeeded(
                    execution_conn,
                    operation_row_id=operation_row_id,
                    result_entity_id=turn_view.turn_id,
                )
                health_service.flush()
            return result_payload
        except Exception as exc:
            _record_external_operation_failure(
                execution_conn,
                operation_row_id=operation_row_id,
                health_service=health_service,
                error=exc,
            )
            raise
        finally:
            execution_conn.close()

    @app.post("/api/asr", response_model=AsrView)
    def post_asr(
        session_id: str = Form(...),
        operation_id: Optional[str] = Form(default=None),
        turn_index: Optional[int] = Form(default=None, ge=1, le=5),
        audio: UploadFile = File(...),
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> AsrView:
        normalized_id = normalized_operation_id(operation_id)
        staged_audio_path, audio_sha256 = _stage_audio_upload(
            audio,
            settings=app_settings,
        )
        prepared = None
        prepare_conn = None
        try:
            prepare_conn = get_connection(app_settings)
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                prepare_conn,
                session_uuid=session_id,
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                prepare_conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            attempt_scope_id = (
                None if expected_phone_hash == TEST_CHANNEL_PHONE_HASH else attempt_id
            )
            media_type = _normalized_media_type(audio.content_type)
            try:
                duration_seconds = read_audio_duration_seconds(
                    staged_audio_path,
                    media_type=media_type,
                )
            except AudioDurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Audio duration could not be determined.",
                ) from exc
            if duration_seconds > app_settings.asr_max_duration_seconds:
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail=(
                        "Audio duration exceeds the "
                        f"{app_settings.asr_max_duration_seconds} second limit."
                    ),
                )

            with transaction(prepare_conn):
                session_view = get_session(
                    prepare_conn,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_uuid=session_id,
                )
                operation_turn_index = turn_index or session_view.expected_turn_index
                if operation_turn_index is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Session is not active: {session_view.status}."
                            if session_view.status != "started"
                            else "Session already has the maximum 5 turns."
                        ),
                    )
                session_row = get_session_by_uuid(prepare_conn, session_uuid=session_id)
                if session_row is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Session not found.",
                    )
                fingerprint = request_fingerprint(
                    {
                        "session_id": session_id,
                        "turn_index": operation_turn_index,
                        "filename": audio.filename or "audio.bin",
                        "content_type": audio.content_type,
                        "audio_sha256": audio_sha256,
                        "duration_ms": round(duration_seconds * 1_000),
                    }
                )
                existing = resolve_external_operation(
                    prepare_conn,
                    operation_id=normalized_id,
                    fingerprint=fingerprint,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_id=int(session_row["id"]),
                    kind="asr",
                    turn_index=operation_turn_index,
                )
                if existing is not None and existing.result_entity_id is not None:
                    return _replay_asr_response(
                        prepare_conn,
                        claim=existing,
                        session_uuid=session_id,
                        settings=app_settings,
                    )
                prepared = prepare_asr_submission(
                    prepare_conn,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_uuid=session_id,
                    filename=audio.filename or "audio.bin",
                    content_type=audio.content_type,
                    staged_audio_path=staged_audio_path,
                    audio_sha256=audio_sha256,
                    settings=app_settings,
                )
                if prepared.turn_index != operation_turn_index:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="ASR operation is stale for the current session turn.",
                    )
                claim = claim_external_operation(
                    prepare_conn,
                    operation_id=normalized_id,
                    fingerprint=fingerprint,
                    participant_id=participant_id,
                    attempt_id=attempt_scope_id,
                    session_id=int(session_row["id"]),
                    kind="asr",
                    turn_index=operation_turn_index,
                )
                if claim.row_id is None:
                    raise RuntimeError("ASR operation was not claimed.")
                operation_row_id = claim.row_id
        except Exception as exc:
            if prepared is not None:
                _remove_unreferenced_audio(
                    settings=app_settings,
                    relative_audio_path=prepared.relative_audio_path,
                    error=exc,
                )
            raise
        finally:
            if prepare_conn is not None:
                prepare_conn.close()
            staged_audio_path.unlink(missing_ok=True)

        execution_conn = get_connection(app_settings)
        health_service = ApiHealthService(
            execution_conn,
            session_id=int(prepared.session_row["id"]),
            turn_index=prepared.turn_index,
            is_test=bool(prepared.session_row["is_test"]),
        )
        try:
            asr_result = run_asr_submission(
                prepared,
                asr_client=get_asr_client(app_settings),
            )
            with transaction(execution_conn):
                finalized = finalize_asr_submission(
                    execution_conn,
                    prepared=prepared,
                    asr_result=asr_result,
                    settings=app_settings,
                    health_service=health_service,
                )
                mark_external_operation_succeeded(
                    execution_conn,
                    operation_row_id=operation_row_id,
                    result_entity_id=finalized.asr_attempt_id,
                    result_json=to_json(
                        {
                            "session_status": finalized.session_status,
                            "retry_count": finalized.view.retry_count,
                            "max_retry_per_turn": finalized.view.max_retry_per_turn,
                        }
                    ),
                )
                health_service.flush()
            return finalized.view
        except Exception as exc:
            operation_failed = _record_external_operation_failure(
                execution_conn,
                operation_row_id=operation_row_id,
                health_service=health_service,
                error=exc,
            )
            if operation_failed:
                _remove_unreferenced_audio(
                    settings=app_settings,
                    relative_audio_path=prepared.relative_audio_path,
                    error=exc,
                )
            raise
        finally:
            execution_conn.close()

    @app.post(
        "/api/turns/{turn_id}/client-timing",
        response_model=ClientTimingView,
    )
    def post_client_timing(
        turn_id: int,
        request: ClientTimingSubmitRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> ClientTimingView:
        conn = get_connection(app_settings)
        try:
            turn_row = get_turn_by_id(conn, turn_id=turn_id)
            if turn_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Turn not found.",
                )
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                conn,
                session_uuid=str(turn_row["session_uuid"]),
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            try:
                with transaction(conn):
                    saved_row = save_client_timing(
                        conn,
                        turn_id=turn_id,
                        client_message_sent_at=request.client_message_sent_at.isoformat(),
                        assistant_render_completed_at=(
                            request.assistant_render_completed_at.isoformat()
                        ),
                        client_response_latency_ms=request.client_response_latency_ms,
                        client_timing_interrupted=request.client_timing_interrupted,
                    )
                    if saved_row is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="Turn not found.",
                        )
            except ClientTimingConflictError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=str(exc),
                ) from exc
            return ClientTimingView(
                turn_id=turn_id,
                client_message_sent_at=saved_row["client_message_sent_at"],
                assistant_render_completed_at=(
                    saved_row["assistant_render_completed_at"]
                ),
                client_response_latency_ms=saved_row["client_response_latency_ms"],
                client_timing_interrupted=bool(
                    saved_row["client_timing_interrupted"]
                ),
                render_timing_received_at=saved_row["render_timing_received_at"],
            )
        finally:
            conn.close()

    @app.post(
        "/api/turns/{turn_id}/rating",
        response_model=RatingView | SessionView | SessionPublicView,
    )
    def post_turn_rating(
        turn_id: int,
        request: RatingSubmitRequest,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> RatingView | SessionView | SessionPublicView:
        conn = get_connection(app_settings)
        try:
            turn_row = get_turn_by_id(conn, turn_id=turn_id)
            if turn_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Turn not found.",
                )
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                conn,
                session_uuid=str(turn_row["session_uuid"]),
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            with transaction(conn):
                rating_result = submit_rating(
                    conn,
                    participant_id=participant_id,
                    turn_id=turn_id,
                    request=request,
                    attempt_id=None
                    if expected_phone_hash == TEST_CHANNEL_PHONE_HASH
                    else attempt_id,
                    settings=app_settings,
                )
                if isinstance(rating_result, SessionView):
                    return participant_safe_session_response(rating_result)
                return rating_result
        finally:
            conn.close()

    @app.post("/api/sessions/{session_id}/complete")
    def post_session_complete(
        session_id: str,
        session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.session_cookie_name,
        ),
        admin_session_token: Optional[str] = Cookie(
            default=None,
            alias=app_settings.admin_session_cookie,
        ),
    ) -> Any:
        conn = get_connection(app_settings)
        try:
            participant_id, attempt_id, expected_phone_hash = require_session_route_access(
                conn,
                session_uuid=session_id,
                participant_session_token=session_token,
                admin_session_token=admin_session_token,
                settings=app_settings,
            )
            require_formal_session_route_participant(
                conn,
                participant_id=participant_id,
                attempt_id=attempt_id,
                phone_hash=expected_phone_hash,
            )
            try:
                with transaction(conn):
                    session_view = complete_session(
                        conn,
                        participant_id=participant_id,
                        session_uuid=session_id,
                        attempt_id=None
                        if expected_phone_hash == TEST_CHANNEL_PHONE_HASH
                        else attempt_id,
                        settings=app_settings,
                    )
                    return participant_safe_session_response(session_view)
            except HTTPException as exc:
                if (
                    exc.status_code == status.HTTP_409_CONFLICT
                    and exc.detail == MISSING_RATING_COMPLETE_DETAIL
                ):
                    session_row = get_session_by_uuid(conn, session_uuid=session_id)
                    if session_row is not None and int(session_row["participant_id"]) == participant_id:
                        turn_rows = list_turns_for_session(
                            conn,
                            session_id=int(session_row["id"]),
                        )
                        ApiHealthService(conn).add_session_risk_flag(
                            session_id=int(session_row["id"]),
                            flag="missing_rating",
                            detail={
                                "missing_turn_indexes": [
                                    int(turn_row["turn_index"])
                                    for turn_row in turn_rows
                                    if turn_row["rating_id"] is None
                                ],
                            },
                        )
                raise
        finally:
            conn.close()

    return configure_production_static_serving(app)


app = create_app()
