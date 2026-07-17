from __future__ import annotations

from copy import deepcopy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.agents.structured import (
    CopyVersionsArtifact,
    DecisionMatrixArtifact,
    PreferenceCardsArtifact,
    ScheduleArtifact,
)
from backend.app.services.weather import ParticipantWeatherCard


PathToken = str | int
_PATH_PART = re.compile(r"([^.[\]]+)(?:\[(\d+)\])?")


class MutationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    error_type_id: str = Field(min_length=1)
    target_kind: str = Field(min_length=1)
    target_path: str = Field(min_length=1)
    original_value: Any
    mutated_value: Any
    centrality: Literal["peripheral", "core", "none"]
    presentation: Literal["assistant_text", "simulated_ui", "system_failure"]
    agent_generated: bool = False
    operation: str = Field(min_length=1)
    magnitude: str | int | float

    @model_validator(mode="after")
    def validate_changed_value(self) -> "MutationTarget":
        if self.original_value == self.mutated_value:
            raise ValueError("mutation_target_must_change_value")
        if not _target_path_matches_presentation(
            self.target_path,
            self.presentation,
        ):
            raise ValueError("target_path_must_match_presentation")
        return self


class ResponseCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assistant_text: str = Field(min_length=1)
    artifact_type: str | None = None
    artifact_payload: dict[str, Any] | None = None
    mutation_targets: list[MutationTarget] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_artifact_pair(self) -> "ResponseCandidate":
        if (self.artifact_type is None) != (self.artifact_payload is None):
            raise ValueError("artifact_type_and_payload_must_be_paired")
        return self


def read_candidate_path(candidate: ResponseCandidate, path: str) -> Any:
    tokens = _parse_path(path)
    if tokens == ("assistant_text",):
        return candidate.assistant_text
    if not tokens or tokens[0] != "artifact" or candidate.artifact_payload is None:
        raise KeyError(path)
    value: Any = candidate.artifact_payload
    for token in tokens[1:]:
        if isinstance(token, int):
            if not isinstance(value, list) or token >= len(value):
                raise KeyError(path)
            value = value[token]
        else:
            if not isinstance(value, dict) or token not in value:
                raise KeyError(path)
            value = value[token]
    return deepcopy(value)


def write_candidate_path(
    candidate: ResponseCandidate,
    path: str,
    value: Any,
) -> ResponseCandidate:
    tokens = _parse_path(path)
    updated = candidate.model_copy(deep=True)
    if tokens == ("assistant_text",):
        return ResponseCandidate.model_validate(
            {
                **updated.model_dump(mode="python"),
                "assistant_text": value,
            }
        )
    if not tokens or tokens[0] != "artifact" or updated.artifact_payload is None:
        raise KeyError(path)
    if len(tokens) == 1:
        if not isinstance(value, dict):
            raise TypeError("artifact_root_must_be_object")
        return ResponseCandidate.model_validate(
            {
                **updated.model_dump(mode="python"),
                "artifact_payload": deepcopy(value),
            }
        )

    parent: Any = updated.artifact_payload
    for token in tokens[1:-1]:
        if isinstance(token, int):
            if not isinstance(parent, list) or token >= len(parent):
                raise KeyError(path)
            parent = parent[token]
        else:
            if not isinstance(parent, dict) or token not in parent:
                raise KeyError(path)
            parent = parent[token]
    final = tokens[-1]
    if isinstance(final, int):
        if not isinstance(parent, list) or final >= len(parent):
            raise KeyError(path)
        parent[final] = deepcopy(value)
    else:
        if not isinstance(parent, dict) or final not in parent:
            raise KeyError(path)
        parent[final] = deepcopy(value)

    _synchronize_participant_views(updated, tokens)
    validate_candidate_artifact(updated)
    return ResponseCandidate.model_validate(updated.model_dump(mode="python"))


def validate_candidate_artifact(candidate: ResponseCandidate) -> None:
    artifact_type = candidate.artifact_type
    payload = candidate.artifact_payload
    if artifact_type is None:
        if payload is not None:
            raise ValueError("artifact_payload_without_type")
        return
    if payload is None:
        raise ValueError("artifact_type_without_payload")

    if artifact_type == "weather_card":
        ParticipantWeatherCard.model_validate(payload)
        return
    if artifact_type == "decision_matrix":
        DecisionMatrixArtifact.model_validate(
            {"assistant_text": candidate.assistant_text, **payload}
        )
        return
    if artifact_type == "preference_cards":
        PreferenceCardsArtifact.model_validate(
            {"assistant_text": candidate.assistant_text, **payload}
        )
        return
    if artifact_type == "table":
        normalized = deepcopy(payload)
        normalized["rows"] = [
            {
                key: row[key]
                for key in ("date", "time", "location", "task", "note")
                if key in row
            }
            for row in normalized.get("rows", [])
        ]
        ScheduleArtifact.model_validate(
            {"assistant_text": candidate.assistant_text, **normalized}
        )
        return
    if artifact_type == "copy_versions":
        normalized = deepcopy(payload)
        versions = normalized.pop("versions", normalized.get("candidates"))
        if versions != normalized.get("candidates"):
            raise ValueError("copy_versions_views_must_match")
        CopyVersionsArtifact.model_validate(
            {"assistant_text": candidate.assistant_text, **normalized}
        )
        return
    if artifact_type == "plan_card":
        if not isinstance(payload.get("title"), str) or not payload["title"].strip():
            raise ValueError("plan_card_title_required")
        if not isinstance(payload.get("summary"), str) or not payload["summary"].strip():
            raise ValueError("plan_card_summary_required")
        return
    raise ValueError(f"unsupported_artifact_type:{artifact_type}")


def project_participant_artifact(
    *,
    artifact_type: str,
    payload: dict[str, Any],
    assistant_text: str,
) -> dict[str, Any]:
    if artifact_type == "weather_card":
        return ParticipantWeatherCard.model_validate(payload).model_dump(mode="json")
    if artifact_type == "decision_matrix":
        artifact = DecisionMatrixArtifact.model_validate(
            {"assistant_text": assistant_text, **payload}
        )
        return artifact.model_dump(
            mode="json",
            exclude={"assistant_text", "status"},
        )
    if artifact_type == "preference_cards":
        artifact = PreferenceCardsArtifact.model_validate(
            {"assistant_text": assistant_text, **payload}
        )
        return artifact.model_dump(
            mode="json",
            exclude={"assistant_text", "status"},
        )
    if artifact_type == "table":
        normalized = deepcopy(payload)
        normalized["rows"] = [
            {
                key: row[key]
                for key in ("date", "time", "location", "task", "note")
                if key in row
            }
            for row in normalized.get("rows", [])
        ]
        artifact = ScheduleArtifact.model_validate(
            {"assistant_text": assistant_text, **normalized}
        )
        return artifact.model_dump(
            mode="json",
            by_alias=True,
            exclude={"assistant_text"},
        )
    if artifact_type == "copy_versions":
        normalized = deepcopy(payload)
        versions = normalized.pop("versions", normalized.get("candidates"))
        if versions != normalized.get("candidates"):
            raise ValueError("copy_versions_views_must_match")
        artifact = CopyVersionsArtifact.model_validate(
            {"assistant_text": assistant_text, **normalized}
        )
        projected = artifact.model_dump(
            mode="json",
            by_alias=True,
            exclude={"assistant_text"},
        )
        projected["versions"] = deepcopy(projected["candidates"])
        return projected
    if artifact_type == "plan_card":
        title = payload.get("title")
        summary = payload.get("summary")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("plan_card_title_required")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("plan_card_summary_required")
        return {"title": title, "summary": summary}
    raise ValueError(f"unsupported_artifact_type:{artifact_type}")


def _parse_path(path: str) -> tuple[PathToken, ...]:
    if path in {"assistant_text", "artifact"}:
        return (path,)
    tokens: list[PathToken] = []
    for part in path.split("."):
        match = _PATH_PART.fullmatch(part)
        if match is None:
            raise ValueError(f"invalid_candidate_path:{path}")
        tokens.append(match.group(1))
        if match.group(2) is not None:
            tokens.append(int(match.group(2)))
    if not tokens or tokens[0] not in {"assistant_text", "artifact"}:
        raise ValueError(f"invalid_candidate_path:{path}")
    return tuple(tokens)


def _target_path_matches_presentation(path: str, presentation: str) -> bool:
    if presentation in {"assistant_text", "system_failure"}:
        return path == "assistant_text"
    if presentation == "simulated_ui":
        return path == "artifact" or path.startswith("artifact.")
    return False


def _synchronize_participant_views(
    candidate: ResponseCandidate,
    tokens: tuple[PathToken, ...],
) -> None:
    payload = candidate.artifact_payload
    if payload is None:
        return
    if candidate.artifact_type == "table" and len(tokens) >= 4:
        if tokens[1] == "rows" and isinstance(tokens[2], int) and isinstance(tokens[3], str):
            aliases = {
                "date": "日期",
                "time": "时间",
                "location": "地点",
                "task": "任务",
                "note": "备注",
            }
            rows = payload.get("rows")
            if isinstance(rows, list) and tokens[2] < len(rows):
                alias = aliases.get(tokens[3])
                if alias:
                    rows[tokens[2]][alias] = rows[tokens[2]][tokens[3]]
                else:
                    english = next(
                        (key for key, chinese in aliases.items() if chinese == tokens[3]),
                        None,
                    )
                    if english:
                        rows[tokens[2]][english] = rows[tokens[2]][tokens[3]]
    if candidate.artifact_type == "copy_versions" and len(tokens) >= 4:
        if tokens[1] == "candidates" and isinstance(tokens[2], int):
            payload["versions"] = deepcopy(payload.get("candidates", []))
