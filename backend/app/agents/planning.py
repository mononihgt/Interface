from __future__ import annotations

from backend.app.agents.graph_base import ControlledGraph, ExperimentGraphState
from backend.app.services.providers import ProviderResponse


def build_planning_graph(*, provider_runner, evaluator_runner=None):
    return ControlledGraph(
        graph_name="planning_graph_v1",
        provider_runner=provider_runner,
        artifact_builder=_planning_artifact_builder,
        evaluator_runner=evaluator_runner,
    )


def _planning_artifact_builder(
    state: ExperimentGraphState,
    provider_response: ProviderResponse,
):
    return (
        "plan_card",
        {
            "title": state.canonical_topic_key or state.topic_key,
            "summary": provider_response.text,
        },
    )
