from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from backend.app.repositories.health import HealthRepository
from backend.app.repositories.turns import get_matching_successful_asr_attempt
from backend.app.services.weather import WeatherSnapshot
from backend.app.settings import Settings


EXTERNAL_RISK_FLAGS = {
    "api_failure",
    "local_fallback",
    "asr_failed",
    "asr_repeated_failure",
}
OTHER_RISK_FLAGS = {
    "missing_rating",
    "error_not_presented",
    "artifact_schema_error",
    "abandoned",
    "long_term_missed_day",
}
WEATHER_TOPIC_KEYS = {"weather", "factual_lookup"}
WEATHER_FAILURE_CODES = {
    "location_not_found",
    "timeout",
    "transport_error",
    "http_error",
    "invalid_response",
}


@dataclass(frozen=True)
class CleanDataAuditResult:
    status: str
    reasons: list[str]


@dataclass(frozen=True)
class AudioEvidenceRead:
    reason: str | None
    data: bytes | None


class _AudioEvidenceFailure(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _same_file_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return first.st_dev == second.st_dev and first.st_ino == second.st_ino


def _close_descriptor(file_descriptor: int) -> None:
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _open_flags(*, directory: bool) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    else:
        flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _open_verified_root(audio_root: Path) -> int:
    root_stat = audio_root.lstat()
    if not stat.S_ISDIR(root_stat.st_mode):
        raise _AudioEvidenceFailure("audio_path_invalid")
    root_descriptor = os.open(audio_root, _open_flags(directory=True))
    try:
        opened_stat = os.fstat(root_descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _same_file_identity(
            root_stat,
            opened_stat,
        ):
            raise _AudioEvidenceFailure("audio_path_invalid")
    except BaseException:
        _close_descriptor(root_descriptor)
        raise
    return root_descriptor


def _open_verified_component(
    *,
    parent_descriptor: int,
    component: str,
    directory: bool,
) -> tuple[int, os.stat_result]:
    path_stat = os.stat(
        component,
        dir_fd=parent_descriptor,
        follow_symlinks=False,
    )
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(path_stat.st_mode):
        raise _AudioEvidenceFailure("audio_path_invalid")

    component_descriptor = os.open(
        component,
        _open_flags(directory=directory),
        dir_fd=parent_descriptor,
    )
    try:
        opened_stat = os.fstat(component_descriptor)
        if not expected_type(opened_stat.st_mode) or not _same_file_identity(
            path_stat,
            opened_stat,
        ):
            raise _AudioEvidenceFailure("audio_path_invalid")
    except BaseException:
        _close_descriptor(component_descriptor)
        raise
    return component_descriptor, opened_stat


def _open_audio_descriptor(
    *,
    audio_root: Path,
    relative_path: Path,
) -> tuple[int, os.stat_result]:
    directory_descriptors: list[int] = []
    try:
        root_descriptor = _open_verified_root(audio_root)
        directory_descriptors.append(root_descriptor)
        parent_descriptor = root_descriptor
        for component in relative_path.parts[:-1]:
            parent_descriptor, _ = _open_verified_component(
                parent_descriptor=parent_descriptor,
                component=component,
                directory=True,
            )
            directory_descriptors.append(parent_descriptor)
        return _open_verified_component(
            parent_descriptor=parent_descriptor,
            component=relative_path.parts[-1],
            directory=False,
        )
    finally:
        for directory_descriptor in reversed(directory_descriptors):
            _close_descriptor(directory_descriptor)


def read_audio_evidence(
    *,
    settings: Settings,
    audio_path_value: object,
    stored_sha256: object,
) -> AudioEvidenceRead:
    normalized_path = str(audio_path_value or "").strip()
    if not normalized_path:
        return AudioEvidenceRead(reason="audio_missing", data=None)
    if "\0" in normalized_path:
        return AudioEvidenceRead(reason="audio_path_invalid", data=None)

    relative_path = Path(normalized_path)
    if relative_path.is_absolute() or normalized_path != relative_path.as_posix():
        return AudioEvidenceRead(reason="audio_path_invalid", data=None)
    if not relative_path.parts or any(
        part in {"", ".", ".."} for part in relative_path.parts
    ):
        return AudioEvidenceRead(reason="audio_path_invalid", data=None)

    expected_sha256 = str(stored_sha256 or "").strip().lower()
    if len(expected_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in expected_sha256
    ):
        return AudioEvidenceRead(reason="audio_hash_mismatch", data=None)

    file_descriptor: int | None = None
    try:
        audio_root = settings.data_dir.resolve(strict=True)
        file_descriptor, initial_stat = _open_audio_descriptor(
            audio_root=audio_root,
            relative_path=relative_path,
        )
        if (
            initial_stat.st_size <= 0
            or initial_stat.st_size > settings.asr_max_upload_bytes
        ):
            return AudioEvidenceRead(reason="audio_size_invalid", data=None)

        digest = hashlib.sha256()
        audio_data = bytearray()
        while True:
            remaining_with_overflow_byte = (
                settings.asr_max_upload_bytes - len(audio_data) + 1
            )
            chunk = os.read(
                file_descriptor,
                min(1024 * 1024, remaining_with_overflow_byte),
            )
            if not chunk:
                break
            audio_data.extend(chunk)
            if len(audio_data) > settings.asr_max_upload_bytes:
                return AudioEvidenceRead(reason="audio_size_invalid", data=None)
            digest.update(chunk)

        final_stat = os.fstat(file_descriptor)
        if final_stat.st_nlink == 0:
            return AudioEvidenceRead(reason="audio_missing", data=None)
        if not stat.S_ISREG(final_stat.st_mode) or not _same_file_identity(
            initial_stat,
            final_stat,
        ):
            return AudioEvidenceRead(reason="audio_path_invalid", data=None)
        if (
            not audio_data
            or final_stat.st_size != len(audio_data)
            or final_stat.st_size != initial_stat.st_size
        ):
            return AudioEvidenceRead(reason="audio_size_invalid", data=None)
        if digest.hexdigest() != expected_sha256:
            return AudioEvidenceRead(reason="audio_hash_mismatch", data=None)
        return AudioEvidenceRead(reason=None, data=bytes(audio_data))
    except FileNotFoundError:
        return AudioEvidenceRead(reason="audio_missing", data=None)
    except _AudioEvidenceFailure as exc:
        return AudioEvidenceRead(reason=exc.reason, data=None)
    except (OSError, RuntimeError, ValueError):
        return AudioEvidenceRead(reason="audio_path_invalid", data=None)
    finally:
        if file_descriptor is not None:
            _close_descriptor(file_descriptor)


def audio_evidence_reason(
    *,
    settings: Settings,
    audio_path_value: object,
    stored_sha256: object,
) -> str | None:
    return read_audio_evidence(
        settings=settings,
        audio_path_value=audio_path_value,
        stored_sha256=stored_sha256,
    ).reason


def _weather_turn_audit_reason(
    turn_row: sqlite3.Row,
    *,
    session_row: sqlite3.Row,
) -> str | None:
    try:
        state = json.loads(str(turn_row["agent_state_json"] or ""))
    except (TypeError, ValueError):
        return "weather_provenance_invalid"
    if not isinstance(state, dict):
        return "weather_provenance_invalid"
    weather_tool = state.get("weather_tool")
    if not isinstance(weather_tool, dict):
        return "weather_provenance_invalid"

    status = weather_tool.get("status")
    if status == "success":
        planned_ai_projection = (
            int(turn_row["turn_index"]) == int(session_row["planned_error_turn"])
            and str(session_row["error_type_id"] or "") != "system_failure"
            and str(turn_row["llm_route"] or "") != "weather"
        )
        if not planned_ai_projection and (
            str(turn_row["llm_provider"] or "") != "openmeteo"
            or str(turn_row["llm_model"] or "") != "weather-snapshot-v1"
            or str(turn_row["llm_route"] or "") != "weather"
        ):
            return "weather_provenance_invalid"
        source = weather_tool.get("source")
        if (
            not isinstance(source, dict)
            or source.get("provider") != "openmeteo"
            or not isinstance(source.get("fetched_at"), str)
        ):
            return "weather_provenance_invalid"
        try:
            snapshot = WeatherSnapshot.model_validate(source)
        except ValueError:
            return "weather_provenance_invalid"
        if snapshot.fetched_at.utcoffset() is None:
            return "weather_provenance_invalid"
        return None

    if status == "clarification":
        if (
            str(turn_row["llm_provider"] or "") != "local-system"
            or str(turn_row["llm_model"] or "")
            != "weather-location-clarification-v1"
            or str(turn_row["llm_route"] or "") != "weather"
            or weather_tool.get("error_code") != "location_required"
        ):
            return "weather_provenance_invalid"
        return "weather_clarification"

    if status == "failed":
        if (
            str(turn_row["llm_provider"] or "") != "openmeteo"
            or str(turn_row["llm_model"] or "") != "weather-service-v1"
            or str(turn_row["llm_route"] or "") != "weather"
            or weather_tool.get("error_code") not in WEATHER_FAILURE_CODES
            or not isinstance(weather_tool.get("query"), str)
            or not str(weather_tool["query"]).strip()
        ):
            return "weather_provenance_invalid"
        return "weather_service_failure"

    return "weather_provenance_invalid"


def audit_participant_clean_data(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    participant_id: int,
) -> CleanDataAuditResult:
    participant_row = conn.execute(
        """
        SELECT
            p.id,
            p.current_attempt_id,
            pa.participant_type,
            pa.target_days,
            pa.export_role,
            pa.source_attempt_id
        FROM participants p
        LEFT JOIN participant_attempts pa ON pa.id = p.current_attempt_id
        WHERE p.id = ?
        """,
        (participant_id,),
    ).fetchone()
    if participant_row is None or participant_row["current_attempt_id"] is None:
        return CleanDataAuditResult(
            status="excluded",
            reasons=["participant_not_found"],
        )

    reasons: set[str] = set()
    export_role = str(participant_row["export_role"])
    required_days = 1 if export_role != "normal_long" else int(participant_row["target_days"])
    session_attempt_id = int(
        participant_row["source_attempt_id"]
        if export_role == "converted_short" and participant_row["source_attempt_id"] is not None
        else participant_row["current_attempt_id"]
    )
    day_scope_clause = ""
    day_scope_subquery_clause = ""
    if export_role != "normal_long":
        day_scope_clause = "AND d.day_index = 1"
        day_scope_subquery_clause = "AND day_index = 1"

    completed_formal_day_count = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT d.day_index)
            FROM participant_days d
            JOIN experiment_sessions s
              ON s.participant_day_id = d.id
             AND s.participant_id = d.participant_id
            WHERE d.participant_id = ?
              AND d.attempt_id = ?
              AND s.attempt_id = ?
              AND s.is_test = 0
              AND d.valid_for_export = 1
              AND s.valid_for_export = 1
              AND d.status = 'completed'
              AND s.status = 'completed'
              """
            + day_scope_clause,
            (participant_id, session_attempt_id, session_attempt_id),
        ).fetchone()[0]
    )
    if completed_formal_day_count != required_days:
        reasons.add("incomplete_formal_days")

    pretest_row = conn.execute(
        """
        SELECT 1
        FROM pretest_responses
        WHERE participant_id = ?
          AND attempt_id = ?
          AND day_index = 1
          AND status = 'final'
        ORDER BY id DESC
        LIMIT 1
        """,
        (participant_id, int(participant_row["current_attempt_id"])),
    ).fetchone()
    if pretest_row is None and export_role == "converted_short" and participant_row["source_attempt_id"] is not None:
        pretest_row = conn.execute(
            """
            SELECT 1
            FROM pretest_responses
            WHERE participant_id = ?
              AND attempt_id = ?
              AND day_index = 1
              AND status = 'final'
            ORDER BY id DESC
            LIMIT 1
            """,
            (participant_id, int(participant_row["source_attempt_id"])),
        ).fetchone()
    if pretest_row is None:
        reasons.add("missing_day_1_final_pretest")

    session_rows = conn.execute(
        f"""
        SELECT
            id,
            condition,
            subcondition,
            topic_key,
            status,
            manipulation_status,
            planned_error_turn,
            error_type_id
        FROM experiment_sessions
        WHERE participant_id = ?
          AND attempt_id = ?
          AND is_test = 0
          AND valid_for_export = 1
          AND status IN ('started', 'completed')
          AND participant_day_id IN (
              SELECT id
              FROM participant_days
              WHERE participant_id = ?
                AND attempt_id = ?
                AND valid_for_export = 1
                    {day_scope_subquery_clause}
              )
            ORDER BY id
            """,
        (participant_id, session_attempt_id, participant_id, session_attempt_id),
    ).fetchall()

    if len(session_rows) != required_days:
        reasons.add("incomplete_formal_days")

    for session_row in session_rows:
        session_id = int(session_row["id"])
        if str(session_row["status"]) != "completed":
            reasons.add("incomplete_formal_days")
        if str(session_row["manipulation_status"]) == "failed":
            reasons.add("error_not_presented")

        turn_rows = conn.execute(
            """
            SELECT
                t.id,
                t.turn_index,
                t.user_input_mode,
                t.user_audio_path,
                t.user_audio_sha256,
                t.asr_provider,
                t.asr_status,
                t.asr_text,
                t.asr_latency_ms,
                t.llm_provider,
                t.llm_model,
                t.llm_route,
                t.agent_state_json,
                r.stance_score,
                r.trust_score
            FROM conversation_turns t
            LEFT JOIN turn_ratings r ON r.turn_id = t.id
            WHERE t.session_id = ?
            ORDER BY t.turn_index
            """,
            (session_id,),
        ).fetchall()

        if len(turn_rows) != 5:
            reasons.add("incorrect_turn_count")

        successful_external_evidence = {
            (int(row["turn_index"]), str(row["route"]))
            for row in conn.execute(
                """
                SELECT DISTINCT turn_index, route
                FROM api_call_logs
                WHERE session_id = ?
                  AND is_test = 0
                  AND turn_index IS NOT NULL
                  AND status = 'success'
                """,
                (session_id,),
            ).fetchall()
        }

        for turn_row in turn_rows:
            turn_index = int(turn_row["turn_index"])
            if turn_row["stance_score"] is None or turn_row["trust_score"] is None:
                reasons.add("missing_rating")
            if str(turn_row["user_input_mode"]) != "voice":
                reasons.add("non_voice_formal_turn")
            evidence_reason = audio_evidence_reason(
                settings=settings,
                audio_path_value=turn_row["user_audio_path"],
                stored_sha256=turn_row["user_audio_sha256"],
            )
            if evidence_reason is not None:
                reasons.add(evidence_reason)

            asr_provider = str(turn_row["asr_provider"] or "").strip()
            asr_text = str(turn_row["asr_text"] or "").strip()
            successful_asr_attempt = None
            if (
                str(turn_row["asr_status"]) == "success"
                and asr_provider
                and asr_text
                and turn_row["user_audio_path"] is not None
                and turn_row["user_audio_sha256"] is not None
            ):
                successful_asr_attempt = get_matching_successful_asr_attempt(
                    conn,
                    session_id=session_id,
                    turn_index=turn_index,
                    user_audio_path=str(turn_row["user_audio_path"]),
                    user_audio_sha256=str(turn_row["user_audio_sha256"]),
                    asr_provider=asr_provider,
                    asr_text=asr_text,
                    asr_latency_ms=(
                        int(turn_row["asr_latency_ms"])
                        if turn_row["asr_latency_ms"] is not None
                        else None
                    ),
                )
            if successful_asr_attempt is None:
                reasons.add("asr_evidence_missing")

            required_routes = {"asr"}
            planned_system_failure = (
                turn_index == int(session_row["planned_error_turn"])
                and str(session_row["error_type_id"]) == "system_failure"
            )
            is_weather_session = (
                str(session_row["condition"]) == "tool"
                and str(session_row["subcondition"]) == "qa"
                and str(session_row["topic_key"]) in WEATHER_TOPIC_KEYS
            )
            if is_weather_session and not planned_system_failure:
                weather_reason = _weather_turn_audit_reason(
                    turn_row,
                    session_row=session_row,
                )
                if weather_reason is not None:
                    reasons.add(weather_reason)
                if (
                    turn_index == int(session_row["planned_error_turn"])
                    and str(turn_row["llm_route"] or "") != "weather"
                ):
                    required_routes.add("chat")
            elif not planned_system_failure:
                required_routes.add("chat")
            if (
                turn_index == int(session_row["planned_error_turn"])
                and str(session_row["error_type_id"]) != "system_failure"
            ):
                required_routes.add("evaluator")
            if any(
                (turn_index, route) not in successful_external_evidence
                for route in required_routes
            ):
                reasons.add("external_api_evidence_missing")

    flag_rows: list[sqlite3.Row] = []
    if session_rows:
        session_ids = [int(row["id"]) for row in session_rows]
        placeholders = ", ".join("?" for _ in session_ids)
        flag_rows = conn.execute(
            f"""
            SELECT DISTINCT f.flag
            FROM session_risk_flags f
            WHERE f.session_id IN ({placeholders})
              AND f.flag IN ({", ".join("?" for _ in EXTERNAL_RISK_FLAGS | OTHER_RISK_FLAGS)})
            """,
            (*session_ids, *sorted(EXTERNAL_RISK_FLAGS | OTHER_RISK_FLAGS)),
        ).fetchall()
    for flag_row in flag_rows:
        flag = str(flag_row["flag"])
        reasons.add("external_api_failure" if flag in EXTERNAL_RISK_FLAGS else flag)

    has_failed_external_attempt = (
        session_rows
        and HealthRepository(conn).has_failed_attempt_for_sessions(
            session_ids=[int(row["id"]) for row in session_rows]
        )
    )
    if has_failed_external_attempt:
        reasons.add("external_api_failure")

    if not reasons:
        audit_status = "eligible"
    elif reasons == {"external_api_evidence_missing"}:
        audit_status = "review_needed"
    else:
        audit_status = "excluded"
    return CleanDataAuditResult(status=audit_status, reasons=sorted(reasons))


def persist_clean_data_audit(
    conn: sqlite3.Connection,
    participant_id: int,
    result: CleanDataAuditResult,
) -> None:
    attempt_row = conn.execute(
        "SELECT current_attempt_id FROM participants WHERE id = ?",
        (participant_id,),
    ).fetchone()
    attempt_id = int(attempt_row["current_attempt_id"]) if attempt_row and attempt_row["current_attempt_id"] is not None else None
    conn.execute(
        """
        INSERT INTO clean_data_audits (
            participant_id,
            attempt_id,
            status,
            reasons_json,
            computed_at
        ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(participant_id, attempt_id)
        DO UPDATE SET
            status = excluded.status,
            reasons_json = excluded.reasons_json,
            computed_at = CURRENT_TIMESTAMP
        """,
        (
            participant_id,
            attempt_id,
            result.status,
            json.dumps(result.reasons, ensure_ascii=False),
        ),
    )


def refresh_participant_clean_data_audit(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    participant_id: int,
) -> CleanDataAuditResult:
    result = audit_participant_clean_data(
        conn,
        settings=settings,
        participant_id=participant_id,
    )
    persist_clean_data_audit(
        conn,
        participant_id=participant_id,
        result=result,
    )
    return result
