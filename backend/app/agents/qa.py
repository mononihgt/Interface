from __future__ import annotations

from backend.app.agents.graph_base import ControlledGraph, ExperimentGraphState
from backend.app.services.providers import ProviderResponse


def build_qa_graph(*, provider_runner, evaluator_runner=None):
    return ControlledGraph(
        graph_name="qa_graph_v1",
        provider_runner=provider_runner,
        artifact_builder=_qa_artifact_builder,
        evaluator_runner=evaluator_runner,
    )


def _qa_artifact_builder(
    state: ExperimentGraphState,
    provider_response: ProviderResponse,
):
    del provider_response
    if (state.canonical_topic_key or state.topic_key) == "weather":
        weather_tool = state.weather_tool or {}
        participant_card = weather_tool.get("participant_card")
        if weather_tool.get("status") == "success" and isinstance(
            participant_card,
            dict,
        ):
            return "weather_card", dict(participant_card)
    return None, None
