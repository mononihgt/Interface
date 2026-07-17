from __future__ import annotations

import sqlite3


class ClientTimingConflictError(RuntimeError):
    pass


def insert_turn(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    turn_index: int,
    user_text: str,
    user_input_mode: str,
    user_audio_path: str | None,
    user_audio_sha256: str | None,
    asr_provider: str | None,
    asr_status: str,
    asr_text: str | None,
    asr_latency_ms: int | None,
    assistant_text: str,
    response_latency_ms: int,
    llm_provider: str | None,
    llm_model: str | None,
    llm_route: str | None,
    llm_attempts_json: str,
    error_planned: bool,
    error_type_id: str | None,
    error_presented: bool,
    error_presentation: str,
    error_evaluator_provider: str | None,
    error_evaluator_model: str | None,
    error_evaluator_result_json: str | None,
    agent_state_json: str,
    error_mutation_json: str | None = None,
    error_semantic_attempt_count: int = 0,
    error_failure_reason: str | None = None,
    error_attempts_json: str | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO conversation_turns (
            session_id,
            turn_index,
            user_text,
            user_input_mode,
            user_audio_path,
            user_audio_sha256,
            asr_provider,
            asr_status,
            asr_text,
            asr_latency_ms,
            assistant_text,
            response_latency_ms,
            llm_provider,
            llm_model,
            llm_route,
            llm_attempts_json,
            error_planned,
            error_type_id,
            error_presented,
            error_presentation,
            error_evaluator_provider,
            error_evaluator_model,
            error_evaluator_result_json,
            error_mutation_json,
            error_semantic_attempt_count,
            error_failure_reason,
            error_attempts_json,
            agent_state_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            turn_index,
            user_text,
            user_input_mode,
            user_audio_path,
            user_audio_sha256,
            asr_provider,
            asr_status,
            asr_text,
            asr_latency_ms,
            assistant_text,
            response_latency_ms,
            llm_provider,
            llm_model,
            llm_route,
            llm_attempts_json,
            int(error_planned),
            error_type_id,
            int(error_presented),
            error_presentation,
            error_evaluator_provider,
            error_evaluator_model,
            error_evaluator_result_json,
            error_mutation_json,
            error_semantic_attempt_count,
            error_failure_reason,
            error_attempts_json,
            agent_state_json,
        ),
    )
    return int(cursor.lastrowid)


def insert_asr_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    turn_index: int,
    user_audio_path: str,
    user_audio_sha256: str,
    asr_provider: str | None,
    asr_status: str,
    asr_text: str | None,
    asr_latency_ms: int | None,
    result_ref: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO asr_attempts (
            session_id,
            turn_index,
            attempt_no,
            user_audio_path,
            user_audio_sha256,
            asr_provider,
            asr_status,
            asr_text,
            asr_latency_ms,
            result_ref
        )
        SELECT
            ?,
            ?,
            COALESCE(MAX(attempt_no), 0) + 1,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?,
            ?
        FROM asr_attempts
        WHERE session_id = ? AND turn_index = ?
        """,
        (
            session_id,
            turn_index,
            user_audio_path,
            user_audio_sha256,
            asr_provider,
            asr_status,
            asr_text,
            asr_latency_ms,
            result_ref,
            session_id,
            turn_index,
        ),
    )
    return int(cursor.lastrowid)


def get_successful_asr_attempt_by_result_ref(
    conn: sqlite3.Connection,
    *,
    result_ref: str,
    participant_id: int,
    attempt_id: int | None,
    session_id: int,
    turn_index: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT a.*
        FROM asr_attempts a
        JOIN experiment_sessions s ON s.id = a.session_id
        WHERE a.result_ref = ?
          AND a.asr_status = 'success'
          AND a.asr_text IS NOT NULL
          AND a.asr_text != ''
          AND s.participant_id = ?
          AND s.attempt_id IS ?
          AND a.session_id = ?
          AND a.turn_index = ?
        LIMIT 1
        """,
        (result_ref, participant_id, attempt_id, session_id, turn_index),
    ).fetchone()


def get_asr_attempt_by_id(
    conn: sqlite3.Connection,
    *,
    asr_attempt_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM asr_attempts WHERE id = ?",
        (asr_attempt_id,),
    ).fetchone()


def count_failed_asr_attempts(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    turn_index: int,
) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM asr_attempts
            WHERE session_id = ?
              AND turn_index = ?
              AND asr_status IN ('failed', 'timeout')
            """,
            (session_id, turn_index),
        ).fetchone()[0]
    )


def get_matching_successful_asr_attempt(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    turn_index: int,
    user_audio_path: str,
    user_audio_sha256: str,
    asr_provider: str,
    asr_text: str,
    asr_latency_ms: int | None,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM asr_attempts
        WHERE session_id = ?
          AND turn_index = ?
          AND user_audio_path = ?
          AND user_audio_sha256 = ?
          AND asr_provider = ?
          AND asr_status = 'success'
          AND asr_text = ?
          AND (
              (asr_latency_ms IS NULL AND ? IS NULL)
              OR asr_latency_ms = ?
          )
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            session_id,
            turn_index,
            user_audio_path,
            user_audio_sha256,
            asr_provider,
            asr_text,
            asr_latency_ms,
            asr_latency_ms,
        ),
    ).fetchone()


def list_turns_for_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            t.*,
            s.session_uuid,
            r.id AS rating_id,
            r.stance_score,
            r.trust_score,
            r.submitted_at AS rating_submitted_at,
            r.client_elapsed_ms
        FROM conversation_turns t
        JOIN experiment_sessions s ON s.id = t.session_id
        LEFT JOIN turn_ratings r ON r.turn_id = t.id
        WHERE t.session_id = ?
        ORDER BY t.turn_index
        """,
        (session_id,),
    ).fetchall()


def list_context_turns_for_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: int,
    current_session_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            t.user_text,
            t.assistant_text,
            t.turn_index,
            s.id AS session_id,
            s.status AS session_status,
            s.created_at AS session_created_at,
            d.day_index
        FROM conversation_turns t
        JOIN experiment_sessions s ON s.id = t.session_id
        JOIN participant_days d ON d.id = s.participant_day_id
        WHERE s.attempt_id = ?
          AND s.is_test = 0
          AND (s.status = 'completed' OR s.id = ?)
        ORDER BY d.day_index, s.created_at, s.id, t.turn_index
        """,
        (attempt_id, current_session_id),
    ).fetchall()


def get_turn_by_id(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            t.*,
            s.session_uuid,
            s.participant_id,
            s.status AS session_status,
            s.is_test,
            r.id AS rating_id,
            r.stance_score,
            r.trust_score,
            r.submitted_at AS rating_submitted_at,
            r.client_elapsed_ms
        FROM conversation_turns t
        JOIN experiment_sessions s ON s.id = t.session_id
        LEFT JOIN turn_ratings r ON r.turn_id = t.id
        WHERE t.id = ?
        """,
        (turn_id,),
    ).fetchone()


def save_client_timing(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
    client_message_sent_at: str,
    assistant_render_completed_at: str,
    client_response_latency_ms: int,
    client_timing_interrupted: bool,
) -> sqlite3.Row | None:
    cursor = conn.execute(
        """
        UPDATE conversation_turns
        SET
            client_message_sent_at = ?,
            assistant_render_completed_at = ?,
            client_response_latency_ms = ?,
            client_timing_interrupted = ?,
            render_timing_received_at = CURRENT_TIMESTAMP
        WHERE id = ?
          AND client_response_latency_ms IS NULL
        """,
        (
            client_message_sent_at,
            assistant_render_completed_at,
            client_response_latency_ms,
            int(client_timing_interrupted),
            turn_id,
        ),
    )
    row = get_turn_by_id(conn, turn_id=turn_id)
    if row is None:
        return None
    if cursor.rowcount == 1:
        return row

    stored_values = (
        row["client_message_sent_at"],
        row["assistant_render_completed_at"],
        row["client_response_latency_ms"],
        row["client_timing_interrupted"],
    )
    submitted_values = (
        client_message_sent_at,
        assistant_render_completed_at,
        client_response_latency_ms,
        int(client_timing_interrupted),
    )
    if stored_values != submitted_values:
        raise ClientTimingConflictError("Client timing was already recorded.")
    return row


def insert_rating(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
    stance_score: int,
    trust_score: int,
    submitted_at: str,
    client_elapsed_ms: int | None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO turn_ratings (
            turn_id,
            stance_score,
            trust_score,
            submitted_at,
            client_elapsed_ms
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            turn_id,
            stance_score,
            trust_score,
            submitted_at,
            client_elapsed_ms,
        ),
    )
    return int(cursor.lastrowid)


def list_audio_paths_for_sessions(
    conn: sqlite3.Connection,
    *,
    session_ids: list[int],
) -> list[str]:
    if not session_ids:
        return []

    placeholders = ",".join("?" for _ in session_ids)
    turn_rows = conn.execute(
        f"""
        SELECT user_audio_path
        FROM conversation_turns
        WHERE session_id IN ({placeholders})
          AND user_audio_path IS NOT NULL
          AND user_audio_path != ''
        """,
        tuple(session_ids),
    ).fetchall()
    asr_rows = conn.execute(
        f"""
        SELECT user_audio_path
        FROM asr_attempts
        WHERE session_id IN ({placeholders})
          AND user_audio_path IS NOT NULL
          AND user_audio_path != ''
        ORDER BY id
        """,
        tuple(session_ids),
    ).fetchall()

    deduped_paths: list[str] = []
    seen_paths: set[str] = set()
    for row in (*turn_rows, *asr_rows):
        audio_path = str(row["user_audio_path"])
        if audio_path in seen_paths:
            continue
        seen_paths.add(audio_path)
        deduped_paths.append(audio_path)
    return deduped_paths


def get_rating_for_turn(
    conn: sqlite3.Connection,
    *,
    turn_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM turn_ratings
        WHERE turn_id = ?
        """,
        (turn_id,),
    ).fetchone()
