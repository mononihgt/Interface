from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any


SYSTEM_FAILURE_TEXT = "系统出现错误，请稍后再试。"
STUB_AGENT_GRAPH_VERSION = "task5-local-stub-v1"
STUB_LLM_PROVIDER = "local_stub"
STUB_LLM_MODEL = "task5-placeholder"
STUB_LLM_ROUTE = "chat"


def timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def from_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def safe_input_metadata(*, user_input_mode: str, user_text: str) -> dict[str, Any]:
    return {
        "input_mode": user_input_mode,
        "user_input_length": len(user_text),
        "user_input_sha256": hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
    }


def scenario_id_for(*, condition: str, subcondition: str, topic_key: str) -> str:
    return f"{condition}_{subcondition}_{topic_key}_v1"


def build_placeholder_turn_record(
    *,
    session_uuid: str,
    turn_index: int,
    condition: str,
    subcondition: str,
    topic_key: str,
    planned_error_turn: int,
    error_type_id: str,
    user_input_mode: str,
    user_text: str,
) -> dict[str, Any]:
    error_planned = turn_index == planned_error_turn
    if error_planned and error_type_id == "system_failure":
        assistant_text = SYSTEM_FAILURE_TEXT
        error_presentation = "system_failure"
    else:
        assistant_text = (
            f"Stub {condition}/{subcondition} response for turn {turn_index}: "
            f"{user_text.strip()}"
        )
        error_presentation = "assistant_text" if error_planned else "none"

    return {
        "assistant_text": assistant_text,
        "response_latency_ms": 0,
        "llm_provider": STUB_LLM_PROVIDER,
        "llm_model": STUB_LLM_MODEL,
        "llm_route": STUB_LLM_ROUTE,
        "llm_attempts_json": to_json(
            [
                {
                    "provider": STUB_LLM_PROVIDER,
                    "model": STUB_LLM_MODEL,
                    "status": "success",
                }
            ]
        ),
        "error_planned": error_planned,
        "error_type_id": error_type_id if error_planned else None,
        "error_presented": error_planned,
        "error_presentation": error_presentation,
        "agent_state_json": to_json(
            {
                "session_id": session_uuid,
                "turn_index": turn_index,
                "condition": condition,
                "subcondition": subcondition,
                "topic_key": topic_key,
                "planned_error_turn": planned_error_turn,
                **safe_input_metadata(
                    user_input_mode=user_input_mode,
                    user_text=user_text,
                ),
            }
        ),
    }
