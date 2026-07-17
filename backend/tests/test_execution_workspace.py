from __future__ import annotations

import json

import pytest

from backend.app.agents.structured import (
    CopyVersionsArtifact,
    ScheduleArtifact,
    StructuredAgentResult,
    parse_structured_output,
)
from backend.app.services.providers import (
    ProviderAttempt,
    ProviderMessage,
    ProviderResponse,
)


def test_schedule_payload_normalizes_columns_aliases_and_time() -> None:
    from backend.app.agents.execution_workspace import normalize_execution_payload

    payload = normalize_execution_payload(
        {
            "assistant_text": "已经整理好了。",
            "status": "completed",
            "columns": ["时间", "事件", "位置"],
            "rows": [
                {
                    "日期": "明天",
                    "时间": "下午3点",
                    "地点": "会议室",
                    "任务": "项目复盘会",
                    "备注": "持续1小时",
                }
            ],
        },
        schema=ScheduleArtifact,
        user_text="请帮我建立日程：明天下午3点在会议室召开项目复盘会，持续1小时。",
        current_artifact=None,
        action_mode="create",
    )

    artifact = ScheduleArtifact.model_validate(payload)

    assert artifact.columns == ["日期", "时间", "地点", "任务", "备注"]
    assert artifact.rows[0].model_dump() == {
        "date": "明天",
        "time": "15:00",
        "location": "会议室",
        "task": "项目复盘会",
        "note": "持续1小时",
    }


def test_copy_payload_normalizes_plain_candidates() -> None:
    from backend.app.agents.execution_workspace import normalize_execution_payload

    payload = normalize_execution_payload(
        {
            "assistant_text": "给你两个版本。",
            "status": "completed",
            "label": "道歉消息",
            "candidates": [
                "抱歉，我临时有事没能赴约，希望能重新约个时间。",
                "真的不好意思，今天临时有事爽约了，改天我认真补上。",
            ],
        },
        schema=CopyVersionsArtifact,
        user_text="我临时有事爽约了朋友，想写一条真诚但不太沉重的道歉消息。",
        current_artifact=None,
        action_mode="create",
    )

    artifact = CopyVersionsArtifact.model_validate(payload)

    assert [candidate.id for candidate in artifact.candidates] == ["v1", "v2"]
    assert artifact.recommended_index == 0
    assert artifact.selected_version is not None
    assert artifact.selected_version.version_id == "v1"
    assert artifact.revision_notes


def test_copy_payload_aligns_selected_version_with_recommendation() -> None:
    from backend.app.agents.execution_workspace import normalize_execution_payload

    payload = normalize_execution_payload(
        {
            "assistant_text": "给你两个版本。",
            "status": "completed",
            "label": "通知",
            "candidates": [
                {"id": "v1", "label": "自然版", "text": "今晚会晚到十分钟，请大家先开始。"},
                {"id": "v2", "label": "礼貌版", "text": "不好意思，今晚会晚到十分钟，辛苦大家先开始。"},
            ],
            "recommendedIndex": 1,
            "selected_version": {"version_id": "missing", "reason": "模型引用了不存在的版本。"},
            "revision_notes": ["语气更礼貌。"],
        },
        schema=CopyVersionsArtifact,
        user_text="把通知改得更礼貌",
        current_artifact=None,
        action_mode="create",
    )

    artifact = CopyVersionsArtifact.model_validate(payload)

    assert artifact.selected_version is not None
    assert artifact.selected_version.version_id == "v2"


def test_local_schedule_artifact_uses_explicit_input_material() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text="请帮我建立日程：明天下午3点在会议室召开项目复盘会，持续1小时。",
        current_artifact=None,
    )

    assert isinstance(artifact, ScheduleArtifact)
    assert artifact.action_mode == "create"
    assert artifact.rows[0].model_dump() == {
        "date": "明天",
        "time": "15:00",
        "location": "会议室",
        "task": "召开项目复盘会",
        "note": "持续1小时",
    }


def test_local_schedule_artifact_requires_time_and_task() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text="帮我整理成表格。",
        current_artifact=None,
    )

    assert artifact is None


def test_local_schedule_revision_preserves_unrelated_rows() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    current = {
        "actionType": "schedule_table",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": "原始安排",
        "columns": ["日期", "时间", "地点", "任务", "备注"],
        "rows": [
            {
                "date": "明天",
                "time": "09:00",
                "location": "办公室",
                "task": "交材料",
                "note": "",
            },
            {
                "date": "明天",
                "time": "15:00",
                "location": "会议室",
                "task": "项目复盘会",
                "note": "持续1小时",
            },
        ],
    }

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text="把项目复盘会的时间改到下午4点。",
        current_artifact=current,
    )

    assert isinstance(artifact, ScheduleArtifact)
    assert artifact.action_mode == "revise"
    assert [(row.task, row.time) for row in artifact.rows] == [
        ("交材料", "09:00"),
        ("项目复盘会", "16:00"),
    ]


def test_local_copy_artifact_uses_explicit_source_material() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="human",
        user_text="我临时有事爽约了朋友，想写一条真诚但不太沉重的道歉消息。",
        current_artifact=None,
    )

    assert isinstance(artifact, CopyVersionsArtifact)
    assert len(artifact.candidates) == 3
    assert all("爽约" in candidate.text for candidate in artifact.candidates)


@pytest.mark.parametrize(
    ("user_text", "rejected_text", "replacement_text"),
    [
        ("并非长江。", "长江", None),
        ("不是长江，是黄河，请修改。", "长江", "黄河"),
    ],
)
def test_local_copy_revision_applies_user_fact_correction(
    user_text: str,
    rejected_text: str,
    replacement_text: str | None,
) -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="human",
        user_text=user_text,
        current_artifact=current_copy_artifact(),
    )

    assert isinstance(artifact, CopyVersionsArtifact)
    assert artifact.status == "completed"
    assert artifact.action_mode == "revise"
    participant_artifact = artifact.model_dump(
        mode="json",
        by_alias=True,
        exclude={"assistant_text"},
    )
    assert rejected_text not in json.dumps(participant_artifact, ensure_ascii=False)
    if replacement_text is not None:
        assert replacement_text in artifact.requested_source
        assert all(replacement_text in candidate.text for candidate in artifact.candidates)


def test_local_copy_revision_asks_for_clarification_when_no_safe_change_exists() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="human",
        user_text="请再调整一下。",
        current_artifact=current_copy_artifact(),
    )

    assert isinstance(artifact, CopyVersionsArtifact)
    assert artifact.status == "pending"
    assert artifact.action_mode == "clarify"
    assert "具体" in artifact.assistant_text


@pytest.mark.parametrize(
    ("user_text", "expected"),
    [
        ("不是明天，是后天。", {"date": "后天"}),
        ("任务不是项目会，是培训。", {"task": "培训"}),
        ("备注改成带材料。", {"note": "带材料"}),
        ("不是长江边。", {"location": "未注明"}),
    ],
)
def test_local_schedule_revision_updates_supported_fields(
    user_text: str,
    expected: dict[str, str],
) -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text=user_text,
        current_artifact=current_schedule_artifact(),
    )

    assert isinstance(artifact, ScheduleArtifact)
    assert artifact.status == "completed"
    row = artifact.rows[0].model_dump()
    assert row | expected == row


def test_local_schedule_revision_deletes_target_and_preserves_unrelated_rows() -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text="删除项目会。",
        current_artifact=current_schedule_artifact(include_unrelated=True),
    )

    assert isinstance(artifact, ScheduleArtifact)
    assert artifact.status == "completed"
    assert [row.task for row in artifact.rows] == ["交材料"]


@pytest.mark.parametrize(
    ("user_text", "field", "expected"),
    [
        ("把项目会的日期改成后天。", "date", "后天"),
        ("把项目会的备注改成改为线上参加。", "note", "改为线上参加"),
    ],
)
def test_local_schedule_revision_targets_named_row_for_direct_field_change(
    user_text: str,
    field: str,
    expected: str,
) -> None:
    from backend.app.agents.execution_workspace import build_local_execution_artifact

    artifact = build_local_execution_artifact(
        condition="tool",
        user_text=user_text,
        current_artifact=current_schedule_artifact(include_unrelated=True),
    )

    assert isinstance(artifact, ScheduleArtifact)
    rows_by_task = {row.task: row.model_dump() for row in artifact.rows}
    assert rows_by_task["项目会"][field] == expected
    assert rows_by_task["交材料"][field] != expected


def test_schedule_revision_normalization_preserves_unrelated_current_rows() -> None:
    from backend.app.agents.execution_workspace import normalize_execution_payload

    payload = normalize_execution_payload(
        {
            "assistant_text": "已修改。",
            "actionType": "schedule_table",
            "actionMode": "revise",
            "status": "completed",
            "requestedSource": "把项目会改到下午4点",
            "rows": [
                {
                    "date": "明天",
                    "time": "16:00",
                    "location": "长江边",
                    "task": "项目会",
                    "note": "持续1小时",
                }
            ],
        },
        schema=ScheduleArtifact,
        user_text="把项目会的时间改到下午4点。",
        current_artifact=current_schedule_artifact(include_unrelated=True),
        action_mode="revise",
    )

    artifact = ScheduleArtifact.model_validate(payload)

    assert [(row.task, row.time) for row in artifact.rows] == [
        ("交材料", "09:00"),
        ("项目会", "16:00"),
    ]


def test_workspace_messages_keep_experiment_prompt_and_add_artifact_context() -> None:
    from backend.app.agents.execution_workspace import build_execution_workspace_messages

    base_messages = [
        ProviderMessage(role="system", content="general + topic + normal prompt"),
        ProviderMessage(role="user", content="明天下午3点开会"),
    ]
    local_artifact = ScheduleArtifact.model_validate(
        {
            "assistant_text": "已整理。",
            "status": "completed",
            "requestedSource": "明天下午3点开会",
            "rows": [
                {
                    "date": "明天",
                    "time": "15:00",
                    "location": "未注明",
                    "task": "开会",
                    "note": "",
                }
            ],
        }
    )

    messages = build_execution_workspace_messages(
        base_messages=base_messages,
        schema=ScheduleArtifact,
        user_text="明天下午3点开会",
        current_artifact=None,
        local_artifact=local_artifact,
    )

    assert messages[0].role == "system"
    assert messages[0].content.startswith(base_messages[0].content)
    assert messages[-1] == base_messages[-1]
    workspace_instruction = messages[0].content
    assert "右侧执行工作区" in workspace_instruction
    assert "不要生成聊天区之外的解释" in workspace_instruction
    assert json.dumps(["日期", "时间", "地点", "任务", "备注"], ensure_ascii=False) in workspace_instruction
    assert "15:00" in workspace_instruction


def test_workspace_messages_target_planned_error_to_simulated_ui() -> None:
    from backend.app.agents.execution_workspace import build_execution_workspace_messages

    messages = build_execution_workspace_messages(
        base_messages=[
            ProviderMessage(role="system", content="factual_major error prompt"),
            ProviderMessage(role="user", content="明天下午3点开会"),
        ],
        schema=ScheduleArtifact,
        user_text="明天下午3点开会",
        current_artifact=None,
        local_artifact=build_reported_schedule_artifact(),
        error_type_id="factual_major",
        error_presentation="simulated_ui",
        target_kind="schedule_core_field",
    )

    prompt = messages[0].content
    assert "simulated_ui" in prompt
    assert "schedule_core_field" in prompt
    assert "错误必须出现在右侧 artifact" in prompt
    assert "assistant_text 保持正常" in prompt


def test_structured_parser_normalizes_before_strict_validation() -> None:
    from backend.app.agents.execution_workspace import normalize_execution_payload

    raw_output = json.dumps(
        {
            "assistant_text": "已整理。",
            "status": "completed",
            "columns": ["时间", "事项", "位置"],
            "rows": [
                {
                    "日期": "明天",
                    "时间": "下午3点",
                    "地点": "会议室",
                    "任务": "项目复盘会",
                    "备注": "持续1小时",
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = parse_structured_output(
        raw_output,
        ScheduleArtifact,
        payload_normalizer=lambda payload: normalize_execution_payload(
            payload,
            schema=ScheduleArtifact,
            user_text="明天下午3点开项目复盘会",
            current_artifact=None,
            action_mode="create",
        ),
    )

    assert parsed.validation_error is None
    assert isinstance(parsed.value, ScheduleArtifact)
    assert parsed.value.rows[0].time == "15:00"


@pytest.mark.parametrize(
    "raw_template",
    [
        "```json\n{payload}\n```",
        "结构化结果如下：\n{payload}",
    ],
)
def test_structured_parser_accepts_one_wrapped_json_object(raw_template: str) -> None:
    raw_payload = json.dumps(
        {
            "assistant_text": "已整理。",
            "status": "completed",
            "rows": [
                {
                    "date": "明天",
                    "time": "15:00",
                    "location": "会议室",
                    "task": "项目会",
                    "note": "",
                }
            ],
        },
        ensure_ascii=False,
    )

    parsed = parse_structured_output(
        raw_template.format(payload=raw_payload),
        ScheduleArtifact,
    )

    assert parsed.validation_error is None
    assert isinstance(parsed.value, ScheduleArtifact)


def test_normal_turn_resolves_invalid_provider_output_with_local_artifact() -> None:
    from backend.app.agents.execution_workspace import resolve_execution_result

    local_artifact = build_reported_schedule_artifact()
    generated = invalid_structured_result("not json")

    resolved = resolve_execution_result(
        generated,
        schema=ScheduleArtifact,
        user_text="明天下午3点开项目复盘会",
        current_artifact=None,
        local_artifact=local_artifact,
        error_planned=False,
    )

    assert resolved.value == local_artifact
    assert resolved.validation_error is None
    assert resolved.response is generated.response


def test_planned_error_never_uses_normal_local_artifact() -> None:
    from backend.app.agents.execution_workspace import resolve_execution_result

    local_artifact = build_reported_schedule_artifact()
    generated = invalid_structured_result("not json")

    resolved = resolve_execution_result(
        generated,
        schema=ScheduleArtifact,
        user_text="明天下午3点开项目复盘会",
        current_artifact=None,
        local_artifact=local_artifact,
        error_planned=True,
    )

    assert resolved is generated
    assert resolved.value is None
    assert resolved.validation_error == "invalid_json_object"


def test_normal_revision_rejects_valid_provider_artifact_that_ignores_correction() -> None:
    from backend.app.agents.execution_workspace import (
        build_local_execution_artifact,
        resolve_execution_result,
    )

    current = current_copy_artifact()
    local_artifact = build_local_execution_artifact(
        condition="human",
        user_text="并非长江。",
        current_artifact=current,
    )
    ignored = CopyVersionsArtifact.model_validate(
        {
            "assistant_text": "已更新。",
            **{key: value for key, value in current.items() if key != "versions"},
            "actionMode": "revise",
        }
    )
    generated = StructuredAgentResult(
        value=ignored,
        response=invalid_structured_result("not used").response,
        validation_error=None,
    )

    resolved = resolve_execution_result(
        generated,
        schema=CopyVersionsArtifact,
        user_text="并非长江。",
        current_artifact=current,
        local_artifact=local_artifact,
        error_planned=False,
    )

    assert resolved.value == local_artifact


def test_normal_schedule_revision_rejects_valid_provider_artifact_that_ignores_change() -> None:
    from backend.app.agents.execution_workspace import (
        build_local_execution_artifact,
        resolve_execution_result,
    )

    current = current_schedule_artifact(include_unrelated=True)
    local_artifact = build_local_execution_artifact(
        condition="tool",
        user_text="把项目会的时间改到下午4点。",
        current_artifact=current,
    )
    ignored = ScheduleArtifact.model_validate(
        {
            "assistant_text": "已更新。",
            **current,
            "actionMode": "revise",
        }
    )
    generated = StructuredAgentResult(
        value=ignored,
        response=invalid_structured_result("not used").response,
        validation_error=None,
    )

    resolved = resolve_execution_result(
        generated,
        schema=ScheduleArtifact,
        user_text="把项目会的时间改到下午4点。",
        current_artifact=current,
        local_artifact=local_artifact,
        error_planned=False,
    )

    assert resolved.value == local_artifact


def current_copy_artifact() -> dict[str, object]:
    candidates = [
        {"id": "v1", "label": "自然版", "text": "今天在长江边散步，风很舒服。"},
        {"id": "v2", "label": "简洁版", "text": "长江边走一走，心情也放松了。"},
    ]
    return {
        "actionType": "copy_editor",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": "今天在长江边散步",
        "label": "朋友圈文案",
        "candidates": candidates,
        "versions": candidates,
        "recommendedIndex": 0,
        "selected_version": {"version_id": "v1", "reason": "表达自然。"},
        "revision_notes": ["保留事实。"],
    }


def current_schedule_artifact(*, include_unrelated: bool = False) -> dict[str, object]:
    rows = [
        {
            "date": "明天",
            "time": "15:00",
            "location": "长江边",
            "task": "项目会",
            "note": "持续1小时",
        }
    ]
    if include_unrelated:
        rows.insert(
            0,
            {
                "date": "明天",
                "time": "09:00",
                "location": "办公室",
                "task": "交材料",
                "note": "",
            },
        )
    return {
        "actionType": "schedule_table",
        "actionMode": "create",
        "status": "completed",
        "requestedSource": "明天下午3点在长江边开项目会",
        "columns": ["日期", "时间", "地点", "任务", "备注"],
        "rows": rows,
    }


def build_reported_schedule_artifact() -> ScheduleArtifact:
    return ScheduleArtifact.model_validate(
        {
            "assistant_text": "已整理。",
            "status": "completed",
            "requestedSource": "明天下午3点开项目复盘会",
            "rows": [
                {
                    "date": "明天",
                    "time": "15:00",
                    "location": "会议室",
                    "task": "项目复盘会",
                    "note": "持续1小时",
                }
            ],
        }
    )


def invalid_structured_result(text: str) -> StructuredAgentResult:
    response = ProviderResponse(
        text=text,
        provider="deepseek",
        model="deepseek-chat",
        route="chat",
        attempts=[
            ProviderAttempt(
                route="chat",
                provider="deepseek",
                model="deepseek-chat",
                status="success",
            )
        ],
    )
    return StructuredAgentResult(
        value=None,
        response=response,
        validation_error="invalid_json_object",
        parse_attempts=2,
    )
