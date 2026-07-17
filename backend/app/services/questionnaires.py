from __future__ import annotations

from datetime import date, datetime, timezone
import json
import sqlite3

from backend.app.models.api import (
    PretestResponseView,
    PretestStatusView,
    PretestSubmissionRequest,
)
from backend.app.repositories.attempts import get_attempt_by_id
from backend.app.repositories.participants import (
    update_participant_day_status,
)
from backend.app.repositories.pretests import (
    finalize_pretest_response,
    get_latest_pretest_response,
    upsert_pretest_response,
)
from backend.app.services.participant_days import resolve_actionable_participant_day


REQUIRED_DEMOGRAPHIC_KEYS = ("birthDate", "gender", "idNumber")
REQUIRED_SCALE_KEYS = tuple(f"q{index}" for index in range(1, 50))
REQUIRED_SLIDER_KEYS = tuple(f"q{index}" for index in range(27, 48))
REQUIRED_CONFIDENCE_SLIDER_KEYS = tuple(
    f"confidence_q{index}" for index in range(27, 47)
)
LIKERT_KEYS = tuple(f"q{index}" for index in range(1, 27))
SLIDER_KEYS = (*REQUIRED_SLIDER_KEYS, *REQUIRED_CONFIDENCE_SLIDER_KEYS)
FREQUENCY_KEYS = ("q48", "q49")
FREQUENCY_VALUES = frozenset("ABCDEFGH")
PAGE_PROGRESS_KEYS = frozenset({"section", "current_step", "completed_steps"})
PRETEST_STEPS = ("intro", "demographics", "scales", "save")
KNOWN_REQUEST_FIELDS = frozenset(
    {
        "demographics",
        "scales",
        "slider_touch_state",
        "page_progress",
        "client_timestamp",
    }
)

REQUIRED_MESSAGE = "此项为必填项。"
UNKNOWN_MESSAGE = "包含未知字段。"
INVALID_MESSAGE = "格式或选项无效。"
OUT_OF_RANGE_MESSAGE = "数值超出允许范围。"
CONTRADICTORY_MESSAGE = "答案与滑块确认状态不一致。"


class PretestSubmissionConflictError(ValueError):
    pass


class PretestValidationError(ValueError):
    def __init__(self, field_errors: dict[str, str]):
        super().__init__("Pretest responses failed validation.")
        self.field_errors = field_errors

    def detail(self) -> dict[str, object]:
        return {
            "code": "pretest_validation_error",
            "message": "前测问卷包含需要修正的内容。",
            "field_errors": self.field_errors,
        }


def _timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _build_payload(request: PretestSubmissionRequest) -> dict[str, object]:
    return {
        field: getattr(request, field)
        for field in KNOWN_REQUEST_FIELDS
        if field in request.model_fields_set
    }


def _serialize_payload(request: PretestSubmissionRequest) -> tuple[dict[str, object], str]:
    payload = _build_payload(request)
    return payload, json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _ensure_current_day(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
) -> sqlite3.Row:
    attempt_row = get_attempt_by_id(conn, attempt_id=attempt_id)
    if attempt_row is None or int(attempt_row["participant_id"]) != participant_id:
        raise LookupError("Participant attempt not found.")
    resolved_day = resolve_actionable_participant_day(
        conn,
        participant_id=participant_id,
        attempt_row=attempt_row,
    )
    return resolved_day.require_actionable_today()


def _mapping_value(
    value: object,
    *,
    field: str,
    errors: dict[str, str],
) -> dict[str, object]:
    if not isinstance(value, dict):
        errors[field] = INVALID_MESSAGE
        return {}
    return value


def _validate_demographics(
    demographics: dict[str, object],
    *,
    final: bool,
    errors: dict[str, str],
) -> None:
    for key in demographics.keys() - set(REQUIRED_DEMOGRAPHIC_KEYS):
        errors[f"demographics.{key}"] = UNKNOWN_MESSAGE

    if final:
        for key in REQUIRED_DEMOGRAPHIC_KEYS:
            if key not in demographics:
                errors[f"demographics.{key}"] = REQUIRED_MESSAGE

    if "birthDate" in demographics:
        birth_date = demographics["birthDate"]
        try:
            parsed_birth_date = (
                date.fromisoformat(birth_date)
                if isinstance(birth_date, str) and birth_date
                else None
            )
        except ValueError:
            parsed_birth_date = None
        if parsed_birth_date is None or parsed_birth_date > date.today():
            errors["demographics.birthDate"] = INVALID_MESSAGE

    if "gender" in demographics:
        gender = demographics["gender"]
        if not isinstance(gender, str) or gender not in {"男", "女"}:
            errors["demographics.gender"] = INVALID_MESSAGE

    if "idNumber" in demographics:
        id_number = demographics["idNumber"]
        if not isinstance(id_number, str) or len(id_number.strip()) not in {9, 18}:
            errors["demographics.idNumber"] = INVALID_MESSAGE


def _validate_scales(
    scales: dict[str, object],
    *,
    final: bool,
    errors: dict[str, str],
) -> None:
    known_scale_keys = set(REQUIRED_SCALE_KEYS) | set(REQUIRED_CONFIDENCE_SLIDER_KEYS)
    for key in scales.keys() - known_scale_keys:
        errors[f"scales.{key}"] = UNKNOWN_MESSAGE

    if final:
        for key in (*REQUIRED_SCALE_KEYS, *REQUIRED_CONFIDENCE_SLIDER_KEYS):
            if key not in scales:
                errors[f"scales.{key}"] = REQUIRED_MESSAGE

    for key in LIKERT_KEYS:
        if key not in scales:
            continue
        value = scales[key]
        if type(value) is not int:
            errors[f"scales.{key}"] = INVALID_MESSAGE
        elif not 1 <= value <= 5:
            errors[f"scales.{key}"] = OUT_OF_RANGE_MESSAGE

    for key in SLIDER_KEYS:
        if key not in scales:
            continue
        value = scales[key]
        if type(value) is not int:
            errors[f"scales.{key}"] = INVALID_MESSAGE
        elif not 1 <= value <= 100:
            errors[f"scales.{key}"] = OUT_OF_RANGE_MESSAGE

    for key in FREQUENCY_KEYS:
        if key not in scales:
            continue
        value = scales[key]
        if not isinstance(value, str) or value not in FREQUENCY_VALUES:
            errors[f"scales.{key}"] = INVALID_MESSAGE


def _validate_slider_touch_state(
    slider_touch_state: dict[str, object],
    scales: dict[str, object],
    *,
    final: bool,
    errors: dict[str, str],
) -> None:
    slider_keys = set(SLIDER_KEYS)
    for key in slider_touch_state.keys() - slider_keys:
        errors[f"slider_touch_state.{key}"] = UNKNOWN_MESSAGE

    for key, value in slider_touch_state.items():
        if key in slider_keys and type(value) is not bool:
            errors[f"slider_touch_state.{key}"] = INVALID_MESSAGE

    for key in SLIDER_KEYS:
        has_value = key in scales
        touched = slider_touch_state.get(key)
        if has_value and touched is not True:
            errors[f"slider_touch_state.{key}"] = CONTRADICTORY_MESSAGE
        elif touched is True and not has_value:
            errors[f"slider_touch_state.{key}"] = CONTRADICTORY_MESSAGE
        elif final and touched is not True:
            errors.setdefault(f"slider_touch_state.{key}", REQUIRED_MESSAGE)


def _validate_page_progress(
    page_progress: dict[str, object],
    *,
    final: bool,
    errors: dict[str, str],
) -> None:
    for key in page_progress.keys() - PAGE_PROGRESS_KEYS:
        errors[f"page_progress.{key}"] = UNKNOWN_MESSAGE

    section = page_progress.get("section")
    current_step = page_progress.get("current_step")
    completed_steps = page_progress.get("completed_steps")
    if "section" in page_progress and (
        not isinstance(section, str) or section not in PRETEST_STEPS
    ):
        errors["page_progress.section"] = INVALID_MESSAGE
    if "current_step" in page_progress and (
        not isinstance(current_step, str) or current_step not in PRETEST_STEPS
    ):
        errors["page_progress.current_step"] = INVALID_MESSAGE
    if section is not None and current_step is not None and section != current_step:
        errors["page_progress.current_step"] = INVALID_MESSAGE
    if "completed_steps" in page_progress and (
        not isinstance(completed_steps, list)
        or any(
            not isinstance(step, str) or step not in PRETEST_STEPS
            for step in completed_steps
        )
        or len(set(completed_steps)) != len(completed_steps)
    ):
        errors["page_progress.completed_steps"] = INVALID_MESSAGE

    if final:
        for key in PAGE_PROGRESS_KEYS:
            if key not in page_progress:
                errors[f"page_progress.{key}"] = REQUIRED_MESSAGE


def _validate_client_timestamp(
    value: object,
    *,
    final: bool,
    supplied: bool,
    errors: dict[str, str],
) -> None:
    if not supplied:
        if final:
            errors["client_timestamp"] = REQUIRED_MESSAGE
        return
    if not isinstance(value, str) or not value:
        errors["client_timestamp"] = INVALID_MESSAGE
        return
    try:
        datetime.fromisoformat(value)
    except ValueError:
        errors["client_timestamp"] = INVALID_MESSAGE


def validate_pretest_submission(
    request: PretestSubmissionRequest,
    *,
    final: bool,
) -> None:
    errors: dict[str, str] = {}
    for key in (request.model_extra or {}):
        errors[key] = UNKNOWN_MESSAGE

    demographics = _mapping_value(
        request.demographics,
        field="demographics",
        errors=errors,
    )
    scales = _mapping_value(request.scales, field="scales", errors=errors)
    slider_touch_state = _mapping_value(
        request.slider_touch_state,
        field="slider_touch_state",
        errors=errors,
    )
    page_progress = _mapping_value(
        request.page_progress,
        field="page_progress",
        errors=errors,
    )
    _validate_demographics(demographics, final=final, errors=errors)
    _validate_scales(scales, final=final, errors=errors)
    _validate_slider_touch_state(
        slider_touch_state,
        scales,
        final=final,
        errors=errors,
    )
    _validate_page_progress(page_progress, final=final, errors=errors)
    _validate_client_timestamp(
        request.client_timestamp,
        final=final,
        supplied="client_timestamp" in request.model_fields_set,
        errors=errors,
    )

    if errors:
        raise PretestValidationError(errors)


def _get_existing_final(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    day_index: int,
) -> sqlite3.Row | None:
    return get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="final",
        attempt_id=attempt_id,
    )


def _response_from_row(
    row: sqlite3.Row,
    *,
    can_start_experiment: bool,
) -> PretestResponseView:
    return PretestResponseView(
        day_index=int(row["day_index"]),
        status=row["status"],
        autosave_count=int(row["autosave_count"]),
        payload=json.loads(row["payload_json"]),
        last_saved_at=row["last_saved_at"],
        submitted_at=row["submitted_at"],
        can_start_experiment=can_start_experiment,
    )


def get_current_pretest_response(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
) -> PretestResponseView | None:
    for response_status in ("final", "draft"):
        row = get_latest_pretest_response(
            conn,
            participant_id=participant_id,
            day_index=1,
            status=response_status,
            attempt_id=attempt_id,
        )
        if row is not None:
            return _response_from_row(
                row,
                can_start_experiment=response_status == "final",
            )
    return None


def get_pretest_status(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    attempt_id: int | None = None,
) -> PretestStatusView:
    final_row = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="final",
        attempt_id=attempt_id,
    )
    draft_row = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="draft",
        attempt_id=attempt_id,
    )

    if final_row is not None:
        return PretestStatusView(
            status="final",
            autosave_count=int(final_row["autosave_count"]),
            has_draft=draft_row is not None,
            has_final=True,
            last_saved_at=final_row["last_saved_at"],
            submitted_at=final_row["submitted_at"],
        )

    if draft_row is not None:
        return PretestStatusView(
            status="draft",
            autosave_count=int(draft_row["autosave_count"]),
            has_draft=True,
            has_final=False,
            last_saved_at=draft_row["last_saved_at"],
            submitted_at=draft_row["submitted_at"],
        )

    return PretestStatusView(
        status="not_started",
        autosave_count=0,
        has_draft=False,
        has_final=False,
        last_saved_at=None,
        submitted_at=None,
    )


def can_start_formal_session(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    day_index: int,
    attempt_id: int | None = None,
) -> bool:
    if day_index != 1:
        return True

    final_row = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=day_index,
        status="final",
        attempt_id=attempt_id,
    )
    return final_row is not None


def save_pretest_draft(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    request: PretestSubmissionRequest,
) -> PretestResponseView:
    validate_pretest_submission(request, final=False)
    participant_day = _ensure_current_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
    )
    existing_final = _get_existing_final(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        day_index=int(participant_day["day_index"]),
    )
    if existing_final is not None:
        raise PretestSubmissionConflictError(
            f"Final pretest already exists for attempt {attempt_id}, day {participant_day['day_index']}."
        )
    payload, payload_json = _serialize_payload(request)
    existing_draft = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=participant_day["day_index"],
        status="draft",
        attempt_id=attempt_id,
    )
    autosave_count = (
        1
        if existing_draft is None
        else int(existing_draft["autosave_count"]) + 1
    )
    saved_at = _timestamp_now()

    upsert_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=participant_day["day_index"],
        attempt_id=attempt_id,
        status="draft",
        payload_json=payload_json,
        autosave_count=autosave_count,
        last_saved_at=saved_at,
        submitted_at=None,
    )

    if participant_day["status"] == "not_started":
        update_participant_day_status(
            conn,
            participant_day_id=participant_day["id"],
            status="pretest",
            started_at=saved_at,
        )

    return PretestResponseView(
        day_index=int(participant_day["day_index"]),
        status="draft",
        autosave_count=autosave_count,
        payload=payload,
        last_saved_at=saved_at,
        submitted_at=None,
        can_start_experiment=can_start_formal_session(
            conn,
            participant_id=participant_id,
            day_index=int(participant_day["day_index"]),
            attempt_id=attempt_id,
        ),
    )


def submit_pretest_final(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    attempt_id: int,
    request: PretestSubmissionRequest,
) -> PretestResponseView:
    validate_pretest_submission(request, final=True)
    payload, payload_json = _serialize_payload(request)
    existing_final = _get_existing_final(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
        day_index=1,
    )
    if existing_final is not None:
        if json.loads(existing_final["payload_json"]) == payload:
            return _response_from_row(existing_final, can_start_experiment=True)
        raise PretestSubmissionConflictError(
            f"Final pretest already exists for attempt {attempt_id}, day 1."
        )

    participant_day = _ensure_current_day(
        conn,
        participant_id=participant_id,
        attempt_id=attempt_id,
    )
    existing_draft = get_latest_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=int(participant_day["day_index"]),
        status="draft",
        attempt_id=attempt_id,
    )
    autosave_count = 0 if existing_draft is None else int(existing_draft["autosave_count"])
    saved_at = _timestamp_now()

    finalize_pretest_response(
        conn,
        participant_id=participant_id,
        day_index=participant_day["day_index"],
        attempt_id=attempt_id,
        payload_json=payload_json,
        autosave_count=autosave_count,
        saved_at=saved_at,
    )

    if participant_day["status"] in {"not_started", "pretest"}:
        update_participant_day_status(
            conn,
            participant_day_id=participant_day["id"],
            status="pretest",
            started_at=saved_at,
        )

    return PretestResponseView(
        day_index=int(participant_day["day_index"]),
        status="final",
        autosave_count=autosave_count,
        payload=payload,
        last_saved_at=saved_at,
        submitted_at=saved_at,
        can_start_experiment=can_start_formal_session(
            conn,
            participant_id=participant_id,
            day_index=int(participant_day["day_index"]),
            attempt_id=attempt_id,
        ),
    )
