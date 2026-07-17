from __future__ import annotations

from pathlib import Path
import sqlite3

from backend.app.models.api import (
    ParticipantView,
    normalize_enrollment_name,
    normalize_enrollment_phone,
)
from backend.app.repositories.attempts import get_current_attempt
from backend.app.repositories.participants import (
    get_participant_by_id,
    get_participant_by_name_phone,
)
from backend.app.settings import Settings, get_settings
from backend.app.services import attempts as attempt_service
from backend.app.services.recruitment import (
    RecruitmentClosedError,
    recruitment_is_open,
)
from backend.app.time_utils import current_shanghai_date


def _sync_compat_clock() -> None:
    attempt_service.current_shanghai_date = current_shanghai_date


def login_participant(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    data_dir: Path | None = None,
    settings: Settings | None = None,
) -> ParticipantView:
    _sync_compat_clock()
    normalized_name = normalize_enrollment_name(name)
    normalized_phone = normalize_enrollment_phone(phone)
    app_settings = settings or get_settings()
    existing_participant = get_participant_by_name_phone(
        conn,
        name=normalized_name,
        phone=normalized_phone,
    )
    needs_initial_assignment = (
        existing_participant is None
        or existing_participant["current_attempt_id"] is None
    )
    if needs_initial_assignment and not recruitment_is_open(
        conn,
        settings=app_settings,
    ):
        raise RecruitmentClosedError("Formal recruitment is currently closed.")
    return attempt_service.login_or_create_current_attempt(
        conn,
        name=normalized_name,
        phone=normalized_phone,
        data_dir=data_dir,
    )


def get_participant_view_by_id(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
) -> ParticipantView | None:
    participant_row = get_participant_by_id(conn, participant_id=participant_id)
    if participant_row is None:
        return None
    return build_participant_view(conn, participant_row=participant_row)


def build_participant_view(
    conn: sqlite3.Connection,
    *,
    participant_row: sqlite3.Row,
) -> ParticipantView:
    _sync_compat_clock()
    attempt_row = get_current_attempt(
        conn,
        participant_id=int(participant_row["id"]),
    )
    if attempt_row is None:
        raise LookupError("Participant has no current attempt.")
    return attempt_service.build_participant_view(
        conn,
        participant_row=participant_row,
        attempt_row=attempt_row,
    )
