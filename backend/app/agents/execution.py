from __future__ import annotations

from typing import Any

from backend.app.agents.graph_base import ControlledGraph, ExperimentGraphState
from backend.app.agents.structured import (
    CopyVersionsArtifact,
    ScheduleArtifact,
    StructuredAgentResult,
)
from backend.app.services.providers import ProviderResponse


def build_execution_graph(*, provider_runner, evaluator_runner=None):
    return ControlledGraph(
        graph_name="execution_graph_v1",
        provider_runner=provider_runner,
        artifact_builder=_execution_artifact_builder,
        evaluator_runner=evaluator_runner,
        requires_structured_output=True,
    )


def _execution_artifact_builder(
    state: ExperimentGraphState,
    provider_result: ProviderResponse | StructuredAgentResult,
) -> tuple[str | None, dict[str, Any] | None]:
    if not isinstance(provider_result, StructuredAgentResult) or provider_result.value is None:
        return None, None
    artifact = provider_result.value
    if getattr(artifact, "status", None) != "completed":
        return None, None
    if state.condition == "tool" and isinstance(artifact, ScheduleArtifact):
        payload = artifact.model_dump(
            mode="json",
            by_alias=True,
            exclude={"assistant_text"},
        )
        payload["rows"] = [
            {
                **row.model_dump(mode="json"),
                "日期": row.date,
                "时间": row.time,
                "地点": row.location,
                "任务": row.task,
                "备注": row.note,
            }
            for row in artifact.rows
        ]
        return "table", payload
    if state.condition == "human" and isinstance(artifact, CopyVersionsArtifact):
        payload = artifact.model_dump(
            mode="json",
            by_alias=True,
            exclude={"assistant_text"},
        )
        payload["versions"] = [candidate.model_dump(mode="json") for candidate in artifact.candidates]
        return "copy_versions", payload
    return None, None
