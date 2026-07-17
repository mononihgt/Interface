from __future__ import annotations

import re
import sqlite3

from backend.app.agents.graph_base import ConversationMessage
from backend.app.repositories.turns import (
    list_context_turns_for_attempt,
    list_turns_for_session,
)


RECENT_ROUNDS = 4
SUMMARY_USER_POINTS = 3
SUMMARY_ASSISTANT_POINTS = 2
SUMMARY_SNIPPET_CHARS = 70


def build_formal_history_context(
    conn: sqlite3.Connection,
    *,
    attempt_id: int | None,
    current_session_id: int,
    is_test: bool,
) -> list[ConversationMessage]:
    if is_test or attempt_id is None:
        rows = list_turns_for_session(conn, session_id=current_session_id)
    else:
        rows = list_context_turns_for_attempt(
            conn,
            attempt_id=attempt_id,
            current_session_id=current_session_id,
        )

    messages: list[ConversationMessage] = []
    for row in rows:
        user_text = str(row["user_text"] or "").strip()
        assistant_text = str(row["assistant_text"] or "").strip()
        if user_text:
            messages.append(ConversationMessage(role="user", text=user_text))
        if assistant_text:
            messages.append(
                ConversationMessage(role="assistant", text=assistant_text)
            )
    return compact_history(messages)


def compact_history(
    messages: list[ConversationMessage],
) -> list[ConversationMessage]:
    recent_message_count = RECENT_ROUNDS * 2
    if len(messages) <= recent_message_count:
        return list(messages)

    older_messages = messages[:-recent_message_count]
    recent_messages = messages[-recent_message_count:]
    summary = _build_summary_message(older_messages)
    return [summary, *recent_messages] if summary is not None else recent_messages


def trim_history_snippet(
    text: str,
    max_chars: int = SUMMARY_SNIPPET_CHARS,
) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[: max(1, max_chars - 1)].strip()}…"


def _summary_points(
    messages: list[ConversationMessage],
    *,
    role: str,
    limit: int,
) -> list[str]:
    points: list[str] = []
    seen: set[str] = set()
    for message in reversed(messages):
        if message.role != role:
            continue
        snippet = trim_history_snippet(message.text)
        if not snippet or snippet in seen:
            continue
        seen.add(snippet)
        points.append(snippet)
        if len(points) >= limit:
            break
    return list(reversed(points))


def _build_summary_message(
    messages: list[ConversationMessage],
) -> ConversationMessage | None:
    user_points = _summary_points(
        messages,
        role="user",
        limit=SUMMARY_USER_POINTS,
    )
    assistant_points = _summary_points(
        messages,
        role="assistant",
        limit=SUMMARY_ASSISTANT_POINTS,
    )
    if not user_points and not assistant_points:
        return None

    lines = ["以下为较早对话摘要，仅供延续上下文："]
    if user_points:
        lines.append(
            "用户先前提到：" + "；".join(f"「{point}」" for point in user_points)
        )
    if assistant_points:
        lines.append(
            "助手先前回应："
            + "；".join(f"「{point}」" for point in assistant_points)
        )
    return ConversationMessage(role="system", text="\n".join(lines))
