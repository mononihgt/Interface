from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
import sqlite3

from backend.app.models.api import ParticipantDayView, ParticipantView
from backend.app.repositories.attempts import (
    AttemptRow,
    create_attempt,
    get_current_attempt,
    set_current_attempt,
    update_attempt_status,
)
from backend.app.repositories.pretests import copy_latest_final_pretest_to_attempt
from backend.app.repositories.participants import (
    create_participant_days,
    get_participant_by_id,
    get_participant_by_name_phone,
    insert_participant_identity,
)
from backend.app.repositories.sessions import (
    delete_sessions_by_ids,
    list_incomplete_formal_sessions_for_attempt,
)
from backend.app.repositories.turns import list_audio_paths_for_sessions
from backend.app.security import hash_phone, mask_phone, normalize_phone
from backend.app.services.assignment import assign_new_participant
from backend.app.services.participant_days import (
    LONG_TERM_MISSED_MESSAGE,
    ResolvedParticipantDay,
    resolve_actionable_participant_day,
)
from backend.app.services.questionnaires import (
    can_start_formal_session,
    get_pretest_status,
)
from backend.app.time_utils import current_shanghai_date


PARTICIPANT_BLOCKED_REASON_MESSAGES = {
    "long_term_missed_day": LONG_TERM_MISSED_MESSAGE,
    "relogin_incomplete_experiment": "本次实验记录已结束，请联系研究人员。",
}


@dataclass(frozen=True)
class CleanupResult:
    deleted_session_ids: list[int]
    audio_paths_to_delete: list[str]
    deleted_audio_paths: list[str]
    failed_audio_paths: list[str]


def login_or_create_current_attempt(
    conn: sqlite3.Connection,
    *,
    name: str,
    phone: str,
    data_dir: Path | None = None,
) -> ParticipantView:
    normalized_phone = normalize_phone(phone)
    phone_hash = hash_phone(normalized_phone)
    participant_row = get_participant_by_name_phone(
        conn,
        name=name,
        phone=normalized_phone,
    )
    if participant_row is None:
        participant_id = insert_participant_identity(
            conn,
            name=name,
            phone=normalized_phone,
            phone_hash=phone_hash,
        )
        _provision_current_attempt_for_participant(
            conn,
            participant_id=participant_id,
            name=name,
            phone_hash=phone_hash,
        )
        participant_row = get_participant_by_id(conn, participant_id=participant_id)
        if participant_row is None:
            raise LookupError("Failed to reload participant after insert.")
    elif participant_row["current_attempt_id"] is None:
        _provision_current_attempt_for_participant(
            conn,
            participant_id=int(participant_row["id"]),
            name=name,
            phone_hash=phone_hash,
        )
        participant_row = get_participant_by_id(
            conn,
            participant_id=int(participant_row["id"]),
        )
        if participant_row is None:
            raise LookupError("Failed to reload participant after attempt creation.")

    attempt_row = get_current_attempt(
        conn,
        participant_id=int(participant_row["id"]),
    )
    if attempt_row is None:
        raise LookupError("Participant has no current attempt.")
    return build_participant_view(
        conn,
        participant_row=participant_row,
        attempt_row=attempt_row,
    )


def abandon_incomplete_attempt_and_reassign(
    conn: sqlite3.Connection,
    *,
    participant_row: sqlite3.Row,
    attempt_row: sqlite3.Row,
    data_dir: Path | None = None,
) -> sqlite3.Row:
    if data_dir is None:
        raise ValueError("Destructive relogin cleanup requires data_dir.")

    old_attempt_id = int(attempt_row["id"])
    delete_incomplete_formal_sessions_for_attempt(
        conn,
        attempt_id=old_attempt_id,
        data_dir=data_dir,
    )
    update_attempt_status(
        conn,
        attempt_id=old_attempt_id,
        status="abandoned",
        valid_for_export=False,
        blocked_reason="relogin_incomplete_experiment",
    )
    new_attempt = _provision_current_attempt_for_participant(
        conn,
        participant_id=int(participant_row["id"]),
        name=str(participant_row["name"]),
        phone_hash=str(participant_row["phone_hash"]),
    )
    copy_latest_final_pretest_to_attempt(
        conn,
        participant_id=int(participant_row["id"]),
        source_attempt_id=old_attempt_id,
        target_attempt_id=int(new_attempt["id"]),
    )
    return new_attempt


def delete_incomplete_formal_sessions_for_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    data_dir: Path,
    deleted_audio_paths_sink: list[str] | None = None,
    failed_audio_paths_sink: list[str] | None = None,
) -> CleanupResult:
    session_rows = list_incomplete_formal_sessions_for_attempt(conn, attempt_id=attempt_id)
    session_ids = [int(row["id"]) for row in session_rows]
    audio_paths = list_audio_paths_for_sessions(conn, session_ids=session_ids)
    del data_dir, deleted_audio_paths_sink, failed_audio_paths_sink
    if audio_paths:
        raise RuntimeError(
            "Audio-bearing session deletion requires durable cleanup operations."
        )

    delete_sessions_by_ids(conn, session_ids=session_ids)
    return CleanupResult(
        deleted_session_ids=session_ids,
        audio_paths_to_delete=audio_paths,
        deleted_audio_paths=[],
        failed_audio_paths=[],
    )


def _provision_current_attempt_for_participant(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    name: str,
    phone_hash: str,
) -> AttemptRow:
    assignment = assign_new_participant(
        conn,
        name=name,
        phone_hash=phone_hash,
    )
    attempt_id = create_attempt(
        conn,
        participant_id=participant_id,
        participant_type=assignment.participant_type,
        condition=assignment.condition,
        subcondition=assignment.subcondition,
        topic_key=assignment.topic_key,
        error_type_id=assignment.error_type_id,
        target_days=assignment.target_days,
    )
    set_current_attempt(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
    )
    create_participant_days(
        conn,
        participant_id=participant_id,
        target_days=assignment.target_days,
        start_date=date.fromisoformat(current_shanghai_date()),
        attempt_id=attempt_id,
    )
    attempt_row = get_current_attempt(conn, participant_id=participant_id)
    if attempt_row is None:
        raise LookupError("Failed to reload attempt after insert.")
    return attempt_row


def build_participant_view(
    conn: sqlite3.Connection,
    *,
    participant_row: sqlite3.Row,
    attempt_row: sqlite3.Row,
) -> ParticipantView:
    attempt_id = int(attempt_row["id"])
    current_status = str(attempt_row["status"])
    resolved_day = resolve_actionable_participant_day(
        conn,
        participant_id=int(participant_row["id"]),
        today=current_shanghai_date(),
        attempt_row=attempt_row,
    )
    current_day_row = resolved_day.row

    current_day_index = int(current_day_row["day_index"])
    pretest_status = get_pretest_status(
        conn,
        participant_id=int(participant_row["id"]),
        day_index=current_day_index,
        attempt_id=attempt_id,
    )
    can_start_experiment = (
        resolved_day.is_actionable
        and str(current_day_row["status"]) != "completed"
        and can_start_formal_session(
            conn,
            participant_id=int(participant_row["id"]),
            day_index=current_day_index,
            attempt_id=attempt_id,
        )
    )
    participation_state = _resolve_participation_state(
        current_status=current_status,
        is_actionable=resolved_day.is_actionable,
        resolution=resolved_day.resolution,
        can_start_experiment=can_start_experiment,
    )
    participation_message = _resolve_participation_message(
        current_status=current_status,
        participation_state=participation_state,
        resolved_day=resolved_day,
        attempt_row=attempt_row,
    )

    return ParticipantView(
        participant_id=int(participant_row["id"]),
        attempt_id=attempt_id,
        attempt_no=int(attempt_row["attempt_no"]),
        name=str(participant_row["name"]),
        masked_phone=mask_phone(str(participant_row["phone"])),
        phone_hash=str(participant_row["phone_hash"]),
        participant_type=str(attempt_row["participant_type"]),
        condition=str(attempt_row["condition"]),
        subcondition=str(attempt_row["subcondition"]),
        topic_key=str(attempt_row["topic_key"]),
        error_type_id=str(attempt_row["error_type_id"]),
        target_days=int(attempt_row["target_days"]),
        current_status=current_status,
        participation_state=participation_state,
        participation_message=participation_message,
        current_day=ParticipantDayView(
            day_index=current_day_index,
            calendar_date=str(current_day_row["calendar_date"]),
            status=str(current_day_row["status"]),
            can_start_experiment=can_start_experiment,
        ),
        pretest_status=pretest_status,
    )


def _resolve_participation_state(
    *,
    current_status: str,
    is_actionable: bool,
    resolution: str,
    can_start_experiment: bool,
) -> str:
    if current_status in {"completed", "converted_to_short"}:
        return "completed"
    if current_status == "blocked":
        return "blocked"
    if resolution == "missed_long_term":
        return "blocked"
    if not is_actionable:
        return "not_scheduled_today"
    if can_start_experiment:
        return "ready_for_experiment"
    return "needs_pretest"


def _resolve_participation_message(
    *,
    current_status: str,
    participation_state: str,
    resolved_day: ResolvedParticipantDay,
    attempt_row: sqlite3.Row,
) -> str | None:
    if participation_state == "blocked":
        blocked_reason = attempt_row["blocked_reason"]
        if resolved_day.resolution == "missed_long_term":
            return LONG_TERM_MISSED_MESSAGE
        if (
            current_status == "blocked"
            and blocked_reason in PARTICIPANT_BLOCKED_REASON_MESSAGES
        ):
            return PARTICIPANT_BLOCKED_REASON_MESSAGES[blocked_reason]
        return "您当前无法继续实验，请联系研究人员。"
    if participation_state == "not_scheduled_today":
        return _resolve_not_scheduled_message(resolved_day)
    return None


def _resolve_not_scheduled_message(resolved_day: ResolvedParticipantDay) -> str:
    day_index = int(resolved_day.row["day_index"])
    if resolved_day.resolution == "before_first" and day_index > 1:
        return "您已经完成今天实验，请明天继续参加，感谢支持！"
    if resolved_day.resolution == "before_first":
        return "今天暂时无法开始实验，请按研究人员安排的日期登录。"
    if resolved_day.resolution == "after_last":
        return "您已完成本次实验，感谢您的参与。"
    return "今天暂时无法继续实验，请按研究人员安排的日期登录。"
