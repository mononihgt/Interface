from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.models.domain import ERROR_TYPE_IDS, TOPIC_KEYS_BY_CELL
from backend.app.scenarios.registry import ScenarioRegistry


ACTIVE_TOPICS = {
    topic_key
    for topic_keys in TOPIC_KEYS_BY_CELL.values()
    for topic_key in topic_keys
}
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


def test_registry_exposes_exactly_thirteen_explicit_active_topic_contracts():
    registry = ScenarioRegistry.load_default()
    scenarios = registry.list_active()

    assert len(scenarios) == 13
    assert {scenario.topic_key for scenario in scenarios} == ACTIVE_TOPICS
    assert len({scenario.scenario_id for scenario in scenarios}) == 13

    for scenario in scenarios:
        assert registry.require(
            condition=scenario.condition,
            subcondition=scenario.subcondition,
            topic_key=scenario.topic_key,
        ) is scenario


def test_active_topic_contracts_are_loaded_as_explicit_yaml_entries():
    registry = ScenarioRegistry.load_default()

    for scenario in registry.list_active():
        assert scenario.topic_key in scenario.scenario_id


def test_require_is_strict_triple_lookup():
    registry = ScenarioRegistry.load_default()

    with pytest.raises(KeyError, match="Unknown active scenario"):
        registry.require(
            condition="tool",
            subcondition="qa",
            topic_key="factual_lookup",
        )

    with pytest.raises(KeyError, match="Unknown active scenario"):
        registry.require(
            condition="human",
            subcondition="qa",
            topic_key="weather",
        )


@pytest.mark.parametrize(
    ("legacy_key", "canonical_topic"),
    LEGACY_TOPIC_ALIASES.items(),
)
def test_explicit_legacy_resolver_maps_persisted_aliases_only(
    legacy_key: tuple[str, str, str],
    canonical_topic: str,
):
    registry = ScenarioRegistry.load_default()
    condition, subcondition, topic_key = legacy_key

    with pytest.raises(KeyError, match="Unknown active scenario"):
        registry.require(
            condition=condition,
            subcondition=subcondition,
            topic_key=topic_key,
        )

    resolved = registry.resolve_legacy(
        condition=condition,
        subcondition=subcondition,
        topic_key=topic_key,
    )

    assert resolved.topic_key == canonical_topic

def test_each_topic_contract_has_complete_cross_validated_policy():
    registry = ScenarioRegistry.load_default()

    for scenario in registry.list_active():
        assert scenario.required_context
        assert scenario.clarification.missing_context
        assert scenario.clarification.response_goal
        assert set(scenario.tools.allowed).isdisjoint(scenario.tools.forbidden)
        assert scenario.response_policy.max_chars > 0
        assert scenario.response_policy.tone
        assert scenario.presentation.allowed_error_presentations
        assert set(scenario.mutation_policy.rules) == set(ERROR_TYPE_IDS)
        assert scenario.fixtures.normal.user_turns
        assert scenario.fixtures.normal.expected_behavior
        assert scenario.fixtures.clarification.user_turns
        assert scenario.fixtures.clarification.expected_behavior

        artifact = scenario.artifact
        assert (artifact.artifact_type is None) == (artifact.schema_id is None)


@pytest.mark.parametrize(
    ("first_topic", "second_topic", "expected_tools", "expected_artifacts"),
    [
        ("weather", "physics", (["open_meteo"], []), ("weather_card", None)),
        ("travelPlan", "hiking", ([], []), ("plan_card", "plan_card")),
        ("news", "tech", ([], []), (None, None)),
    ],
)
def test_currently_collapsed_topic_pairs_have_distinct_contracts(
    first_topic: str,
    second_topic: str,
    expected_tools: tuple[list[str], list[str]],
    expected_artifacts: tuple[str | None, str | None],
):
    registry = ScenarioRegistry.load_default()
    first_cell = next(cell for cell, topics in TOPIC_KEYS_BY_CELL.items() if first_topic in topics)
    second_cell = next(cell for cell, topics in TOPIC_KEYS_BY_CELL.items() if second_topic in topics)
    first = registry.require(
        condition=first_cell[0],
        subcondition=first_cell[1],
        topic_key=first_topic,
    )
    second = registry.require(
        condition=second_cell[0],
        subcondition=second_cell[1],
        topic_key=second_topic,
    )

    assert first.scenario_id != second.scenario_id
    assert first.system_prompt != second.system_prompt
    assert first.required_context != second.required_context
    assert first.clarification != second.clarification
    assert (first.tools.allowed, second.tools.allowed) == expected_tools
    assert (first.artifact_type, second.artifact_type) == expected_artifacts


def test_weather_and_physics_have_opposite_weather_tool_and_artifact_contracts():
    registry = ScenarioRegistry.load_default()
    weather = registry.require(condition="tool", subcondition="qa", topic_key="weather")
    physics = registry.require(condition="tool", subcondition="qa", topic_key="physics")

    assert weather.tools.allowed == ["open_meteo"]
    assert weather.artifact.artifact_type == "weather_card"
    assert "open_meteo" in physics.tools.forbidden
    assert physics.artifact.artifact_type is None


@pytest.mark.parametrize(
    ("condition", "topic_key"),
    [
        ("tool", "valueDecision"),
        ("human", "preferenceDecision"),
    ],
)
def test_decision_scenarios_are_pure_text(
    condition: str,
    topic_key: str,
) -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition=condition,
        subcondition="decision",
        topic_key=topic_key,
    )

    assert scenario.artifact_type is None
    assert scenario.artifact.participant_visible is False
    assert set(scenario.error_policy.allowed_presentations) == {
        "assistant_text",
        "system_failure",
    }
    assert all(
        rule.presentation == "assistant_text"
        for error_type_id, rule in scenario.mutation_policy.rules.items()
        if error_type_id != "system_failure"
    )


def test_registry_rejects_duplicate_scenario_ids(tmp_path: Path):
    path = tmp_path / "duplicate.yaml"
    source = ScenarioRegistry.load_default().list_active()[0].model_dump(mode="json")
    duplicate = dict(source)
    path.write_text(json.dumps([source, duplicate], ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate scenario_id"):
        ScenarioRegistry.load([path])
