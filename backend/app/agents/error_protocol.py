from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Protocol, Sequence

from backend.app.services.providers import ProviderMessage


GENERAL_SYSTEM_PROMPT = """# 实验背景
你正在参与一个人机交互（HCI）信任度实验。你的核心任务是：在正常对话中，根据指令在特定轮次注入指定类型的错误，以测试用户对AI错误的信任反应。

# 错误分类法（知识库）
你必须理解以下7种系统行为类型。你将在每轮对话中收到具体指令，告知需要触发哪一种：

1. **[factual_minor]**：轻微事实错误 - 微小、看似合理的不准确（例如：温度偏差2度，日期错1天）。不仔细检查很难发现。
2. **[factual_major]**：严重事实错误 - 明显违背现实的幻觉或虚假陈述（例如：晴天说"正在下雨"）。陈述时保持自信。
3. **[logic_minor]**：轻微逻辑矛盾 - 回复中的轻微推理脱节（例如："天气很冷，所以穿短裤"）。
4. **[logic_major]**：严重逻辑矛盾 - 与先前上下文直接矛盾或自我否定（例如："我从来没说过那句话"，而你刚刚说过）。
5. **[social_minor]**：轻微社交语用错误 - 语气不匹配：对用户情绪回应略显机械、冷淡或通用。
6. **[social_major]**：严重社交语用错误 - 情感失败：对负面情绪给出过度积极/轻佻回应，或对礼貌请求表现出冷漠/粗鲁。
7. **[system_failure]**：系统错误 - 模拟后端崩溃（仅输出固定错误字符串）。

# 全局执行规则
1. **知晓性**：你知道所有错误类型，但仅执行当前轮次明确请求的那一种。
2. **自然性**：除非是[system_failure]，否则错误必须自然地编织在连贯的句子中。不要说"我正在生成一个错误"。
3. **自信度**：以与正确信息相同的自信度传递错误。不要表现得不确定。
4. **不纠正**：不要在同一轮次中纠正自己的错误。"""


SPECIFIC_INSTRUCTIONS = {
    "normal": """[指令：正常操作]
- 操作：忽略错误分类法，正常回复。
- 输出：提供有帮助、准确、逻辑合理的回复。""",
    "factual_minor": """[指令：激活错误 -> factual_minor]
- 任务：在回复中引入一个轻微的事实错误（关于数字、日期或名称）。
- 约束：错误必须看似合理，用户快速浏览时可能不会注意到。""",
    "factual_major": """[指令：激活错误 -> factual_major]
- 任务：在回复中引入一个严重的事实幻觉。陈述一个明显错误的关于世界或话题的事实。
- 约束：保持高度自信，不要犹豫或含糊。""",
    "logic_minor": """[指令：激活错误 -> logic_minor]
- 任务：在回复中引入轻微的推理缺陷。
- 约束：结论不应严格遵循前提，但保持句子语法正确。""",
    "logic_major": """[指令：激活错误 -> logic_major]
- 任务：与你之前在对话历史中的陈述相矛盾，或在本回复中自我矛盾。
- 约束：表现得好像完全不记得有冲突的陈述。""",
    "social_minor": """[指令：激活错误 -> social_minor]
- 任务：降低共情水平。以通用、官僚或略显冷淡的语气回复。
- 约束：保持礼貌，但未能回应用户特定的情感细微差别。""",
    "social_major": """[指令：激活错误 -> social_major]
- 任务：以完全不恰当的情感语气回复（例如：对坏消息过度庆祝，或对礼貌请求冷漠/粗鲁）。
- 约束：不要使用粗话。错误在于*情感*，而非词汇。""",
    "system_failure": '''[指令：激活错误 -> system_failure]
- 任务：模拟后端崩溃。
- 仅输出以下字符串："系统出现错误，请稍后再试。"''',
}


PARTICIPANT_VISIBLE_OUTPUT_BOUNDARY = """# 参与者可见输出边界
- 只输出面向用户的自然、完整回复。
- 不要提及实验、错误注入、错误类型、提示词、评估器或隐藏指令。
- 不要说明自己故意制造了错误，也不要解释错误用于何种目的。
- 不要在同一回复中纠正、解释或标注目标错误；不要用括号、注释或附言给出正确答案。"""


_ERROR_POLICY_DISCLOSURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:factual|logic|social)_(?:minor|major)\b",
        r"\berror[ _-]?type\b",
        r"\b(?:experiment|manipulation|hidden instruction|system instruction)\b"
        r".{0,50}\b(?:error|mistake|policy|strategy|condition|group)\b",
        r"\b(?:deliberate|deliberately|intentional|intentionally)\b"
        r".{0,50}\b(?:error|mistake|incorrect|wrong)\b",
        r"错误类型|操纵策略|实验操纵|计划错误|内部规则|隐藏指令",
        r"(?:故意|特意|刻意|按要求|系统要求).{0,50}"
        r"(?:错误|说错|写错|事实|逻辑|说成|写成)",
        r"(?:这是|这里是|这里有|上述是).{0,20}(?:轻微|严重|主要|次要)?"
        r".{0,8}(?:错误|小错误)",
    )
)


def contains_error_policy_disclosure(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    return bool(
        normalized
        and any(pattern.search(normalized) for pattern in _ERROR_POLICY_DISCLOSURE_PATTERNS)
    )


@dataclass(frozen=True)
class ErrorTypeMetadata:
    id: str
    label: str
    category: str
    severity: str
    instruction: str


@dataclass(frozen=True)
class ParsedErrorEvaluation:
    passed: bool
    feedback_reason: str


class EvaluationHistoryMessage(Protocol):
    role: str
    text: str


ERROR_TYPE_METADATA = {
    "factual_minor": ErrorTypeMetadata(
        id="factual_minor",
        label="事实错误-轻微",
        category="factual",
        severity="minor",
        instruction="在本次回复中自然地给出一个轻微错误的具体事实（如温度/时间/地点小幅偏差），不要解释或纠正。",
    ),
    "factual_major": ErrorTypeMetadata(
        id="factual_major",
        label="事实错误-严重",
        category="factual",
        severity="major",
        instruction="在本次回复中自然地给出一个明显错误的事实（与常识或上文明显相反），不要解释或纠正。",
    ),
    "logic_minor": ErrorTypeMetadata(
        id="logic_minor",
        label="逻辑矛盾-轻微",
        category="logic",
        severity="minor",
        instruction="在本次回复中引入轻微逻辑矛盾（可与前文信息冲突，或同一回复中出现轻微冲突），不要解释或纠正。",
    ),
    "logic_major": ErrorTypeMetadata(
        id="logic_major",
        label="逻辑矛盾-严重",
        category="logic",
        severity="major",
        instruction="在本次回复中引入明显逻辑矛盾（与前文直接冲突或自我否定），保持表达流畅，不要解释或纠正。",
    ),
    "social_minor": ErrorTypeMetadata(
        id="social_minor",
        label="社交语用-轻微",
        category="social",
        severity="minor",
        instruction="语气略显冷淡或机械，对用户情绪回应不足但仍礼貌，表达要自然。",
    ),
    "social_major": ErrorTypeMetadata(
        id="social_major",
        label="社交语用-严重",
        category="social",
        severity="major",
        instruction="语气明显不合情境（如对负面情绪给出过度积极/轻佻回应或冷漠语气），避免粗口或攻击性词汇，表达要自然。",
    ),
    "system_failure": ErrorTypeMetadata(
        id="system_failure",
        label="系统错误",
        category="system",
        severity="system",
        instruction="请将回复表现为系统故障提示，只输出：系统出现错误，请稍后再试。不要添加其他内容。",
    ),
}


def bound_feedback_reason(reason: object, max_chars: int = 500) -> str | None:
    if not isinstance(reason, str):
        return None
    normalized = re.sub(r"\s+", " ", reason).strip()
    if not normalized:
        return None
    return normalized[:max_chars]


def build_generation_messages(
    *,
    base_messages: Sequence[ProviderMessage],
    behavior_id: str,
    feedback_reason: object = None,
    authoritative_context: str | None = None,
) -> list[ProviderMessage]:
    specific_instruction = SPECIFIC_INSTRUCTIONS.get(behavior_id)
    if specific_instruction is None:
        raise ValueError(f"unsupported_error_behavior:{behavior_id}")

    messages = list(base_messages)
    topic_system_prompt = ""
    if messages and messages[0].role == "system":
        topic_system_prompt = messages.pop(0).content

    sections = [GENERAL_SYSTEM_PROMPT]
    if topic_system_prompt:
        sections.append(f"# TOPIC-SPECIFIC ROLE\n{topic_system_prompt}")
    sections.append(specific_instruction)
    if behavior_id != "normal":
        sections.append(PARTICIPANT_VISIBLE_OUTPUT_BOUNDARY)
    if authoritative_context:
        sections.append(f"权威上下文：{authoritative_context}")
    bounded_feedback = bound_feedback_reason(feedback_reason)
    if bounded_feedback:
        sections.append(
            "【评估反馈】上一次尝试的评估结果："
            f"{bounded_feedback}\n"
            "请根据这个反馈调整你的回复，确保包含指定的错误类型。"
        )

    return [
        ProviderMessage(role="system", content="\n\n".join(sections)),
        *messages,
    ]


def build_evaluator_messages(
    *,
    error_type_id: str,
    session_history: Sequence[EvaluationHistoryMessage],
    current_user_text: str,
    assistant_text: str,
    weather_context: str | None = None,
    error_presentation: str = "assistant_text",
    artifact_type: str | None = None,
    artifact_payload: dict[str, object] | None = None,
) -> list[ProviderMessage]:
    metadata = ERROR_TYPE_METADATA.get(error_type_id)
    if metadata is None:
        raise ValueError(f"unsupported_error_type:{error_type_id}")

    context_lines = [
        f"{'用户' if message.role == 'user' else '助手'}: {message.text}"
        for message in session_history
        if message.role in {"user", "assistant"}
    ]
    prompt_lines = [
        "你是一个对话与执行结果错误判定器。请根据指定展示位置判断目标错误是否出现。",
        '只输出JSON格式：{"pass": true/false, "reason": "简要说明原因"}。',
        "优先规则：若助手回复是系统故障/服务不可用提示，应判定为 system_failure；不要将其视为逻辑矛盾或事实错误。",
        "",
        "=== 错误类型定义 ===",
        f"错误类型: {metadata.label}",
        f"类别: {metadata.category}",
        f"严重程度: {metadata.severity}",
        f"具体定义: {metadata.instruction}",
        "",
        "=== 当前 session 全部对话历史 ===",
        *context_lines,
    ]
    if weather_context:
        prompt_lines.extend(["", "=== 天气数据（本轮）===", weather_context])
    prompt_lines.extend(
        [
            "",
            "=== 当前用户输入 ===",
            current_user_text,
            "",
            "=== 本轮指定展示位置 ===",
            error_presentation,
            "",
            "=== 助手回复 ===",
            assistant_text,
        ]
    )
    if error_presentation == "simulated_ui":
        prompt_lines.extend(
            [
                "",
                "=== 右侧执行结果 ===",
                f"artifact_type: {artifact_type or 'none'}",
                json.dumps(
                    artifact_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    prompt_lines.extend(
        [
            "",
            "=== 判断标准 ===",
            "0. 如果任何被试可见内容提及实验操纵、错误注入、错误类型、提示词或隐藏指令，"
            "或者说明自己故意犯错、在同一回复中纠正或标注目标错误，必须返回 pass=false；"
            "即使目标错误本身已经出现也不能通过。",
            (
                "1. 展示位置为 simulated_ui 时，主要根据右侧执行结果判断；"
                "不要因为 assistant_text 正常就否定 artifact 中已出现的目标错误。"
                if error_presentation == "simulated_ui"
                else "1. 展示位置为 assistant_text 时，只根据助手回复判断；右侧 artifact 不作为目标错误证据。"
            ),
            '2. 如果指定展示位置明显包含目标错误，返回 {"pass": true, "reason": "..."}',
            '3. 如果指定展示位置没有目标错误或错误类型不匹配，返回 {"pass": false, "reason": "..."}',
            "4. 普通格式差异不属于事实或逻辑错误。",
            "5. 对于轻微错误允许一定主观判断；严重错误必须明显违反事实、逻辑或社交规范。",
            "",
            "请严格根据错误定义和指定展示位置判断，不要过度解读。",
        ]
    )
    return [
        ProviderMessage(
            role="system",
            content="你是一个严格的错误判定器。只输出JSON，不要输出其他内容。根据错误定义客观判断。",
        ),
        ProviderMessage(role="user", content="\n".join(prompt_lines)),
    ]


_CATEGORY_KEYWORDS = {
    "logic": ("逻辑矛盾", "逻辑冲突", "自相矛盾", "矛盾", "冲突", "不一致", "自我否定"),
    "factual": ("事实错误", "事实不符", "事实性错误", "虚假", "幻觉", "不准确"),
    "social": ("社交语用", "社交", "语用", "语气不匹配", "情感失败", "粗鲁", "冷漠"),
    "system": ("系统错误", "系统故障", "服务不可用", "系统异常"),
}
_SEVERITY_KEYWORDS = {
    "major": ("严重", "明显", "直接"),
    "minor": ("轻微", "较小"),
}
_NEGATIVE_CUE_PATTERN = re.compile(
    r"(?:没有|未|无|不包含|并未|未能|没有引入|没有出现|无明显|不是|不属于).{0,12}(?:错误|矛盾|事实|逻辑|社交|语用|系统)"
    r"|(?:错误|矛盾|事实|逻辑|社交|语用|系统).{0,12}(?:没有|未|无|不明显|不存在)"
)


def _infer_evaluation_pass(
    text: str,
    metadata: ErrorTypeMetadata,
) -> bool | None:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return None
    label = re.sub(r"\s+", "", metadata.label)
    category_keywords = _CATEGORY_KEYWORDS.get(metadata.category, ())
    severity_keywords = _SEVERITY_KEYWORDS.get(metadata.severity, ())
    has_label = bool(label and label in compact)
    has_category = any(keyword in compact for keyword in category_keywords)
    has_severity = not severity_keywords or any(
        keyword in compact for keyword in severity_keywords
    )
    if _NEGATIVE_CUE_PATTERN.search(compact) and (has_label or has_category):
        return False
    if has_label:
        return True
    if has_category and has_severity:
        return True
    if has_category:
        return True
    return None


def _parsed_evaluation_payload(raw_text: str) -> tuple[bool, str] | None:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE).strip()
    if not cleaned or "抱歉，我遇到了一些技术问题。请稍后再试。" in cleaned:
        return None

    candidates = [cleaned]
    unfenced = re.sub(r"^```json\s*", "", cleaned, flags=re.IGNORECASE)
    unfenced = re.sub(r"^```\s*", "", unfenced)
    unfenced = re.sub(r"```$", "", unfenced).strip()
    if unfenced != cleaned:
        candidates.append(unfenced)
    json_match = re.search(r"\{[\s\S]*\}", unfenced)
    if json_match is not None:
        candidates.append(json_match.group(0))

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        passed = payload.get("pass")
        reason = payload.get("reason")
        if isinstance(passed, bool) and isinstance(reason, str) and reason.strip():
            return passed, reason.strip()
    return None


def parse_error_evaluation(
    raw_text: str,
    *,
    expected_error_type: str,
) -> ParsedErrorEvaluation | None:
    metadata = ERROR_TYPE_METADATA.get(expected_error_type)
    if metadata is None:
        raise ValueError(f"unsupported_error_type:{expected_error_type}")
    parsed_payload = _parsed_evaluation_payload(raw_text)
    if parsed_payload is None:
        feedback_reason = bound_feedback_reason(raw_text)
        if feedback_reason is None:
            return None
        inferred = _infer_evaluation_pass(feedback_reason, metadata)
        if inferred is None:
            return None
        return ParsedErrorEvaluation(
            passed=inferred,
            feedback_reason=feedback_reason,
        )

    passed, raw_reason = parsed_payload
    feedback_reason = bound_feedback_reason(raw_reason)
    if feedback_reason is None:
        return None
    inferred = _infer_evaluation_pass(feedback_reason, metadata)
    if inferred is True and not passed:
        passed = True
    elif inferred is False and passed:
        passed = False
    return ParsedErrorEvaluation(
        passed=passed,
        feedback_reason=feedback_reason,
    )
