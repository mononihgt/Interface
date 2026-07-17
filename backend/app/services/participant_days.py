from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import sqlite3

from backend.app.repositories.attempts import (
    get_attempt_by_id,
    get_current_attempt,
    update_attempt_status,
)
from backend.app.repositories.participants import (
    get_participant_day_for_date,
    get_participant_days,
    update_participant_day_status,
)
from backend.app.time_utils import current_shanghai_date


LONG_TERM_MISSED_MESSAGE = "您未按要求连续三天完成实验，已无法参与实验。"


class ParticipantDayScheduleError(ValueError):
    """Raised when a participant attempts a day-bound action outside a scheduled day."""


@dataclass(frozen=True)
class ResolvedParticipantDay:
    row: sqlite3.Row
    resolution: str
    today: str

    @property
    def is_scheduled_today(self) -> bool:
        return self.resolution == "scheduled"

    @property
    def is_actionable(self) -> bool:
        return self.resolution in {"scheduled", "late_unfinished_day"}

    def require_scheduled_today(self) -> sqlite3.Row:
        if self.is_scheduled_today:
            return self.row
        raise ParticipantDayScheduleError(self.schedule_error_message)

    def require_actionable_today(self) -> sqlite3.Row:
        if self.is_actionable:
            return self.row
        raise ParticipantDayScheduleError(self.schedule_error_message)

    @property
    def schedule_error_message(self) -> str:
        day_index = int(self.row["day_index"])
        calendar_date = str(self.row["calendar_date"])
        if self.resolution == "missed_long_term":
            return LONG_TERM_MISSED_MESSAGE
        if self.resolution == "before_first":
            return (
                "Participant is not scheduled for today. "
                f"Day {day_index} is scheduled on {calendar_date}."
            )
        if self.resolution == "after_last":
            return (
                "Participant is not scheduled for today. "
                f"The last scheduled day was Day {day_index} on {calendar_date}."
            )
        return (
            "Participant is not scheduled for today. "
            f"The most recent scheduled day was Day {day_index} on {calendar_date}."
        )


def complete_participant_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    participant_day_id: int,
    attempt_id: int | None,
    completed_at: str,
) -> None:
    update_participant_day_status(
        conn,
        participant_day_id=participant_day_id,
        status="completed",
        completed_at=completed_at,
    )
    attempt_row = (
        get_attempt_by_id(conn, attempt_id=attempt_id)
        if attempt_id is not None
        else get_current_attempt(conn, participant_id=participant_id)
    )
    if attempt_row is None:
        return

    attempt_days = get_participant_days(
        conn,
        participant_id=participant_id,
        attempt_id=int(attempt_row["id"]),
    )
    if attempt_days and all(
        str(participant_day["status"]) == "completed"
        for participant_day in attempt_days
    ):
        update_attempt_status(
            conn,
            attempt_id=int(attempt_row["id"]),
            status="completed",
            valid_for_export=bool(attempt_row["valid_for_export"]),
        )


def resolve_current_participant_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    today: str | None = None,
    attempt_id: int | None = None,
) -> ResolvedParticipantDay:
    resolved_today = today or current_shanghai_date()
    scheduled_row = get_participant_day_for_date(
        conn,
        participant_id=participant_id,
        calendar_date=resolved_today,
        attempt_id=attempt_id,
    )
    if scheduled_row is not None:
        return ResolvedParticipantDay(
            row=scheduled_row,
            resolution="scheduled",
            today=resolved_today,
        )

    participant_days = get_participant_days(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
    )
    if not participant_days:
        raise LookupError("Participant has no participant_day rows.")

    current_date = date.fromisoformat(resolved_today)
    first_row = participant_days[0]
    first_date = date.fromisoformat(str(first_row["calendar_date"]))
    if current_date < first_date:
        return ResolvedParticipantDay(
            row=first_row,
            resolution="before_first",
            today=resolved_today,
        )

    last_row = participant_days[-1]
    last_date = date.fromisoformat(str(last_row["calendar_date"]))
    if current_date > last_date:
        return ResolvedParticipantDay(
            row=last_row,
            resolution="after_last",
            today=resolved_today,
        )

    most_recent_row = first_row
    for participant_day in participant_days:
        participant_day_date = date.fromisoformat(str(participant_day["calendar_date"]))
        if participant_day_date >= current_date:
            break
        most_recent_row = participant_day

    return ResolvedParticipantDay(
        row=most_recent_row,
        resolution="between_days",
        today=resolved_today,
    )


def resolve_actionable_participant_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_row: sqlite3.Row,
    today: str | None = None,
) -> ResolvedParticipantDay:
    resolved_today = today or current_shanghai_date()
    participant_days = get_participant_days(
        conn,
        participant_id=participant_id,
        attempt_id=int(attempt_row["id"]),
    )
    if not participant_days:
        raise LookupError("Participant has no participant_day rows.")

    current_date = date.fromisoformat(resolved_today)
    participant_type = str(attempt_row["participant_type"])
    incomplete_days = [
        row for row in participant_days if str(row["status"]) != "completed"
    ]
    if not incomplete_days:
        return resolve_current_participant_day(
            conn,
            participant_id=participant_id,
            today=resolved_today,
            attempt_id=int(attempt_row["id"]),
        )

    if participant_type == "long":
        completed_count = len(participant_days) - len(incomplete_days)
        if completed_count == 0:
            return _resolve_late_allowed_day(
                row=participant_days[0],
                current_date=current_date,
                today=resolved_today,
            )

        next_day = incomplete_days[0]
        next_date = date.fromisoformat(str(next_day["calendar_date"]))
        if current_date > next_date:
            return ResolvedParticipantDay(
                row=next_day,
                resolution="missed_long_term",
                today=resolved_today,
            )
        if current_date == next_date:
            return ResolvedParticipantDay(
                row=next_day,
                resolution="scheduled",
                today=resolved_today,
            )
        return ResolvedParticipantDay(
            row=next_day,
            resolution="before_first",
            today=resolved_today,
        )

    return _resolve_late_allowed_day(
        row=incomplete_days[0],
        current_date=current_date,
        today=resolved_today,
    )


def _resolve_late_allowed_day(
    *,
    row: sqlite3.Row,
    current_date: date,
    today: str,
) -> ResolvedParticipantDay:
    row_date = date.fromisoformat(str(row["calendar_date"]))
    if current_date < row_date:
        return ResolvedParticipantDay(row=row, resolution="before_first", today=today)
    if current_date == row_date:
        return ResolvedParticipantDay(row=row, resolution="scheduled", today=today)
    return ResolvedParticipantDay(
        row=row,
        resolution="late_unfinished_day",
        today=today,
    )
