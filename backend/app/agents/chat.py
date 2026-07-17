from __future__ import annotations

from backend.app.agents.graph_base import ControlledGraph


def build_chat_graph(*, provider_runner, evaluator_runner=None):
    return ControlledGraph(
        graph_name="chat_graph_v1",
        provider_runner=provider_runner,
        evaluator_runner=evaluator_runner,
    )
