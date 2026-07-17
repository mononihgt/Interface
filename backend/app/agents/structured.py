from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_core import PydanticCustomError

from backend.app.services.providers import ProviderResponse


STRUCTURED_ARTIFACT_FALLBACK_TEXT = "抱歉，我暂时无法生成可靠的结构化结果，请补充具体信息后再试。"
SCHEDULE_COLUMNS = ["日期", "时间", "地点", "任务", "备注"]

ArtifactT = TypeVar("ArtifactT", bound=BaseModel)

ERROR_SEVERITY_BY_TYPE = {
    "factual_minor": "minor",
    "factual_major": "major",
    "logic_minor": "minor",
    "logic_major": "major",
    "social_minor": "minor",
    "social_major": "major",
    "system_failure": "system",
}
SEMANTIC_FAILURE_CODES = frozenset(
    {
        "artifact_schema_invalid",
        "compatible_target_missing",
        "error_not_injected",
        "evaluator_local_fallback",
        "evaluator_not_presented",
        "evaluator_presented",
        "generation_local_fallback",
        "invalid_evaluator_json",
        "invalid_json_object",
        "mutated_candidate_invalid",
        "mutation_not_applied",
        "non_finite_number",
        "provider_local_fallback",
        "schema_validation_error",
        "semantic_attempt_failed",
        "semantic_attempts_exhausted",
        "semantic_loop_timeout",
        "structured_mutation_invalid",
        "structured_mutation_disclosure",
        "target_original_mismatch",
        "target_path_missing",
    }
)


def normalize_semantic_failure_code(
    value: object,
    *,
    default: str,
) -> str:
    normalized = str(value).strip() if isinstance(value, str) else ""
    if normalized in SEMANTIC_FAILURE_CODES:
        return normalized
    if default not in SEMANTIC_FAILURE_CODES:
        raise ValueError("default_failure_reason_must_be_allowlisted")
    return default


@dataclass(frozen=True)
class StructuredParseResult(Generic[ArtifactT]):
    value: ArtifactT | None
    validation_error: str | None


@dataclass(frozen=True)
class StructuredAgentResult(Generic[ArtifactT]):
    value: ArtifactT | None
    response: ProviderResponse
    validation_error: str | None
    parse_attempts: int = 1


class ErrorMutation(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    error_type_id: Literal[
        "factual_minor",
        "factual_major",
        "logic_minor",
        "logic_major",
        "social_minor",
        "social_major",
        "system_failure",
    ] = Field(alias="errorTypeId")
    severity: Literal["minor", "major", "system"]
    presentation: Literal["assistant_text", "simulated_ui", "system_failure", "none"]
    target_kind: str = Field(min_length=1, alias="targetKind")
    target_path: str = Field(min_length=1, alias="targetPath")
    original_value: Any = Field(None, alias="originalValue")
    mutated_value: Any = Field(None, alias="mutatedValue")
    applied: bool
    failure_reason: str | None = Field(None, alias="failureReason")
    centrality: Literal["peripheral", "core", "none"]
    operation: str | None = None
    magnitude: str | int | float | None = None
    agent_generated: bool = Field(False, alias="agentGenerated")

    @model_validator(mode="after")
    def validate_mutation_contract(self) -> "ErrorMutation":
        expected_severity = ERROR_SEVERITY_BY_TYPE[self.error_type_id]
        if self.severity != expected_severity:
            raise ValueError("severity_must_match_error_type")
        if self.applied and self.presentation == "none":
            raise ValueError("applied_mutation_requires_presentation")
        if self.applied and not _error_target_path_matches_presentation(
            self.target_path,
            self.presentation,
        ):
            raise ValueError("target_path_must_match_presentation")
        if self.applied and self.original_value == self.mutated_value:
            raise ValueError("applied_mutation_must_change_value")
        if self.applied and self.failure_reason is not None:
            raise ValueError("applied_mutation_cannot_have_failure_reason")
        if not self.applied and self.presentation != "none":
            raise ValueError("unapplied_mutation_must_use_none_presentation")
        if not self.applied and not self.failure_reason:
            raise ValueError("unapplied_mutation_requires_failure_reason")
        if (
            not self.applied
            and self.failure_reason not in SEMANTIC_FAILURE_CODES
        ):
            raise ValueError("failure_reason_must_be_allowlisted")
        return self


def _error_target_path_matches_presentation(path: str, presentation: str) -> bool:
    if presentation in {"assistant_text", "system_failure"}:
        return path == "assistant_text"
    if presentation == "simulated_ui":
        return path == "artifact" or path.startswith("artifact.")
    return False

class StructuredArtifact(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        str_strip_whitespace=True,
    )

    assistant_text: str = Field(min_length=1)


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    attributes: dict[str, str | int | float | bool | None]


class DecisionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    value: str | int | float | bool


class DecisionWeight(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    criterion: str = Field(min_length=1)
    weight: float = Field(ge=0, le=1)


class DecisionRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    option_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    score: float | None = None


class DecisionMatrixArtifact(StructuredArtifact):
    status: Literal["completed", "clarify"] = "completed"
    options: list[DecisionOption] = Field(default_factory=list)
    constraints: list[DecisionConstraint] = Field(default_factory=list)
    weights: list[DecisionWeight] = Field(default_factory=list)
    recommendation: DecisionRecommendation | None = None
    reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_completed_artifact(self) -> "DecisionMatrixArtifact":
        if self.status == "clarify":
            return self
        if not self.options:
            raise PydanticCustomError(
                "options_required_for_completed_artifact",
                "options_required_for_completed_artifact",
            )
        if not self.constraints or not self.weights or self.recommendation is None or not self.reasons:
            raise PydanticCustomError(
                "incomplete_decision_matrix",
                "incomplete_decision_matrix",
            )
        option_ids = {option.id for option in self.options}
        if self.recommendation.option_id not in option_ids:
            raise PydanticCustomError(
                "recommendation_option_not_found",
                "recommendation_option_not_found",
            )
        return self


class PreferenceOption(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    signals: list[str] = Field(min_length=1)


class PreferenceRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    option_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class PreferenceCardsArtifact(StructuredArtifact):
    status: Literal["completed", "clarify"] = "completed"
    mood: str = ""
    preferences: list[str] = Field(default_factory=list)
    options: list[PreferenceOption] = Field(default_factory=list)
    ai_preference: PreferenceRecommendation | None = None
    friend_like_reason: str = ""

    @model_validator(mode="after")
    def validate_completed_artifact(self) -> "PreferenceCardsArtifact":
        if self.status == "clarify":
            return self
        if (
            not self.mood
            or not self.preferences
            or not self.options
            or self.ai_preference is None
            or not self.friend_like_reason
        ):
            raise PydanticCustomError(
                "incomplete_preference_cards",
                "incomplete_preference_cards",
            )
        if self.ai_preference.option_id not in {option.id for option in self.options}:
            raise PydanticCustomError(
                "recommendation_option_not_found",
                "recommendation_option_not_found",
            )
        return self


class ScheduleRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    date: str
    time: str
    location: str
    task: str = Field(min_length=1)
    note: str


class ScheduleArtifact(StructuredArtifact):
    action_type: Literal["schedule_table"] = Field("schedule_table", alias="actionType")
    action_mode: Literal["create", "revise", "clarify"] = Field("create", alias="actionMode")
    status: Literal["completed", "pending", "failed"]
    requested_source: str = Field("", alias="requestedSource")
    columns: list[str] = Field(default_factory=lambda: list(SCHEDULE_COLUMNS))
    rows: list[ScheduleRow] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def reject_empty_completed_rows(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("status") == "completed" and not data.get("rows"):
            raise PydanticCustomError(
                "rows_required_for_completed_artifact",
                "rows_required_for_completed_artifact",
            )
        return data

    @model_validator(mode="after")
    def validate_schedule_shape(self) -> "ScheduleArtifact":
        if self.columns != SCHEDULE_COLUMNS:
            raise PydanticCustomError(
                "invalid_schedule_columns",
                "invalid_schedule_columns",
            )
        if self.action_mode == "clarify" and self.status == "completed":
            raise PydanticCustomError(
                "clarification_cannot_be_completed",
                "clarification_cannot_be_completed",
            )
        return self


class CopyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SelectedCopyVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    version_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class CopyVersionsArtifact(StructuredArtifact):
    action_type: Literal["copy_editor"] = Field("copy_editor", alias="actionType")
    action_mode: Literal["create", "revise", "clarify"] = Field("create", alias="actionMode")
    status: Literal["completed", "pending", "failed"]
    requested_source: str = Field("", alias="requestedSource")
    label: str = ""
    candidates: list[CopyCandidate] = Field(default_factory=list)
    recommended_index: int | None = Field(None, alias="recommendedIndex", ge=0)
    selected_version: SelectedCopyVersion | None = None
    revision_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_copy_shape(self) -> "CopyVersionsArtifact":
        if self.action_mode == "clarify" and self.status == "completed":
            raise PydanticCustomError(
                "clarification_cannot_be_completed",
                "clarification_cannot_be_completed",
            )
        if self.status != "completed":
            return self
        if not 2 <= len(self.candidates) <= 3:
            raise PydanticCustomError(
                "invalid_copy_candidate_count",
                "invalid_copy_candidate_count",
            )
        if (
            self.recommended_index is None
            or self.selected_version is None
            or not self.revision_notes
        ):
            raise PydanticCustomError(
                "incomplete_copy_versions",
                "incomplete_copy_versions",
            )
        if self.recommended_index >= len(self.candidates):
            raise PydanticCustomError(
                "recommended_index_out_of_range",
                "recommended_index_out_of_range",
            )
        recommended = self.candidates[self.recommended_index]
        if self.selected_version.version_id != recommended.id:
            raise PydanticCustomError(
                "selected_version_mismatch",
                "selected_version_mismatch",
            )
        return self


def _reject_non_standard_json_constant(constant: str) -> None:
    raise ValueError(f"Non-standard JSON constant: {constant}")


def _contains_non_finite_number(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(_contains_non_finite_number(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite_number(item) for item in value)
    return False


def _decode_single_json_object(raw_output: str) -> dict[str, Any]:
    if not isinstance(raw_output, str):
        raise ValueError("invalid_json_object")
    cleaned = raw_output.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(\{[\s\S]*\})\s*```",
        cleaned,
        flags=re.IGNORECASE,
    )
    if fenced is not None:
        cleaned = fenced.group(1).strip()

    decoder = json.JSONDecoder(parse_constant=_reject_non_standard_json_constant)
    try:
        decoded = decoder.decode(cleaned)
    except ValueError:
        object_start = cleaned.find("{")
        if object_start < 0:
            raise
        decoded, object_end = decoder.raw_decode(cleaned, object_start)
        prefix = cleaned[:object_start].strip()
        suffix = cleaned[object_end:].strip()
        if len(prefix) > 500 or len(suffix) > 500:
            raise ValueError("wrapped_json_text_too_long")
        if any(token in prefix or token in suffix for token in ("{", "}", "[", "]", "```")):
            raise ValueError("multiple_json_values")
    if not isinstance(decoded, dict):
        raise ValueError("invalid_json_object")
    return decoded


def parse_structured_output(
    raw_output: str,
    schema: type[ArtifactT],
    *,
    payload_normalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> StructuredParseResult[ArtifactT]:
    try:
        decoded = _decode_single_json_object(raw_output)
    except (TypeError, ValueError):
        return StructuredParseResult(value=None, validation_error="invalid_json_object")
    if _contains_non_finite_number(decoded):
        return StructuredParseResult(value=None, validation_error="non_finite_number")
    if payload_normalizer is not None:
        try:
            decoded = payload_normalizer(decoded)
        except (TypeError, ValueError):
            return StructuredParseResult(value=None, validation_error="schema_validation_error")

    try:
        value = schema.model_validate(decoded)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        error_type = str(first_error.get("type", "schema_validation_error"))
        if error_type.endswith("_artifact") or error_type in {
            "invalid_schedule_columns",
            "clarification_cannot_be_completed",
            "incomplete_decision_matrix",
            "incomplete_preference_cards",
            "recommendation_option_not_found",
            "recommended_index_out_of_range",
            "selected_version_mismatch",
            "incomplete_copy_versions",
            "invalid_copy_candidate_count",
        }:
            validation_error = error_type
        else:
            validation_error = "schema_validation_error"
        return StructuredParseResult(value=None, validation_error=validation_error)

    if _contains_non_finite_number(value.model_dump(mode="python")):
        return StructuredParseResult(value=None, validation_error="non_finite_number")

    return StructuredParseResult(value=value, validation_error=None)


def schema_for_artifact(*, condition: str, subcondition: str) -> type[StructuredArtifact]:
    schemas: dict[tuple[str, str], type[StructuredArtifact]] = {
        ("tool", "decision"): DecisionMatrixArtifact,
        ("human", "decision"): PreferenceCardsArtifact,
        ("tool", "execution"): ScheduleArtifact,
        ("human", "execution"): CopyVersionsArtifact,
    }
    try:
        return schemas[(condition, subcondition)]
    except KeyError as exc:
        raise ValueError(f"No structured artifact schema for {condition}/{subcondition}.") from exc
