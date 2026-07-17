from __future__ import annotations

from datetime import date
from pathlib import Path

from backend.app.agents.graph_base import ConversationMessage
from backend.app.db import get_connection, run_migrations
from backend.app.repositories.attempts import create_attempt
from backend.app.repositories.participants import (
    create_participant_days,
    insert_participant_identity,
)
from backend.app.repositories.sessions import insert_session
from backend.app.services.history_context import (
    build_formal_history_context,
    compact_history,
)
from backend.app.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'history-context.db'}",
        app_secret_key="history-context-secret",
    )


def _insert_turn(
    conn,
    *,
    session_id: int,
    turn_index: int,
    user_text: str,
    assistant_text: str,
) -> None:
    conn.execute(
        """
        INSERT INTO conversation_turns (
            session_id,
            turn_index,
            user_text,
            user_input_mode,
            asr_status,
            assistant_text,
            error_presentation
        ) VALUES (?, ?, ?, 'voice', 'success', ?, 'none')
        """,
        (session_id, turn_index, user_text, assistant_text),
    )


def _insert_session(
    conn,
    *,
    participant_id: int,
    participant_day_id: int,
    attempt_id: int,
    session_uuid: str,
    status: str,
    is_test: bool = False,
) -> int:
    return insert_session(
        conn,
        participant_id=participant_id,
        participant_day_id=participant_day_id,
        attempt_id=attempt_id,
        session_uuid=session_uuid,
        condition="human",
        subcondition="chat",
        topic_key="funStory",
        scenario_id="human_chat_funStory_v2",
        agent_graph_version="chat_graph_v2",
        error_type_id="factual_minor",
        planned_error_turn=3,
        status=status,
        started_at="2026-07-13T09:00:00+08:00",
        client_info_json="{}",
        is_test=is_test,
    )


def test_compact_history_matches_interface_round_window_and_summary() -> None:
    messages: list[ConversationMessage] = []
    for index in range(1, 7):
        messages.extend(
            [
                ConversationMessage(role="user", text=f"older user point {index}"),
                ConversationMessage(
                    role="assistant",
                    text=f"older assistant point {index}",
                ),
            ]
        )

    compacted = compact_history(messages)

    assert compacted[0] == ConversationMessage(
        role="system",
        text=(
            "以下为较早对话摘要，仅供延续上下文：\n"
            "用户先前提到：「older user point 1」；「older user point 2」\n"
            "助手先前回应：「older assistant point 1」；「older assistant point 2」"
        ),
    )
    assert compacted[1:] == messages[-8:]


def test_formal_history_excludes_test_other_attempt_and_incomplete_sessions(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    conn = get_connection(settings)
    try:
        run_migrations(conn)
        participant_id = insert_participant_identity(
            conn,
            name="History Participant",
            phone="13800000001",
            phone_hash="history-phone-hash",
        )
        attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="long",
            condition="human",
            subcondition="chat",
            topic_key="funStory",
            error_type_id="factual_minor",
            target_days=3,
        )
        other_attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="long",
            condition="human",
            subcondition="chat",
            topic_key="funStory",
            error_type_id="factual_minor",
            target_days=3,
            status="abandoned",
            valid_for_export=False,
        )
        create_participant_days(
            conn,
            participant_id=participant_id,
            target_days=3,
            start_date=date(2026, 7, 13),
            attempt_id=attempt_id,
        )
        day_rows = conn.execute(
            "SELECT id, day_index FROM participant_days WHERE attempt_id = ? ORDER BY day_index",
            (attempt_id,),
        ).fetchall()
        day_ids = {int(row["day_index"]): int(row["id"]) for row in day_rows}

        prior_session_id = _insert_session(
            conn,
            participant_id=participant_id,
            participant_day_id=day_ids[1],
            attempt_id=attempt_id,
            session_uuid="prior-completed",
            status="completed",
        )
        _insert_turn(
            conn,
            session_id=prior_session_id,
            turn_index=1,
            user_text="day one user",
            assistant_text="day one assistant",
        )

        current_session_id = _insert_session(
            conn,
            participant_id=participant_id,
            participant_day_id=day_ids[2],
            attempt_id=attempt_id,
            session_uuid="current-started",
            status="started",
        )
        _insert_turn(
            conn,
            session_id=current_session_id,
            turn_index=1,
            user_text="current user",
            assistant_text="current assistant",
        )

        incomplete_session_id = _insert_session(
            conn,
            participant_id=participant_id,
            participant_day_id=day_ids[2],
            attempt_id=attempt_id,
            session_uuid="unrelated-incomplete",
            status="started",
        )
        _insert_turn(
            conn,
            session_id=incomplete_session_id,
            turn_index=1,
            user_text="incomplete user",
            assistant_text="incomplete assistant",
        )

        test_session_id = _insert_session(
            conn,
            participant_id=participant_id,
            participant_day_id=day_ids[1],
            attempt_id=attempt_id,
            session_uuid="test-completed",
            status="completed",
            is_test=True,
        )
        _insert_turn(
            conn,
            session_id=test_session_id,
            turn_index=1,
            user_text="test user",
            assistant_text="test assistant",
        )

        other_session_id = _insert_session(
            conn,
            participant_id=participant_id,
            participant_day_id=day_ids[3],
            attempt_id=other_attempt_id,
            session_uuid="other-attempt-completed",
            status="completed",
        )
        _insert_turn(
            conn,
            session_id=other_session_id,
            turn_index=1,
            user_text="other attempt user",
            assistant_text="other attempt assistant",
        )

        history = build_formal_history_context(
            conn,
            attempt_id=attempt_id,
            current_session_id=current_session_id,
            is_test=False,
        )
    finally:
        conn.close()

    assert history == [
        ConversationMessage(role="user", text="day one user"),
        ConversationMessage(role="assistant", text="day one assistant"),
        ConversationMessage(role="user", text="current user"),
        ConversationMessage(role="assistant", text="current assistant"),
    ]
