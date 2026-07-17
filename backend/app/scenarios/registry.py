from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.app.models.domain import ERROR_TYPE_IDS, TOPIC_KEYS_BY_CELL


LEGACY_TOPIC_ALIASES = {
    ("human", "qa", "life_advice"): "advice",
    ("human", "planning", "goal_planning"): "goalPlan",
    ("human", "chat", "daily_chat"): "funStory",
    ("human", "decision", "preference_decision"): "preferenceDecision",
    ("human", "execution", "copy_editing"): "collaborativeExecution",
    ("tool", "qa", "factual_lookup"): "weather",
    ("tool", "planning", "itinerary_planning"): "travelPlan",
    ("tool", "chat", "news_chat"): "news",
    ("tool", "decision", "rational_decision"): "valueDecision",
    ("tool", "execution", "task_table"): "taskExecution",
}


class ClarificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    missing_context: list[str] = Field(min_length=1)
    response_goal: str = Field(min_length=1)


class ToolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    allowed: list[str]
    forbidden: list[str]

    @model_validator(mode="after")
    def validate_disjoint_tools(self) -> "ToolPolicy":
        if set(self.allowed) & set(self.forbidden):
            raise ValueError("allowed_and_forbidden_tools_must_be_disjoint")
        return self


class ResponsePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    max_chars: int = Field(ge=1)
    tone: str = Field(min_length=1)
    capability_limits: list[str] = Field(default_factory=list)


class ArtifactContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    artifact_type: Optional[str] = None
    schema_id: Optional[str] = None
    participant_visible: bool = False

    @model_validator(mode="after")
    def validate_artifact_schema_pair(self) -> "ArtifactContract":
        if (self.artifact_type is None) != (self.schema_id is None):
            raise ValueError("artifact_type_and_schema_id_must_be_paired")
        if self.artifact_type is None and self.participant_visible:
            raise ValueError("missing_artifact_cannot_be_participant_visible")
        return self


class PresentationContract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    allowed_error_presentations: list[
        Literal["assistant_text", "simulated_ui", "system_failure"]
    ] = Field(min_length=1)


class MutationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_kind: str = Field(min_length=1)
    centrality: Literal["peripheral", "core", "none"]
    presentation: Literal["assistant_text", "simulated_ui", "system_failure"]


class MutationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    rules: dict[str, MutationRule]

    @model_validator(mode="after")
    def validate_complete_error_coverage(self) -> "MutationPolicy":
        if set(self.rules) != set(ERROR_TYPE_IDS):
            raise ValueError("mutation_policy_must_cover_all_error_types")
        return self


class ScenarioFixture(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    user_turns: list[str] = Field(min_length=1)
    expected_behavior: str = Field(min_length=1)


class ScenarioFixtures(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    normal: ScenarioFixture
    clarification: ScenarioFixture


class ErrorPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    allowed_presentations: list[str] = Field(min_length=1)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scenario_id: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    subcondition: str = Field(min_length=1)
    topic_key: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    display_title: str = Field(min_length=1)
    participant_brief: str = Field(min_length=1)
    required_context: list[str] = Field(min_length=1)
    clarification: ClarificationPolicy
    tools: ToolPolicy
    response_policy: ResponsePolicy
    artifact: ArtifactContract
    presentation: PresentationContract
    mutation_policy: MutationPolicy
    fixtures: ScenarioFixtures
    max_turns: int = Field(ge=1)
    input_mode: str = Field(min_length=1)
    graph: str = Field(min_length=1)

    @property
    def artifact_type(self) -> str | None:
        return self.artifact.artifact_type

    @property
    def provider_system_prompt(self) -> str:
        requirements = [
            self.system_prompt,
            "场景执行要求：",
            (
                "- 信息不足时需要补充："
                f"{'、'.join(self.clarification.missing_context)}；"
                f"澄清目标：{self.clarification.response_goal}"
            ),
            (
                f"- 回复风格：{self.response_policy.tone}；"
                f"回复不超过{self.response_policy.max_chars}字。"
            ),
        ]
        if self.response_policy.capability_limits:
            requirements.append(
                "- 能力边界："
                + "；".join(self.response_policy.capability_limits)
                + "。"
            )
        if self.tools.allowed:
            requirements.append("- 允许使用：" + "、".join(self.tools.allowed) + "。")
        if self.tools.forbidden:
            requirements.append("- 禁止使用：" + "、".join(self.tools.forbidden) + "。")
        return "\n".join(requirements)

    @property
    def error_policy(self) -> ErrorPolicy:
        return ErrorPolicy(
            allowed_presentations=list(self.presentation.allowed_error_presentations)
        )

    @model_validator(mode="after")
    def validate_topic_contract(self) -> "Scenario":
        expected_topics = TOPIC_KEYS_BY_CELL.get((self.condition, self.subcondition), ())
        if self.topic_key not in expected_topics:
            raise ValueError("topic_key_must_match_condition_subcondition")
        allowed_presentations = set(self.presentation.allowed_error_presentations)
        for error_type_id, rule in self.mutation_policy.rules.items():
            if rule.presentation not in allowed_presentations:
                raise ValueError(
                    f"mutation_presentation_not_allowed:{error_type_id}"
                )
            if rule.presentation == "simulated_ui" and self.artifact_type is None:
                raise ValueError(
                    f"simulated_ui_mutation_requires_artifact:{error_type_id}"
                )
        return self


class ScenarioRegistry:
    def __init__(self, scenarios: list[Scenario], *, require_complete: bool = False) -> None:
        self._scenarios = scenarios
        self._ensure_unique_contracts(scenarios)
        self._by_topic = {
            (scenario.condition, scenario.subcondition, scenario.topic_key): scenario
            for scenario in scenarios
        }
        if require_complete:
            self._ensure_complete_active_topics(scenarios)

    @classmethod
    def load_default(cls) -> "ScenarioRegistry":
        return cls(_load_default_scenarios(), require_complete=True)

    @classmethod
    def load(cls, paths: Sequence[Path]) -> "ScenarioRegistry":
        scenarios: list[Scenario] = []
        for path in paths:
            scenarios.extend(_load_scenarios_file(path))
        return cls(scenarios)

    def list_active(self) -> list[Scenario]:
        return list(self._scenarios)

    def require(self, *, condition: str, subcondition: str, topic_key: str) -> Scenario:
        scenario = self._by_topic.get((condition, subcondition, topic_key))
        if scenario is None:
            raise KeyError(
                f"Unknown active scenario for {condition}/{subcondition}/{topic_key}."
            )
        return scenario

    def resolve_legacy(
        self,
        *,
        condition: str,
        subcondition: str,
        topic_key: str,
    ) -> Scenario:
        canonical_topic = LEGACY_TOPIC_ALIASES.get(
            (condition, subcondition, topic_key)
        )
        if canonical_topic is None:
            raise KeyError(
                f"Unknown legacy scenario for {condition}/{subcondition}/{topic_key}."
            )
        return self.require(
            condition=condition,
            subcondition=subcondition,
            topic_key=canonical_topic,
        )

    def resolve_persisted(
        self,
        *,
        condition: str,
        subcondition: str,
        topic_key: str,
    ) -> Scenario:
        try:
            return self.require(
                condition=condition,
                subcondition=subcondition,
                topic_key=topic_key,
            )
        except KeyError:
            return self.resolve_legacy(
                condition=condition,
                subcondition=subcondition,
                topic_key=topic_key,
            )

    @staticmethod
    def _ensure_unique_contracts(scenarios: list[Scenario]) -> None:
        seen_ids: set[str] = set()
        seen_topics: set[tuple[str, str, str]] = set()
        for scenario in scenarios:
            if scenario.scenario_id in seen_ids:
                raise ValueError(f"Duplicate scenario_id: {scenario.scenario_id}.")
            seen_ids.add(scenario.scenario_id)
            topic = (scenario.condition, scenario.subcondition, scenario.topic_key)
            if topic in seen_topics:
                raise ValueError(
                    "Duplicate active scenario entry for "
                    f"{scenario.condition}/{scenario.subcondition}/{scenario.topic_key}."
                )
            seen_topics.add(topic)

    @staticmethod
    def _ensure_complete_active_topics(scenarios: list[Scenario]) -> None:
        expected = {
            (condition, subcondition, topic_key)
            for (condition, subcondition), topic_keys in TOPIC_KEYS_BY_CELL.items()
            for topic_key in topic_keys
        }
        actual = {
            (scenario.condition, scenario.subcondition, scenario.topic_key)
            for scenario in scenarios
        }
        if actual != expected:
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ValueError(
                f"Active scenario coverage mismatch; missing={missing}, unexpected={unexpected}."
            )


@lru_cache(maxsize=1)
def _load_default_scenarios() -> list[Scenario]:
    scenarios_dir = Path(__file__).resolve().parent
    filenames = [
        "qa.yaml",
        "planning.yaml",
        "chat.yaml",
        "decision.yaml",
        "execution.yaml",
    ]
    loaded: list[Scenario] = []
    for filename in filenames:
        loaded.extend(_load_scenarios_file(scenarios_dir / filename))
    return loaded


def _load_scenarios_file(path: Path) -> list[Scenario]:
    payload = _load_simple_yaml(path)
    if not isinstance(payload, list):
        raise ValueError(f"{path.name} must contain a list of scenarios.")
    return [Scenario.model_validate(item) for item in payload]


def _load_simple_yaml(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON-subset YAML.") from exc
