from __future__ import annotations

import re
from typing import Any, Literal, Optional
import unicodedata

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


ENROLLMENT_NAME_MIN_LENGTH = 2
ENROLLMENT_NAME_MAX_LENGTH = 64
ENROLLMENT_NAME_SEPARATORS = frozenset({" ", "-", "'", "’", ".", "·", "•", "・"})
MAINLAND_MOBILE_PATTERN = re.compile(r"1[3-9][0-9]{9}")
NAME_CONTROL_ERROR = "姓名不能包含控制字符。"
NAME_LENGTH_ERROR = "姓名长度必须为 2 到 64 个字符。"
NAME_LETTER_ERROR = "姓名必须至少包含一个文字字符。"
NAME_CHARACTER_ERROR = "姓名包含不支持的字符。"
PHONE_ASCII_ERROR = "手机号只能使用半角数字。"
PHONE_CONTROL_ERROR = "手机号不能包含控制字符。"
PHONE_FORMAT_ERROR = "请输入有效的中国大陆手机号码。"


def normalize_enrollment_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(NAME_CONTROL_ERROR)
    normalized = " ".join(normalized.split())
    if not ENROLLMENT_NAME_MIN_LENGTH <= len(normalized) <= ENROLLMENT_NAME_MAX_LENGTH:
        raise ValueError(NAME_LENGTH_ERROR)
    if not any(unicodedata.category(character).startswith("L") for character in normalized):
        raise ValueError(NAME_LETTER_ERROR)
    if any(
        not unicodedata.category(character).startswith(("L", "M"))
        and character not in ENROLLMENT_NAME_SEPARATORS
        for character in normalized
    ):
        raise ValueError(NAME_CHARACTER_ERROR)
    return normalized


def normalize_enrollment_phone(value: str) -> str:
    if any(
        character.isdigit() and character not in "0123456789"
        for character in value
    ):
        raise ValueError(PHONE_ASCII_ERROR)
    normalized = unicodedata.normalize("NFKC", value)
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError(PHONE_CONTROL_ERROR)
    compact = re.sub(r"[\s-]+", "", normalized)
    if compact.startswith("+86"):
        compact = compact[3:]
    elif compact.startswith("0086"):
        compact = compact[4:]
    if MAINLAND_MOBILE_PATTERN.fullmatch(compact) is None:
        raise ValueError(PHONE_FORMAT_ERROR)
    return compact


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str
    phone: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return normalize_enrollment_name(value)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        return normalize_enrollment_phone(value)


class PretestSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    demographics: Any = Field(default_factory=dict)
    scales: Any = Field(default_factory=dict)
    slider_touch_state: Any = Field(default_factory=dict)
    page_progress: Any = Field(default_factory=dict)
    client_timestamp: Any = None


class PretestStatusView(BaseModel):
    status: str
    autosave_count: int
    has_draft: bool
    has_final: bool
    last_saved_at: Optional[str] = None
    submitted_at: Optional[str] = None


class ParticipantDayView(BaseModel):
    day_index: int
    calendar_date: str
    status: str
    can_start_experiment: bool


class PretestResponseView(BaseModel):
    day_index: int
    status: str
    autosave_count: int
    payload: dict[str, Any]
    last_saved_at: Optional[str] = None
    submitted_at: Optional[str] = None
    can_start_experiment: bool


class ParticipantView(BaseModel):
    participant_id: int
    attempt_id: int
    attempt_no: int
    name: str
    masked_phone: str
    phone_hash: str
    participant_type: str
    condition: str
    subcondition: str
    topic_key: str
    error_type_id: str
    target_days: int
    current_status: str
    participation_state: str
    participation_message: Optional[str] = None
    current_day: ParticipantDayView
    pretest_status: PretestStatusView


class ParticipantPublicView(BaseModel):
    participant_id: int
    attempt_id: int
    attempt_no: int
    name: str
    masked_phone: str
    participant_type: str
    target_days: int
    current_status: str
    participation_state: str
    participation_message: Optional[str] = None
    current_day: ParticipantDayView
    pretest_status: PretestStatusView


class ClientInfo(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    device_type: Literal["desktop", "mobile", "tablet"]
    viewport_width: int = Field(ge=0)
    is_secure_context: bool
    browser_name: str
    browser_version: Optional[str] = None
    microphone_available: bool
    microphone_permission: Literal["granted", "denied", "prompt", "unavailable"]


class SessionStartRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    is_test: bool = False
    client_info: ClientInfo
    condition: Optional[Literal["human", "tool"]] = None
    subcondition: Optional[
        Literal["qa", "planning", "chat", "decision", "execution"]
    ] = None
    topic_key: Optional[str] = Field(default=None, min_length=1)
    error_type_id: Optional[
        Literal[
            "factual_minor",
            "factual_major",
            "logic_minor",
            "logic_major",
            "social_minor",
            "social_major",
            "system_failure",
        ]
    ] = None
    planned_error_turn: Optional[int] = Field(default=None, ge=1, le=5)


class TurnSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    session_id: str
    operation_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    turn_index: Optional[int] = Field(default=None, ge=1, le=5)
    input_mode: Literal["voice", "text_test_only"]
    user_text: Optional[str] = Field(default=None, min_length=1)
    asr_result_id: Optional[str] = Field(default=None, min_length=32, max_length=128)


class RatingSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    stance_score: int = Field(ge=1, le=5)
    trust_score: int = Field(ge=1, le=7)
    client_elapsed_ms: Optional[int] = Field(default=None, ge=0)


class ClientTimingSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_message_sent_at: AwareDatetime
    assistant_render_completed_at: AwareDatetime
    client_response_latency_ms: int = Field(ge=0, le=3_600_000)
    client_timing_interrupted: bool


class ClientTimingView(ClientTimingSubmitRequest):
    turn_id: int
    render_timing_received_at: str


class TurnRatingView(BaseModel):
    turn_id: int
    stance_score: int
    trust_score: int
    submitted_at: str
    client_elapsed_ms: Optional[int] = None


class TurnView(BaseModel):
    turn_id: int
    session_id: str
    turn_index: int
    user_text: str
    user_input_mode: str
    assistant_text: str
    error_planned: bool
    error_presented: bool
    error_presentation: str
    session_is_test: bool
    artifact_type: Optional[str] = None
    artifact_payload: Optional[Any] = None
    graph_trace: Optional[dict[str, Any]] = None
    provider_attempts: Optional[list[dict[str, Any]]] = None
    evaluator_result: Optional[dict[str, Any]] = None
    rating: Optional[TurnRatingView] = None


class TurnPublicView(BaseModel):
    turn_id: int
    turn_index: int
    user_text: str
    user_input_mode: str
    assistant_text: str
    artifact_type: Optional[str] = None
    artifact_payload: Optional[Any] = None
    rating: Optional[TurnRatingView] = None


class AsrView(BaseModel):
    asr_result_id: str
    asr_status: Literal["success", "failed", "timeout"]
    asr_text: Optional[str] = None
    retry_count: int = 0
    max_retry_per_turn: int


class RatingView(TurnRatingView):
    pass


class SessionView(BaseModel):
    session_id: str
    day_index: int
    status: str
    condition: str
    subcondition: str
    topic_key: str
    error_type_id: str
    planned_error_turn: int
    started_at: str
    completed_at: Optional[str] = None
    is_test: bool
    client_info: ClientInfo
    expected_turn_index: Optional[int] = None
    presentation_mode: Literal["conversation", "execution"]
    artifact_kind: Optional[Literal["schedule_table", "copy_editor"]] = None
    artifact_status: Literal["none", "awaiting_input", "completed", "failed"]
    artifact_type: Optional[str] = None
    artifact_payload: Optional[Any] = None
    graph_trace: Optional[dict[str, Any]] = None
    provider_attempts: Optional[list[dict[str, Any]]] = None
    evaluator_result: Optional[dict[str, Any]] = None
    turns: list[TurnView]


class SessionPublicView(BaseModel):
    session_id: str
    day_index: int
    status: str
    topic_title: str
    topic_description: str
    started_at: str
    completed_at: Optional[str] = None
    is_test: bool
    expected_turn_index: Optional[int] = None
    presentation_mode: Literal["conversation", "execution"]
    artifact_kind: Optional[Literal["schedule_table", "copy_editor"]] = None
    artifact_status: Literal["none", "awaiting_input", "completed", "failed"]
    artifact_type: Optional[str] = None
    artifact_payload: Optional[Any] = None
    turns: list[TurnPublicView]
