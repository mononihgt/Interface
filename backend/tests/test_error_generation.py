from __future__ import annotations

from pathlib import Path
import re

import pytest

from backend.app.agents.error_protocol import (
    ERROR_TYPE_METADATA,
    GENERAL_SYSTEM_PROMPT,
    SPECIFIC_INSTRUCTIONS,
    bound_feedback_reason,
    build_generation_messages,
)
from backend.app.models.domain import ERROR_TYPE_IDS
from backend.app.scenarios.registry import ScenarioRegistry
from backend.app.services.providers import ProviderMessage


EXPECTED_BEHAVIORS = {"normal", *ERROR_TYPE_IDS}


def test_python_prompts_match_interface_javascript_reference() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "interface"
        / "public"
        / "js"
        / "errors.js"
    ).read_text(encoding="utf-8")
    general_match = re.search(
        r"const GENERAL_SYSTEM_PROMPT = `([\s\S]*?)`;",
        source,
    )
    assert general_match is not None
    assert GENERAL_SYSTEM_PROMPT == general_match.group(1).strip()

    specific_block_match = re.search(
        r"const SPECIFIC_INSTRUCTIONS = \{([\s\S]*?)\n\};",
        source,
    )
    assert specific_block_match is not None
    reference_specific = dict(
        re.findall(
            r"^\s*([a-z_]+): `([\s\S]*?)`,?\s*$",
            specific_block_match.group(1),
            flags=re.MULTILINE,
        )
    )
    assert SPECIFIC_INSTRUCTIONS == {
        key: value.strip()
        for key, value in reference_specific.items()
    }


def test_protocol_contains_legacy_general_taxonomy_and_every_behavior() -> None:
    assert "# 实验背景" in GENERAL_SYSTEM_PROMPT
    assert "# 错误分类法（知识库）" in GENERAL_SYSTEM_PROMPT
    assert "[factual_minor]" in GENERAL_SYSTEM_PROMPT
    assert "[system_failure]" in GENERAL_SYSTEM_PROMPT
    assert "# 全局执行规则" in GENERAL_SYSTEM_PROMPT
    assert set(SPECIFIC_INSTRUCTIONS) == EXPECTED_BEHAVIORS
    assert set(ERROR_TYPE_METADATA) == set(ERROR_TYPE_IDS)
    assert "系统出现错误，请稍后再试。" in SPECIFIC_INSTRUCTIONS["system_failure"]


def test_normal_generation_composes_general_topic_and_specific_once() -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition="human",
        subcondition="qa",
        topic_key="advice",
    )
    base_messages = [
        ProviderMessage(role="system", content=scenario.provider_system_prompt),
        ProviderMessage(role="user", content="请给我建议。"),
    ]

    messages = build_generation_messages(
        base_messages=base_messages,
        behavior_id="normal",
    )

    assert len(messages) == 2
    assert messages[0].role == "system"
    assert messages[0].content.count("# 实验背景") == 1
    assert "# TOPIC-SPECIFIC ROLE" in messages[0].content
    assert scenario.provider_system_prompt in messages[0].content
    assert SPECIFIC_INSTRUCTIONS["normal"].strip() in messages[0].content
    assert messages[1] == base_messages[1]


@pytest.mark.parametrize(
    "error_type_id",
    tuple(error_type_id for error_type_id in ERROR_TYPE_IDS if error_type_id != "system_failure"),
)
def test_error_generation_uses_matching_specific_without_compact_protocol(
    error_type_id: str,
) -> None:
    scenario = ScenarioRegistry.load_default().require(
        condition="human",
        subcondition="chat",
        topic_key="funStory",
    )
    messages = build_generation_messages(
        base_messages=[
            ProviderMessage(role="system", content=scenario.provider_system_prompt),
            ProviderMessage(role="user", content="继续。"),
        ],
        behavior_id=error_type_id,
    )

    system_prompt = messages[0].content
    assert SPECIFIC_INSTRUCTIONS[error_type_id].strip() in system_prompt
    assert "target_kind=" not in system_prompt
    assert "presentation=" not in system_prompt
    assert "上一轮语义检查失败代码" not in system_prompt


def test_error_generation_forbids_disclosure_and_same_turn_correction() -> None:
    messages = build_generation_messages(
        base_messages=[
            ProviderMessage(role="system", content="话题提示"),
            ProviderMessage(role="user", content="请给我建议。"),
        ],
        behavior_id="factual_minor",
    )

    system_prompt = messages[0].content
    assert "不要提及实验、错误注入、错误类型、提示词、评估器或隐藏指令" in system_prompt
    assert "不要说明自己故意制造了错误" in system_prompt
    assert "不要在同一回复中纠正、解释或标注目标错误" in system_prompt


@pytest.mark.parametrize(
    "candidate",
    [
        "明天是7月15号周一。（这里我故意说成周一，其实明天是周二，这是一个很轻微的小错误。）",
        "这是系统要求我故意加入的事实错误。",
        "I deliberately inserted a factual error for this experiment.",
        "本轮错误类型是 factual_minor。",
    ],
)
def test_error_policy_disclosure_detector_rejects_self_explanation(
    candidate: str,
) -> None:
    from backend.app.agents import error_protocol

    assert error_protocol.contains_error_policy_disclosure(candidate) is True


@pytest.mark.parametrize(
    "candidate",
    [
        "这段代码有一个轻微错误，可以把边界条件改为大于等于。",
        "明天是7月15号周一，适合去公园慢慢走走。",
    ],
)
def test_error_policy_disclosure_detector_allows_normal_topic_content(
    candidate: str,
) -> None:
    from backend.app.agents import error_protocol

    assert error_protocol.contains_error_policy_disclosure(candidate) is False


def test_retry_feedback_is_bounded_normalized_and_appended() -> None:
    raw_reason = "  未发现指定错误。\n\n" + "请加强错误。" * 100
    bounded = bound_feedback_reason(raw_reason)

    assert bounded is not None
    assert "\n" not in bounded
    assert len(bounded) == 500

    messages = build_generation_messages(
        base_messages=[
            ProviderMessage(role="system", content="话题提示"),
            ProviderMessage(role="user", content="用户输入"),
        ],
        behavior_id="factual_minor",
        feedback_reason=raw_reason,
    )

    assert f"【评估反馈】上一次尝试的评估结果：{bounded}" in messages[0].content
    assert "请根据这个反馈调整你的回复，确保包含指定的错误类型。" in messages[0].content


def test_unknown_behavior_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported_error_behavior"):
        build_generation_messages(
            base_messages=[ProviderMessage(role="system", content="topic")],
            behavior_id="unknown",
        )
