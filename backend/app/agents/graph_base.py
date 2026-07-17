from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Tuple, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, ConfigDict, Field

from backend.app.scenarios.registry import ErrorPolicy, Scenario
from backend.app.agents.structured import (
    ERROR_SEVERITY_BY_TYPE,
    ErrorMutation,
    STRUCTURED_ARTIFACT_FALLBACK_TEXT,
    StructuredAgentResult,
    normalize_semantic_failure_code,
)
from backend.app.services.providers import ProviderResponse
from backend.app.services.records import SYSTEM_FAILURE_TEXT, to_json


class ConversationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["system", "user", "assistant"]
    text: str


class GraphInput(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    user_text: str = Field(min_length=1)
    input_mode: Literal["voice", "text_test_only"]
    frontend_assignment_override: Optional[dict[str, str]] = None


class ExperimentGraphState(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str
    participant_id: int
    turn_index: int
    condition: Literal["human", "tool"]
    subcondition: Literal["qa", "planning", "chat", "decision", "execution"]
    topic_key: str
    canonical_topic_key: Optional[str] = None
    scenario_id: str
    error_type_id: Optional[str] = None
    planned_error_turn: Optional[int] = None
    user_input: str
    input_mode: Literal["voice", "text_test_only"]
    recent_history: list[ConversationMessage] = Field(default_factory=list)
    should_inject_error: bool = False
    scenario: Scenario
    error_policy: Optional[ErrorPolicy] = None
    error_presentation: Literal["assistant_text", "simulated_ui", "system_failure", "none"] = "none"
    parsed_task: dict[str, Any] = Field(default_factory=dict)
    weather_tool: Optional[dict[str, Any]] = None
    assistant_text: str = ""
    artifact_type: Optional[str] = None
    artifact_payload: Optional[dict[str, Any]] = None
    error_presented: bool = False
    error_agent_result: Optional[ErrorMutation] = None
    error_mutation: Optional[ErrorMutation] = None
    llm_attempts: list[dict[str, Any]] = Field(default_factory=list)
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_route: Optional[str] = None
    provider_status: Optional[str] = None
    artifact_validation_status: Literal["not_requested", "valid", "invalid"] = "not_requested"
    artifact_validation_error: Optional[str] = None
    evaluator_result: Optional[dict[str, Any]] = None
    error_evaluator_provider: Optional[str] = None
    error_evaluator_model: Optional[str] = None
    graph_version: str


@dataclass
class GraphRunResult:
    state: ExperimentGraphState
    client_response: dict[str, Any]
    turn_record: dict[str, Any]


class GraphState(TypedDict, total=False):
    session_id: str
    participant_id: int
    turn_index: int
    condition: str
    subcondition: str
    topic_key: str
    canonical_topic_key: Optional[str]
    scenario_id: str
    error_type_id: Optional[str]
    planned_error_turn: Optional[int]
    user_input: str
    input_mode: str
    recent_history: list[dict[str, Any]]
    should_inject_error: bool
    scenario: dict[str, Any]
    error_policy: Optional[dict[str, Any]]
    error_presentation: str
    parsed_task: dict[str, Any]
    weather_tool: Optional[dict[str, Any]]
    assistant_text: str
    artifact_type: Optional[str]
    artifact_payload: Optional[dict[str, Any]]
    error_presented: bool
    error_agent_result: Optional[dict[str, Any]]
    error_mutation: Optional[dict[str, Any]]
    llm_attempts: list[dict[str, Any]]
    llm_provider: Optional[str]
    llm_model: Optional[str]
    llm_route: Optional[str]
    provider_status: Optional[str]
    artifact_validation_status: str
    artifact_validation_error: Optional[str]
    evaluator_result: Optional[dict[str, Any]]
    error_evaluator_provider: Optional[str]
    error_evaluator_model: Optional[str]
    graph_version: str
    provider_response: ProviderResponse
    provider_result: Any
    turn_record: dict[str, Any]
    client_response: dict[str, Any]


ProviderRunner = Callable[
    [ExperimentGraphState],
    ProviderResponse | StructuredAgentResult[BaseModel],
]
ArtifactBuilder = Callable[
    [ExperimentGraphState, ProviderResponse | StructuredAgentResult[BaseModel]],
    Tuple[Optional[str], Optional[dict[str, Any]]],
]
EvaluatorRunner = Callable[
    [ExperimentGraphState, str, Optional[str], Optional[dict[str, Any]]],
    Optional[dict[str, Any]],
]


def build_graph_state(
    *,
    session_row: dict[str, Any],
    turn_index: int,
    graph_input: GraphInput,
    recent_history: list[ConversationMessage],
    scenario: Scenario,
    graph_version: str,
    weather_tool: dict[str, Any] | None = None,
) -> ExperimentGraphState:
    return ExperimentGraphState(
        session_id=str(session_row["session_uuid"]),
        participant_id=int(session_row["participant_id"]),
        turn_index=turn_index,
        condition=str(session_row["condition"]),
        subcondition=str(session_row["subcondition"]),
        topic_key=str(session_row["topic_key"]),
        canonical_topic_key=scenario.topic_key,
        scenario_id=str(session_row["scenario_id"]),
        error_type_id=(
            str(session_row["error_type_id"])
            if session_row.get("error_type_id") is not None
            else None
        ),
        planned_error_turn=(
            int(session_row["planned_error_turn"])
            if session_row.get("planned_error_turn") is not None
            else None
        ),
        user_input=graph_input.user_text,
        input_mode=graph_input.input_mode,
        recent_history=recent_history,
        scenario=scenario,
        error_policy=scenario.error_policy,
        weather_tool=weather_tool,
        graph_version=graph_version,
    )


class ControlledGraph:
    def __init__(
        self,
        *,
        graph_name: str,
        provider_runner: ProviderRunner,
        artifact_builder: Optional[ArtifactBuilder] = None,
        evaluator_runner: Optional[EvaluatorRunner] = None,
        requires_structured_output: bool = False,
    ) -> None:
        self._graph_name = graph_name
        self._provider_runner = provider_runner
        self._artifact_builder = artifact_builder or _no_artifact
        self._evaluator_runner = evaluator_runner
        self._requires_structured_output = requires_structured_output
        self.compiled_graph = self._build_graph()

    def run(self, state: ExperimentGraphState) -> GraphRunResult:
        graph_state = self.compiled_graph.invoke(_state_to_graph(state))
        final_state = _graph_to_model(graph_state)
        return GraphRunResult(
            state=final_state,
            client_response=dict(graph_state["client_response"]),
            turn_record=dict(graph_state["turn_record"]),
        )

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(GraphState)
        graph.add_node("load_session_state", self._load_session_state)
        graph.add_node("validate_turn", self._validate_turn)
        graph.add_node("normalize_input", self._normalize_input)
        graph.add_node("build_context", self._build_context)
        graph.add_node("select_scenario_policy", self._select_scenario_policy)
        graph.add_node("determine_error", self._determine_error)
        graph.add_node("run_scenario_task", self._run_scenario_task)
        graph.add_node("generate_message", self._generate_message)
        graph.add_node("inject_error", self._inject_error)
        graph.add_node("evaluate_error", self._evaluate_error)
        graph.add_node("persist_turn_draft", self._persist_turn_draft)
        graph.add_node("build_client_response", self._build_client_response)
        graph.add_edge(START, "load_session_state")
        graph.add_edge("load_session_state", "validate_turn")
        graph.add_edge("validate_turn", "normalize_input")
        graph.add_edge("normalize_input", "build_context")
        graph.add_edge("build_context", "select_scenario_policy")
        graph.add_edge("select_scenario_policy", "determine_error")
        graph.add_edge("determine_error", "run_scenario_task")
        graph.add_edge("run_scenario_task", "generate_message")
        graph.add_edge("generate_message", "inject_error")
        graph.add_edge("inject_error", "evaluate_error")
        graph.add_edge("evaluate_error", "persist_turn_draft")
        graph.add_edge("persist_turn_draft", "build_client_response")
        graph.add_edge("build_client_response", END)
        return graph.compile()

    def _load_session_state(self, state: GraphState) -> GraphState:
        return {
            "parsed_task": dict(state.get("parsed_task", {})),
        }

    def _validate_turn(self, state: GraphState) -> GraphState:
        if state["turn_index"] < 1:
            raise ValueError("turn_index must be positive.")
        if not state["user_input"].strip():
            raise ValueError("user_input must not be empty.")
        return {}

    def _normalize_input(self, state: GraphState) -> GraphState:
        return {"user_input": state["user_input"].strip()}

    def _build_context(self, state: GraphState) -> GraphState:
        return {
            "parsed_task": {
                "history_turns": len(state["recent_history"]),
                "topic_key": state["topic_key"],
                "canonical_topic_key": state.get("canonical_topic_key"),
            }
        }

    def _select_scenario_policy(self, state: GraphState) -> GraphState:
        if state.get("scenario") is None:
            raise ValueError("scenario is required.")
        return {}

    def _determine_error(self, state: GraphState) -> GraphState:
        return {
            "should_inject_error": (
                state.get("planned_error_turn") is not None
                and state["turn_index"] == state["planned_error_turn"]
                and state.get("error_type_id") not in (None, "system_failure")
            )
        }

    def _run_scenario_task(self, state: GraphState) -> GraphState:
        if (
            state.get("planned_error_turn") == state["turn_index"]
            and state.get("error_type_id") == "system_failure"
        ):
            provider_response = ProviderResponse(
                text=SYSTEM_FAILURE_TEXT,
                provider="local-system",
                model="planned-system-failure-v1",
                route="system_failure",
                attempts=[],
                used_local_fallback=False,
            )
        else:
            provider_result = self._provider_runner(_graph_to_model(state))
            provider_response = (
                provider_result.response
                if isinstance(provider_result, StructuredAgentResult)
                else provider_result
            )
            return {
                "provider_result": provider_result,
                "provider_response": provider_response,
            }
        return {
            "provider_result": provider_response,
            "provider_response": provider_response,
        }

    def _generate_message(self, state: GraphState) -> GraphState:
        provider_response = state["provider_response"]
        llm_attempts = [
            {
                "route": attempt.route,
                "provider": attempt.provider,
                "model": attempt.model,
                "status": attempt.status,
                "latency_ms": attempt.latency_ms,
                "http_status": attempt.http_status,
                "cooldown_applied": attempt.cooldown_applied,
            }
            for attempt in provider_response.attempts
        ]
        provider_status = (
            provider_response.attempts[-1].status
            if provider_response.attempts
            else ("system_failure" if provider_response.route == "system_failure" else "success")
        )
        artifact_type = None
        artifact_payload = None
        assistant_text = provider_response.text
        validation_status = "not_requested"
        validation_error = None
        provider_result = state.get("provider_result", provider_response)
        if self._requires_structured_output and provider_response.route != "system_failure":
            validation_status = "invalid"
            if not isinstance(provider_result, StructuredAgentResult):
                validation_error = "structured_result_required"
            elif provider_result.value is None:
                validation_error = provider_result.validation_error or "schema_validation_error"
            else:
                validation_status = "valid"
                assistant_text = provider_result.value.assistant_text
                artifact_type, artifact_payload = self._artifact_builder(
                    _graph_to_model(state),
                    provider_result,
                )
            if validation_status == "invalid":
                assistant_text = (
                    provider_response.text
                    if provider_response.used_local_fallback
                    or state.get("should_inject_error")
                    else STRUCTURED_ARTIFACT_FALLBACK_TEXT
                )
        elif provider_response.route != "system_failure":
            artifact_type, artifact_payload = self._artifact_builder(
                _graph_to_model(state),
                provider_response,
            )

        return {
            "assistant_text": assistant_text,
            "llm_attempts": llm_attempts,
            "llm_provider": provider_response.provider,
            "llm_model": provider_response.model,
            "llm_route": provider_response.route,
            "provider_status": provider_status,
            "artifact_validation_status": validation_status,
            "artifact_validation_error": validation_error,
            "artifact_type": artifact_type,
            "artifact_payload": artifact_payload,
        }

    def _inject_error(self, state: GraphState) -> GraphState:
        from backend.app.agents.error_injection import (
            generation_fallback_prevents_error_presentation,
        )

        provider_response = state["provider_response"]
        if provider_response.route == "system_failure":
            graph_state = _graph_to_model(state)
            error_type_id = "system_failure"
            rule = graph_state.scenario.mutation_policy.rules[error_type_id]
            evidence = ErrorMutation(
                error_type_id=error_type_id,
                severity=ERROR_SEVERITY_BY_TYPE[error_type_id],
                presentation=rule.presentation,
                target_kind=rule.target_kind,
                target_path="assistant_text",
                original_value="provider_not_called",
                mutated_value=SYSTEM_FAILURE_TEXT,
                applied=True,
                centrality=rule.centrality,
                operation="planned_system_failure",
                magnitude="service_unavailable",
            )
            return {
                "assistant_text": SYSTEM_FAILURE_TEXT,
                "artifact_type": None,
                "artifact_payload": None,
                "error_presentation": "system_failure",
                "error_presented": True,
                "error_mutation": evidence.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                ),
            }
        if generation_fallback_prevents_error_presentation(
            provider_response=provider_response
        ):
            return {
                "assistant_text": provider_response.text,
                "error_presentation": "none",
                "error_presented": False,
            }
        if state.get("artifact_validation_status") == "invalid":
            return {
                "error_presentation": "none",
                "error_presented": False,
            }

        if state.get("should_inject_error"):
            error_agent_result = state.get("error_agent_result")
            if error_agent_result is None:
                return {
                    "error_presentation": "none",
                    "error_presented": False,
                }
            evidence = ErrorMutation.model_validate(error_agent_result)
            if not evidence.applied:
                return {
                    "error_presentation": "none",
                    "error_presented": False,
                    "error_mutation": evidence.model_dump(
                        mode="json",
                        by_alias=True,
                    ),
                }
            return {
                "assistant_text": state["assistant_text"],
                "artifact_type": state.get("artifact_type"),
                "artifact_payload": state.get("artifact_payload"),
                "error_presentation": evidence.presentation,
                "error_presented": True,
                "error_mutation": evidence.model_dump(
                    mode="json",
                    by_alias=True,
                ),
            }

        return {
            "error_presentation": "none",
            "error_presented": False,
        }

    def _evaluate_error(self, state: GraphState) -> GraphState:
        from backend.app.agents.error_injection import (
            generation_fallback_prevents_error_presentation,
        )

        error_planned = state.get("planned_error_turn") == state["turn_index"]
        if error_planned and generation_fallback_prevents_error_presentation(
            provider_status=state.get("provider_status"),
            provider_name=state.get("llm_provider"),
            provider_route=state.get("llm_route"),
        ):
            result = {
                "status": "failed",
                "presented": False,
                "provider": state.get("llm_provider"),
                "model": state.get("llm_model"),
                "route": state.get("llm_route") or self._graph_name,
                "reason": "generation_local_fallback",
            }
            return {
                "error_presented": False,
                "evaluator_result": result,
                "error_evaluator_provider": result.get("provider"),
                "error_evaluator_model": result.get("model"),
            }
        if not error_planned or state.get("error_presentation") in {"none", "system_failure"}:
            return {}
        if self._evaluator_runner is None:
            return {"error_presented": False}

        result = self._evaluator_runner(
            _graph_to_model(state),
            state["assistant_text"],
            state.get("artifact_type"),
            state.get("artifact_payload"),
        )
        if result is None:
            return {"error_presented": False}
        result = dict(result)
        result["reason"] = normalize_semantic_failure_code(
            result.get("reason"),
            default=(
                "evaluator_not_presented"
                if not bool(result.get("presented"))
                else "evaluator_presented"
            ),
        )
        return {
            "error_presented": bool(result.get("presented")),
            "evaluator_result": result,
            "error_evaluator_provider": result.get("provider"),
            "error_evaluator_model": result.get("model"),
        }

    def _persist_turn_draft(self, state: GraphState) -> GraphState:
        turn_record = {
            "assistant_text": state["assistant_text"],
            "response_latency_ms": next(
                (
                    attempt["latency_ms"]
                    for attempt in reversed(state["llm_attempts"])
                    if attempt["status"] == "success" and attempt["latency_ms"] is not None
                ),
                0,
            ),
            "llm_provider": state.get("llm_provider"),
            "llm_model": state.get("llm_model"),
            "llm_route": state.get("llm_route") or self._graph_name,
            "llm_attempts_json": to_json(
                [
                    {
                        "route": attempt["route"],
                        "provider": attempt["provider"],
                        "model": attempt["model"],
                        "status": attempt["status"],
                        "http_status": attempt["http_status"],
                        "cooldown_applied": attempt["cooldown_applied"],
                    }
                    for attempt in state["llm_attempts"]
                ]
            ),
            "error_planned": state.get("planned_error_turn") == state["turn_index"],
            "error_type_id": (
                state.get("error_type_id")
                if state.get("planned_error_turn") == state["turn_index"]
                else None
            ),
            "error_presented": state["error_presented"],
            "error_presentation": state["error_presentation"],
            "error_evaluator_provider": state.get("error_evaluator_provider"),
            "error_evaluator_model": state.get("error_evaluator_model"),
            "error_evaluator_result_json": (
                to_json(state["evaluator_result"])
                if state.get("evaluator_result") is not None
                else None
            ),
            "agent_state_json": to_json(
                {
                    "session_id": state["session_id"],
                    "turn_index": state["turn_index"],
                    "condition": state["condition"],
                    "subcondition": state["subcondition"],
                    "topic_key": state["topic_key"],
                    "canonical_topic_key": state.get("canonical_topic_key"),
                    "scenario_id": state["scenario_id"],
                    "graph_version": state["graph_version"],
                    "input_mode": state["input_mode"],
                    "provider_route": state.get("llm_route"),
                    "provider_name": state.get("llm_provider"),
                    "provider_model": state.get("llm_model"),
                    "provider_status": state.get("provider_status"),
                    "artifact_validation_status": state.get("artifact_validation_status"),
                    "artifact_validation_error": state.get("artifact_validation_error"),
                    "error_type_id": state.get("error_type_id"),
                    "error_presented": state.get("error_presented"),
                    "error_presentation": state["error_presentation"],
                    "allowed_error_presentations": (
                        state["error_policy"]["allowed_presentations"]
                        if state.get("error_policy") is not None
                        else []
                    ),
                    "artifact_type": state.get("artifact_type"),
                    "weather_tool": state.get("weather_tool"),
                }
            ),
        }
        return {"turn_record": turn_record}

    def _build_client_response(self, state: GraphState) -> GraphState:
        return {
            "client_response": {
                "assistant_text": state["assistant_text"],
                "artifact_type": state.get("artifact_type"),
                "artifact_payload": state.get("artifact_payload"),
                "error_presentation": state["error_presentation"],
            }
        }


def _no_artifact(
    state: ExperimentGraphState,
    provider_response: ProviderResponse,
) -> Tuple[Optional[str], Optional[dict[str, Any]]]:
    del state, provider_response
    return None, None


def _state_to_graph(state: ExperimentGraphState) -> GraphState:
    payload = state.model_dump(mode="python")
    payload["recent_history"] = [message.model_dump(mode="python") for message in state.recent_history]
    payload["error_policy"] = (
        state.error_policy.model_dump(mode="python") if state.error_policy is not None else None
    )
    payload["scenario"] = state.scenario.model_dump(mode="python")
    return payload


def _graph_to_model(state: GraphState) -> ExperimentGraphState:
    payload = {
        field_name: state.get(field_name)
        for field_name in ExperimentGraphState.model_fields
    }
    payload["recent_history"] = [
        ConversationMessage.model_validate(message)
        for message in state.get("recent_history", [])
    ]
    payload["error_policy"] = (
        ErrorPolicy.model_validate(state["error_policy"])
        if state.get("error_policy") is not None
        else None
    )
    payload["scenario"] = Scenario.model_validate(state["scenario"])
    return ExperimentGraphState.model_validate(payload)
