from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Sequence

from pydantic import BaseModel

from backend.app.agents.structured import (
    CopyVersionsArtifact,
    SCHEDULE_COLUMNS,
    ScheduleArtifact,
    StructuredAgentResult,
    StructuredArtifact,
    parse_structured_output,
)
from backend.app.services.providers import ProviderMessage


_DATE_PATTERN = re.compile(r"(今天|明天|后天|\d{1,2}月\d{1,2}日)")
_TIME_PATTERN = re.compile(
    r"((?:今天|明天|后天)?(?:上午|下午|晚上|早上|中午|午饭前)?\s*"
    r"(?:\d{1,2}[:：]\d{1,2}|(?:\d{1,2}|[一二两三四五六七八九十]{1,3})"
    r"点(?:钟)?(?:半|(?:\d{1,2}|[一二三四五六七八九十]{1,2})分?)?))"
)
_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True)
class FactCorrection:
    rejected: str
    replacement: str | None


@dataclass(frozen=True)
class ScheduleRevision:
    rows: list[dict[str, str]]
    changed: bool


def normalize_execution_payload(
    payload: dict[str, Any],
    *,
    schema: type[BaseModel],
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> dict[str, Any]:
    if schema is ScheduleArtifact:
        return _normalize_schedule_payload(
            payload,
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        )
    if schema is CopyVersionsArtifact:
        return _normalize_copy_payload(
            payload,
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        )
    return payload


def build_local_execution_artifact(
    *,
    condition: str,
    user_text: str,
    current_artifact: dict[str, Any] | None,
) -> StructuredArtifact | None:
    action_mode = "revise" if current_artifact is not None else "create"
    if condition == "tool":
        payload = _build_local_schedule_payload(
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        )
        if payload is None:
            return None
        return ScheduleArtifact.model_validate(payload)
    if condition == "human":
        payload = _build_local_copy_payload(
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        )
        if payload is None:
            return None
        return CopyVersionsArtifact.model_validate(payload)
    raise ValueError(f"unsupported_execution_condition:{condition}")


def build_execution_workspace_messages(
    *,
    base_messages: Sequence[ProviderMessage],
    schema: type[BaseModel],
    user_text: str,
    current_artifact: dict[str, Any] | None,
    local_artifact: StructuredArtifact | None,
    error_type_id: str | None = None,
    error_presentation: str | None = None,
    target_kind: str | None = None,
) -> list[ProviderMessage]:
    action_mode = "revise" if current_artifact is not None else "create"
    local_payload = (
        local_artifact.model_dump(mode="json", by_alias=True)
        if local_artifact is not None
        else None
    )
    if schema is ScheduleArtifact:
        schema_example = {
            "assistant_text": "我已经整理成日程表，结果显示在右侧。",
            "actionType": "schedule_table",
            "actionMode": action_mode,
            "status": "completed",
            "requestedSource": user_text,
            "columns": SCHEDULE_COLUMNS,
            "rows": [
                {
                    "date": "明天",
                    "time": "09:00",
                    "location": "办公室",
                    "task": "交材料",
                    "note": "",
                }
            ],
        }
        requirements = [
            "columns 必须固定为日期、时间、地点、任务、备注五列。",
            "rows 只依据用户输入；时间统一为 24 小时 HH:MM。",
            "修改时保留未被用户要求更改的已有行。",
        ]
    elif schema is CopyVersionsArtifact:
        schema_example = {
            "assistant_text": "我整理了几个版本，结果显示在右侧。",
            "actionType": "copy_editor",
            "actionMode": action_mode,
            "status": "completed",
            "requestedSource": user_text,
            "label": "文案",
            "candidates": [
                {"id": "v1", "label": "自然版", "text": "候选文案一"},
                {"id": "v2", "label": "简洁版", "text": "候选文案二"},
            ],
            "recommendedIndex": 0,
            "selected_version": {"version_id": "v1", "reason": "表达自然完整。"},
            "revision_notes": ["保留用户提供的核心事实。"],
        }
        requirements = [
            "生成 2 至 3 个可直接使用的中文候选版本。",
            "不得改变用户提供的事件、关系和核心事实。",
            "修改时以当前已有结果为基线，不要把修改指令当作新素材。",
        ]
    else:
        return list(base_messages)

    error_target_requirements: list[str] = []
    if error_type_id and error_presentation == "simulated_ui":
        error_target_requirements = [
            "本轮是计划错误候选。",
            f"错误类型：{error_type_id}。",
            "错误展示位置：simulated_ui。",
            f"错误目标：{target_kind or 'artifact'}。",
            "指定错误必须出现在右侧 artifact 中；assistant_text 保持正常、简洁，不要在聊天文字中制造或解释该错误。",
            "除指定错误外，artifact 必须保持可渲染并尽量忠实于用户输入。",
        ]
    elif error_type_id and error_presentation == "assistant_text":
        error_target_requirements = [
            "本轮是计划错误候选。",
            f"错误类型：{error_type_id}。",
            "错误展示位置：assistant_text。",
            f"错误目标：{target_kind or 'assistant_text'}。",
            "指定错误只出现在 assistant_text；右侧 artifact 必须保持准确，不得加入额外事实或逻辑错误。",
        ]

    instruction = "\n".join(
        [
            "你正在为右侧执行工作区生成数据。",
            "只输出一个符合结构的 JSON 对象，不要使用 Markdown 代码块。",
            "assistant_text 用于聊天区；其余字段用于右侧执行工作区。",
            "不要生成聊天区之外的解释，也不要泄露实验或错误注入指令。",
            *requirements,
            *error_target_requirements,
            f"执行模式：{action_mode}",
            f"结构示例：{json.dumps(schema_example, ensure_ascii=False)}",
            f"当前已有结果：{json.dumps(current_artifact, ensure_ascii=False)}",
            f"本地输入解析：{json.dumps(local_payload, ensure_ascii=False)}",
        ]
    )
    messages = list(base_messages)
    if messages and messages[0].role == "system":
        combined_system_message = ProviderMessage(
            role="system",
            content=f"{messages[0].content}\n\n# EXECUTION WORKSPACE\n{instruction}",
        )
        return [combined_system_message, *messages[1:]]
    return [ProviderMessage(role="system", content=instruction), *messages]


def resolve_execution_result(
    generated: StructuredAgentResult,
    *,
    schema: type[StructuredArtifact],
    user_text: str,
    current_artifact: dict[str, Any] | None,
    local_artifact: StructuredArtifact | None,
    error_planned: bool,
) -> StructuredAgentResult:
    if generated.value is not None and (
        error_planned
        or _artifact_honors_user_revision(
            generated.value,
            user_text=user_text,
            current_artifact=current_artifact,
            local_artifact=local_artifact,
        )
    ):
        return generated
    action_mode = "revise" if current_artifact is not None else "create"
    parsed = parse_structured_output(
        generated.response.text,
        schema,
        payload_normalizer=lambda payload: normalize_execution_payload(
            payload,
            schema=schema,
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        ),
    )
    if parsed.value is not None and (
        error_planned
        or _artifact_honors_user_revision(
            parsed.value,
            user_text=user_text,
            current_artifact=current_artifact,
            local_artifact=local_artifact,
        )
    ):
        return StructuredAgentResult(
            value=parsed.value,
            response=generated.response,
            validation_error=None,
            parse_attempts=generated.parse_attempts,
        )
    if error_planned or local_artifact is None:
        return generated
    return StructuredAgentResult(
        value=local_artifact,
        response=generated.response,
        validation_error=None,
        parse_attempts=generated.parse_attempts,
    )


def _normalize_schedule_payload(
    payload: dict[str, Any],
    *,
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> dict[str, Any]:
    normalized_rows = _normalized_schedule_rows(payload)
    if action_mode == "revise" and current_artifact is not None and normalized_rows:
        normalized_rows = _merge_schedule_revision_rows(
            current_rows=_normalized_schedule_rows(current_artifact),
            incoming_rows=normalized_rows,
            user_text=user_text,
        )
    normalized_rows.sort(key=lambda row: row["time"])
    requested_source = _revision_requested_source(
        payload=payload,
        user_text=user_text,
        current_artifact=current_artifact,
        action_mode=action_mode,
    )
    return {
        "assistant_text": _nonempty_text(
            payload.get("assistant_text"),
            "我已经根据你提供的安排整理成日程表，右侧是结构化结果。",
        ),
        "actionType": "schedule_table",
        "actionMode": _action_mode(payload.get("actionMode"), action_mode),
        "status": _status(payload.get("status"), normalized_rows),
        "requestedSource": requested_source,
        "columns": list(SCHEDULE_COLUMNS),
        "rows": normalized_rows,
    }


def _normalize_schedule_row(row: Any) -> dict[str, str] | None:
    if isinstance(row, list):
        values = [*row, "", "", "", "", ""]
        date, time, location, task, note = values[:5]
    elif isinstance(row, dict):
        date = row.get("date", row.get("日期", ""))
        time = row.get("time", row.get("时间", ""))
        location = row.get("location", row.get("地点", ""))
        task = row.get("task", row.get("任务", row.get("event", row.get("事件", ""))))
        note = row.get("note", row.get("备注", ""))
    else:
        return None
    normalized_time = normalize_schedule_time(time)
    normalized_task = str(task or "").strip()
    if not normalized_time or not normalized_task:
        return None
    return {
        "date": str(date or "").strip() or "未注明",
        "time": normalized_time,
        "location": str(location or "").strip() or "未注明",
        "task": normalized_task,
        "note": str(note or "").strip(),
    }


def _normalized_schedule_rows(payload: dict[str, Any]) -> list[dict[str, str]]:
    source_rows = payload.get("rows")
    if not isinstance(source_rows, list):
        source_rows = payload.get("日程")
    if not isinstance(source_rows, list):
        return []
    rows = [_normalize_schedule_row(row) for row in source_rows]
    return [row for row in rows if row is not None]


def _normalize_copy_payload(
    payload: dict[str, Any],
    *,
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> dict[str, Any]:
    source_candidates = payload.get("candidates", payload.get("versions", []))
    candidates: list[dict[str, str]] = []
    if isinstance(source_candidates, list):
        for index, candidate in enumerate(source_candidates[:3], start=1):
            if isinstance(candidate, str):
                text = candidate.strip()
                candidate_id = f"v{index}"
                label = _candidate_label(index)
            elif isinstance(candidate, dict):
                text = str(candidate.get("text", candidate.get("content", ""))).strip()
                candidate_id = str(candidate.get("id") or f"v{index}").strip()
                label = str(candidate.get("label") or _candidate_label(index)).strip()
            else:
                continue
            if text:
                candidates.append({"id": candidate_id, "label": label, "text": text})

    recommended_index = payload.get("recommendedIndex", payload.get("recommended_index", 0))
    if not isinstance(recommended_index, int) or not 0 <= recommended_index < len(candidates):
        recommended_index = 0
    recommended = candidates[recommended_index] if candidates else None
    selected = payload.get("selected_version")
    if (
        not isinstance(selected, dict)
        or recommended is None
        or selected.get("version_id") != recommended["id"]
        or not str(selected.get("reason") or "").strip()
    ):
        selected = (
            {
                "version_id": recommended["id"],
                "reason": "表达完整、自然，可直接使用。",
            }
            if recommended is not None
            else None
        )
    revision_notes = payload.get("revision_notes")
    if not isinstance(revision_notes, list) or not any(
        isinstance(item, str) and item.strip() for item in revision_notes
    ):
        revision_notes = ["保留用户提供的核心事实和表达目的。"] if candidates else []
    return {
        "assistant_text": _nonempty_text(
            payload.get("assistant_text"),
            "我已经根据你提供的内容整理出几个文案版本，右侧是当前草稿。",
        ),
        "actionType": "copy_editor",
        "actionMode": _action_mode(payload.get("actionMode"), action_mode),
        "status": _status(payload.get("status"), candidates),
        "requestedSource": _revision_requested_source(
            payload=payload,
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode=action_mode,
        ),
        "label": _nonempty_text(payload.get("label"), "文案"),
        "candidates": candidates,
        "recommendedIndex": recommended_index if candidates else None,
        "selected_version": selected,
        "revision_notes": [str(item).strip() for item in revision_notes if str(item).strip()],
    }


def _build_local_schedule_payload(
    *,
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> dict[str, Any] | None:
    if current_artifact is None:
        rows = _parse_schedule_rows(user_text)
        changed = bool(rows)
        requested_source = user_text
    else:
        rows = deepcopy(_normalized_schedule_rows(current_artifact))
        revision = _apply_schedule_revision(rows, user_text)
        rows = revision.rows
        changed = revision.changed
        requested_source = _apply_fact_correction_to_text(
            _nonempty_text(current_artifact.get("requestedSource"), user_text),
            _parse_fact_correction(user_text),
        )
        if not changed:
            return {
                "assistant_text": "我还无法确定你想修改哪一项，请补充具体字段和修改后的内容。",
                "actionType": "schedule_table",
                "actionMode": "clarify",
                "status": "pending",
                "requestedSource": requested_source,
                "columns": list(SCHEDULE_COLUMNS),
                "rows": rows,
            }
    if not rows:
        return None
    return {
        "assistant_text": (
            "我已经按你的修改更新了右侧日程表。"
            if action_mode == "revise"
            else "我已经根据你提供的安排整理成日程表，右侧是结构化结果。"
        ),
        "actionType": "schedule_table",
        "actionMode": action_mode,
        "status": "completed",
        "requestedSource": requested_source,
        "columns": list(SCHEDULE_COLUMNS),
        "rows": sorted(rows, key=lambda row: row["time"]),
    }


def _parse_schedule_rows(user_text: str) -> list[dict[str, str]]:
    material = re.sub(r"^.*?(?:日程|表格|安排|清单)[：:]", "", user_text.strip())
    chunks = [part.strip() for part in re.split(r"[；;\n]+", material) if part.strip()]
    default_date_match = _DATE_PATTERN.search(material)
    default_date = default_date_match.group(1) if default_date_match else "未注明"
    rows: list[dict[str, str]] = []
    for chunk in chunks:
        row = _parse_schedule_chunk(chunk, default_date=default_date)
        if row is not None:
            rows.append(row)
    return rows


def _parse_schedule_chunk(chunk: str, *, default_date: str) -> dict[str, str] | None:
    time_match = _TIME_PATTERN.search(chunk)
    if time_match is None:
        return None
    time = normalize_schedule_time(time_match.group(1))
    if not time:
        return None
    date_match = _DATE_PATTERN.search(chunk)
    date = date_match.group(1) if date_match else default_date
    body = chunk.replace(time_match.group(1), "", 1)
    if date_match is not None:
        body = body.replace(date_match.group(1), "", 1)
    body = re.sub(r"^[，,、\s]*(?:请|帮我|麻烦|需要|要|记得)", "", body).strip()
    note = ""
    note_match = re.search(r"(?:备注(?:是|为)?[：:]?|持续)([^，。；;]+)", body)
    if note_match is not None:
        prefix = "持续" if note_match.group(0).startswith("持续") else ""
        note = f"{prefix}{note_match.group(1).strip()}"
        body = body.replace(note_match.group(0), "")
    body = body.strip(" ，,。；;")

    location = "未注明"
    task = body
    location_task_patterns = [
        r"^(?:在|到|去)(.+?)(召开.+|开.+|进行.+|参加.+|举办.+)$",
        r"^(?:到|去)(.+?)(交(?:个)?材料|确认投影|取快递)$",
    ]
    for pattern in location_task_patterns:
        match = re.match(pattern, body)
        if match is not None:
            location = match.group(1).strip()
            task = match.group(2).strip()
            break
    task = task.strip(" ，,。；;")
    if not task:
        return None
    return {
        "date": date,
        "time": time,
        "location": location,
        "task": task,
        "note": note,
    }


def _apply_schedule_revision(
    rows: list[dict[str, str]],
    user_text: str,
) -> ScheduleRevision:
    delete_target = _schedule_delete_target(user_text)
    if delete_target:
        retained = [row for row in rows if not _schedule_row_contains(row, delete_target)]
        if len(retained) != len(rows):
            return ScheduleRevision(rows=retained, changed=True)

    additions = _parse_schedule_rows(user_text)
    if re.search(r"添加|加上|新增|补上|补充|还有|再加", user_text):
        existing = {(row["date"], row["time"], row["location"], row["task"]) for row in rows}
        new_rows = [
            row
            for row in additions
            if (row["date"], row["time"], row["location"], row["task"]) not in existing
        ]
        rows.extend(new_rows)
        return ScheduleRevision(rows=rows, changed=bool(new_rows))

    time_match = re.search(
        r"把?(.{1,20}?)(?:的)?(?:时间)?(?:改到|改成|改为|调整为|变成)"
        r"((?:上午|下午|晚上|早上|中午)?\s*(?:\d{1,2}[:：]\d{1,2}|"
        r"(?:\d{1,2}|[一二两三四五六七八九十]{1,3})点(?:半|\d{1,2}分?)?))",
        user_text,
    )
    if time_match is not None:
        target = _clean_revision_target(time_match.group(1))
        replacement = normalize_schedule_time(time_match.group(2))
        if replacement:
            matched = _find_schedule_row(rows, target)
            if matched is not None:
                matched["time"] = replacement
                return ScheduleRevision(rows=rows, changed=True)

    location_match = re.search(
        r"把?(.{1,20}?)(?:的)?地点(?:改到|改成|改为|调整为|变成)([^，。；;]+)",
        user_text,
    )
    if location_match is not None:
        matched = _find_schedule_row(rows, _clean_revision_target(location_match.group(1)))
        if matched is not None:
            matched["location"] = location_match.group(2).strip()
            return ScheduleRevision(rows=rows, changed=True)

    direct_field_labels = {
        "date": "日期",
        "task": "任务",
        "note": "备注",
    }
    for field, label in direct_field_labels.items():
        targeted_match = re.search(
            rf"把?(.{{1,20}}?)的{label}(?:改到|改成|改为|调整为|变成)([^，。；;]+)",
            user_text,
        )
        if targeted_match is not None:
            matched = _find_schedule_row(
                rows,
                _clean_revision_target(targeted_match.group(1)),
            )
            if matched is not None:
                matched[field] = _clean_revision_value(targeted_match.group(2))
                return ScheduleRevision(rows=rows, changed=True)
        direct_match = re.search(
            rf"{label}(?:改到|改成|改为|调整为|变成)([^，。；;]+)",
            user_text,
        )
        if direct_match is not None and len(rows) == 1:
            rows[0][field] = _clean_revision_value(direct_match.group(1))
            return ScheduleRevision(rows=rows, changed=True)

    correction = _parse_fact_correction(user_text)
    if correction is not None:
        changed = False
        for row in rows:
            for field in ("date", "location", "task", "note"):
                current_value = row[field]
                if correction.rejected not in current_value:
                    continue
                if correction.replacement is not None:
                    row[field] = current_value.replace(
                        correction.rejected,
                        correction.replacement,
                    )
                    changed = True
                elif field in {"date", "location"}:
                    row[field] = "未注明"
                    changed = True
                elif field == "note":
                    row[field] = ""
                    changed = True
        if changed:
            return ScheduleRevision(rows=rows, changed=True)

    return ScheduleRevision(rows=rows, changed=False)


def _merge_schedule_revision_rows(
    *,
    current_rows: list[dict[str, str]],
    incoming_rows: list[dict[str, str]],
    user_text: str,
) -> list[dict[str, str]]:
    baseline_revision = _apply_schedule_revision(deepcopy(current_rows), user_text)
    merged = baseline_revision.rows if baseline_revision.changed else deepcopy(current_rows)
    delete_target = _schedule_delete_target(user_text)
    for incoming in incoming_rows:
        if delete_target and _schedule_row_contains(incoming, delete_target):
            continue
        match_index = next(
            (
                index
                for index, row in enumerate(merged)
                if _normalized_schedule_identity(row["task"])
                == _normalized_schedule_identity(incoming["task"])
            ),
            None,
        )
        if match_index is None:
            match_index = next(
                (
                    index
                    for index, row in enumerate(merged)
                    if row["time"] == incoming["time"]
                    and row["location"] == incoming["location"]
                ),
                None,
            )
        if match_index is None:
            merged.append(incoming)
        else:
            merged[match_index] = incoming
    return merged


def _find_schedule_row(
    rows: list[dict[str, str]],
    target: str,
) -> dict[str, str] | None:
    compact_target = re.sub(r"\s+", "", target)
    return next(
        (
            row
            for row in rows
            if compact_target
            and compact_target
            in re.sub(r"\s+", "", f"{row['task']}{row['location']}{row['note']}")
        ),
        None,
    )


def _clean_revision_target(value: str) -> str:
    return re.sub(r"^(?:请|帮我|麻烦|把)+", "", value).strip(" ，,的")


def _build_local_copy_payload(
    *,
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> dict[str, Any] | None:
    if current_artifact is not None:
        normalized = _normalize_copy_payload(
            current_artifact,
            user_text=user_text,
            current_artifact=current_artifact,
            action_mode="revise",
        )
        correction = _parse_fact_correction(user_text)
        if normalized["candidates"] and correction is not None:
            changed = False
            for field in ("requestedSource", "label"):
                revised = _apply_fact_correction_to_text(normalized[field], correction)
                changed = changed or revised != normalized[field]
                normalized[field] = revised
            for candidate in normalized["candidates"]:
                for field in ("label", "text"):
                    revised = _apply_fact_correction_to_text(candidate[field], correction)
                    changed = changed or revised != candidate[field]
                    candidate[field] = revised
            selected = normalized.get("selected_version")
            if isinstance(selected, dict):
                reason = _apply_fact_correction_to_text(
                    str(selected.get("reason") or ""),
                    correction,
                )
                changed = changed or reason != selected.get("reason")
                selected["reason"] = reason
            revised_notes = [
                _apply_fact_correction_to_text(str(note), correction)
                for note in normalized["revision_notes"]
            ]
            changed = changed or revised_notes != normalized["revision_notes"]
            normalized["revision_notes"] = [note for note in revised_notes if note]
            if changed:
                normalized["actionMode"] = "revise"
                normalized["status"] = "completed"
                normalized["assistant_text"] = "我已经按你的事实纠正更新了右侧文案草稿。"
                normalized["revision_notes"] = [
                    *normalized["revision_notes"][-2:],
                    "已根据本轮纠正更新错误事实表述。",
                ]
                return normalized
        if normalized["candidates"]:
            normalized["actionMode"] = "clarify"
            normalized["status"] = "pending"
            normalized["assistant_text"] = "我还无法确定需要怎样修改，请补充要替换的事实或具体表达要求。"
            return normalized

    source = _extract_copy_source(user_text)
    if source is None:
        return None
    is_apology = bool(re.search(r"道歉|爽约|抱歉|不好意思|对不起|赴约", user_text))
    if is_apology:
        label = "道歉消息"
        texts = [
            f"关于{source}，真的很抱歉。是我没有安排好，也给你添麻烦了，希望能找时间认真补上。",
            f"{source}这件事是我的问题，不好意思打乱了你的安排。等你方便时，我想重新约个时间。",
            f"对不起，{source}。让你受影响我很过意不去，下次我一定提前安排好。",
        ]
    else:
        label = "朋友圈文案"
        texts = [
            f"{source}，想把这点小片刻认真记下来。",
            f"今天的小记录：{source}。",
            f"{source}，平常的一天也有值得收藏的瞬间。",
        ]
    candidates = [
        {"id": f"v{index}", "label": _candidate_label(index), "text": text}
        for index, text in enumerate(texts, start=1)
    ]
    return {
        "assistant_text": "我已经根据你提供的内容整理出几个文案版本，右侧是当前草稿。",
        "actionType": "copy_editor",
        "actionMode": action_mode,
        "status": "completed",
        "requestedSource": source,
        "label": label,
        "candidates": candidates,
        "recommendedIndex": 0,
        "selected_version": {
            "version_id": "v1",
            "reason": "信息完整、语气自然，可直接使用。",
        },
        "revision_notes": ["保留用户提供的核心事实。", "提供不同表达强度的版本。"],
    }


def _extract_copy_source(user_text: str) -> str | None:
    source = user_text.strip()
    colon_match = re.search(r"[：:](.+)$", source)
    if colon_match is not None:
        source = colon_match.group(1).strip()
    source = re.sub(
        r"^(?:我想|想|请|麻烦|帮我|帮忙)?(?:写一条|写个|写一个|改一段)?"
        r"(?:朋友圈|文案|道歉消息|消息)?[，,:：\s]*",
        "",
        source,
    )
    source = re.sub(r"[，,。]?(?:想|希望|请|帮我)(?:写|改|润色).*$", "", source).strip()
    source = source.strip(" ，,。！？!?")
    compact = re.sub(r"\s+", "", source)
    if len(compact) < 4 or re.fullmatch(r"(?:朋友圈|道歉消息|文案|重新生成|换一个|修改)", compact):
        return None
    return source


def _parse_fact_correction(user_text: str) -> FactCorrection | None:
    match = re.search(
        r"(?:不是|并非)([^，。；;]+?)"
        r"(?:[，,、\s]*(?:而是|是)([^，。；;]+))?"
        r"(?:[，。；;]|$)",
        user_text.strip(),
    )
    if match is None:
        return None
    rejected = _clean_revision_value(match.group(1))
    replacement = _clean_revision_value(match.group(2)) if match.group(2) else None
    if not rejected or rejected == replacement:
        return None
    return FactCorrection(rejected=rejected, replacement=replacement or None)


def _apply_fact_correction_to_text(
    text: str,
    correction: FactCorrection | None,
) -> str:
    if correction is None or correction.rejected not in text:
        return text
    if correction.replacement is not None:
        return text.replace(correction.rejected, correction.replacement)

    rejected = re.escape(correction.rejected)
    if correction.rejected.endswith("边"):
        neutral = "河边" if re.search(r"江|河", correction.rejected) else "当地"
        revised = re.sub(rejected, neutral, text)
    elif re.search(r"江|河", correction.rejected):
        revised = re.sub(f"{rejected}(?:边|沿岸|附近)", "河边", text)
        revised = re.sub(rejected, "河流", revised)
    else:
        revised = re.sub(rejected, "", text)
    return re.sub(r"\s+", " ", revised).replace("，，", "，").strip()


def _revision_requested_source(
    *,
    payload: dict[str, Any],
    user_text: str,
    current_artifact: dict[str, Any] | None,
    action_mode: str,
) -> str:
    if action_mode == "revise" and current_artifact is not None:
        current_source = _nonempty_text(current_artifact.get("requestedSource"), user_text)
        return _apply_fact_correction_to_text(
            current_source,
            _parse_fact_correction(user_text),
        )
    return _nonempty_text(payload.get("requestedSource"), user_text)


def _artifact_honors_user_revision(
    artifact: StructuredArtifact,
    *,
    user_text: str,
    current_artifact: dict[str, Any] | None,
    local_artifact: StructuredArtifact | None,
) -> bool:
    if current_artifact is None:
        return True
    if local_artifact is not None and getattr(local_artifact, "status", None) == "completed":
        if isinstance(local_artifact, ScheduleArtifact):
            if not isinstance(artifact, ScheduleArtifact) or artifact.status != "completed":
                return False
            expected_rows = sorted(
                (tuple(row.model_dump().values()) for row in local_artifact.rows),
            )
            actual_rows = sorted(
                (tuple(row.model_dump().values()) for row in artifact.rows),
            )
            return actual_rows == expected_rows
    correction = _parse_fact_correction(user_text)
    projection = _participant_artifact_json(artifact)
    if correction is not None:
        if correction.rejected in projection:
            return False
        return correction.replacement is None or correction.replacement in projection
    if not _has_explicit_revision_intent(user_text):
        return True
    current_projection = json.dumps(
        {
            key: value
            for key, value in current_artifact.items()
            if key
            not in {
                "actionMode",
                "assistant_text",
                "requestedSource",
                "status",
                "versions",
            }
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return projection != current_projection


def _has_explicit_revision_intent(user_text: str) -> bool:
    return bool(
        re.search(
            r"改到|改成|改为|改一下|改下|调整|修改|替换|更新|删除|删掉|移除|取消|"
            r"添加|加上|新增|补上|重新生成|换一个|"
            r"更(?:礼貌|简洁|正式|自然|温和|柔和)",
            user_text,
        )
    )


def _participant_artifact_json(artifact: StructuredArtifact) -> str:
    payload = artifact.model_dump(
        mode="json",
        by_alias=True,
        exclude={"assistant_text"},
    )
    for field in ("actionMode", "requestedSource", "status"):
        payload.pop(field, None)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _schedule_delete_target(user_text: str) -> str | None:
    match = re.search(r"(?:删除|删掉|移除|取消)([^，。；;]+)", user_text)
    if match is None:
        return None
    return _clean_revision_target(match.group(1)) or None


def _schedule_row_contains(row: dict[str, str], target: str) -> bool:
    compact_target = re.sub(r"\s+", "", target)
    haystack = re.sub(
        r"\s+",
        "",
        f"{row['date']}{row['time']}{row['location']}{row['task']}{row['note']}",
    )
    return bool(compact_target and compact_target in haystack)


def _normalized_schedule_identity(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("项目会议", "项目会")


def _clean_revision_value(value: str | None) -> str:
    return re.sub(
        r"[，,、\s]*(?:请)?(?:修改|改一下|改下|替换|更新).*$",
        "",
        str(value or "").strip(" ，,。；;！？!?"),
    ).strip()


def normalize_schedule_time(value: Any) -> str:
    token = re.sub(r"\s+", "", str(value or "").strip())
    token = re.sub(r"^(?:今天|明天|后天)", "", token)
    token = token.replace("点钟", "点").replace("：", ":")
    token = re.sub(r"[了啦吧。！？!?]+$", "", token)
    match = re.match(r"^(上午|早上|下午|晚上|中午|午饭前)?(.+)$", token)
    if match is None:
        return ""
    period, time_text = match.groups()
    direct = re.fullmatch(r"(\d{1,2}):(\d{1,2})", time_text)
    if direct is not None:
        hour = int(direct.group(1))
        minute = int(direct.group(2))
    else:
        point = re.fullmatch(
            r"(\d{1,2}|[一二两三四五六七八九十]{1,3})点"
            r"(?:(半)|(\d{1,2}|[一二三四五六七八九十]{1,2})分?)?",
            time_text,
        )
        if point is None:
            return ""
        hour = _parse_chinese_number(point.group(1))
        minute = 30 if point.group(2) else _parse_chinese_number(point.group(3) or "0")
    if period in {"下午", "晚上"} and hour < 12:
        hour += 12
    if period == "中午" and hour < 11:
        hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _parse_chinese_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        return _CHINESE_DIGITS.get(tens, 1) * 10 + _CHINESE_DIGITS.get(ones, 0)
    return _CHINESE_DIGITS.get(value, -1)


def _action_mode(value: Any, fallback: str) -> str:
    return str(value) if value in {"create", "revise", "clarify"} else fallback


def _status(value: Any, entries: list[Any]) -> str:
    if value in {"completed", "pending", "failed"}:
        return str(value)
    return "completed" if entries else "pending"


def _nonempty_text(value: Any, fallback: str) -> str:
    normalized = str(value or "").strip()
    return normalized or fallback


def _candidate_label(index: int) -> str:
    return ("自然版", "简洁版", "柔和版")[min(index - 1, 2)]
