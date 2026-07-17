from __future__ import annotations

import csv
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from tempfile import mkstemp, TemporaryDirectory
from typing import Any, Iterable, Iterator, Sequence
import unicodedata
import zipfile

from backend.app.security import mask_phone
from backend.app.services.clean_data import read_audio_evidence
from backend.app.services.file_naming import safe_filename_component
from backend.app.services.providers import normalize_provider_error_code
from backend.app.settings import Settings
from backend.app.time_utils import shanghai_date_from_timestamp


CSV_EXPORTS = {
    "participants.csv",
    "sessions.csv",
    "turns.csv",
    "ratings.csv",
    "api_call_logs.csv",
    "integrated.csv",
    "reimbursement.csv",
}
JSONL_EXPORTS = {
    "artifacts.jsonl",
    "pretest_responses.jsonl",
}
CSV_FIELDNAMES = {
    "participants.csv": [
        "participant_id",
        "attempt_id",
        "attempt_no",
        "source_attempt_id",
        "attempt_status",
        "export_role",
        "export_day_scope",
        "participant_type",
        "condition",
        "subcondition",
        "topic_key",
        "error_type_id",
        "target_days",
        "current_status",
        "blocked_reason",
        "created_at",
        "updated_at",
    ],
    "sessions.csv": [
        "session_id",
        "participant_id",
        "attempt_id",
        "source_attempt_id",
        "participant_day_id",
        "session_uuid",
        "condition",
        "subcondition",
        "topic_key",
        "scenario_id",
        "agent_graph_version",
        "error_type_id",
        "planned_error_turn",
        "status",
        "manipulation_status",
        "started_at",
        "completed_at",
        "client_info_json",
        "is_test",
        "valid_for_export",
        "export_role",
        "export_day_scope",
        "export_scope_note",
        "created_at",
        "updated_at",
    ],
    "turns.csv": [
        "turn_id",
        "session_id",
        "turn_index",
        "user_text",
        "user_input_mode",
        "user_audio_path",
        "user_audio_sha256",
        "asr_provider",
        "asr_status",
        "asr_text",
        "asr_latency_ms",
        "assistant_text",
        "response_latency_ms",
        "client_message_sent_at",
        "assistant_render_completed_at",
        "client_response_latency_ms",
        "client_timing_interrupted",
        "render_timing_received_at",
        "llm_provider",
        "llm_model",
        "llm_route",
        "llm_attempts_json",
        "error_planned",
        "error_type_id",
        "error_presented",
        "error_presentation",
        "error_evaluator_provider",
        "error_evaluator_model",
        "error_evaluator_result_json",
        "error_mutation_json",
        "error_semantic_attempt_count",
        "error_failure_reason",
        "error_attempts_json",
        "agent_state_json",
        "created_at",
    ],
    "ratings.csv": [
        "rating_id",
        "turn_id",
        "stance_score",
        "trust_score",
        "submitted_at",
        "client_elapsed_ms",
    ],
    "api_call_logs.csv": [
        "api_call_log_id",
        "request_id",
        "session_id",
        "turn_index",
        "is_test",
        "route",
        "provider",
        "model",
        "status",
        "http_status",
        "error_code",
        "error_message_summary",
        "latency_ms",
        "cooldown_applied",
        "created_at",
    ],
    "reimbursement.csv": [
        "name",
        "phone",
        "id_number",
        "target_days",
        "completed_days",
    ],
    "integrated.csv": [
        "json_file",
        "audio_file",
        "participant_id",
        "attempt_id",
        "participant_type",
        "day",
        "turn",
        "session_id",
        "user_text",
        "assistant_text",
        "llm_provider",
        "llm_model",
        "llm_route",
        "stance_score",
        "trust_score",
    ],
}
EXPORT_MEMBER_NAMES = (
    "participants.csv",
    "sessions.csv",
    "turns.csv",
    "ratings.csv",
    "artifacts.jsonl",
    "api_call_logs.csv",
    "pretest_responses.jsonl",
)
REIMBURSEMENT_MEMBER_NAME = "reimbursement.csv"
INTERNAL_TEST_PHONE_HASH = "test-channel"
INTERNAL_TEST_PARTICIPANT_NAME = "__test_channel__"
PRETEST_IDENTITY_KEYS = frozenset(
    {
        "idnumber",
        "id_number",
        "name",
        "phone",
        "reimbursement_id",
        "reimbursementid",
    }
)
UNTRUSTED_EXPORT_FIELDS = {
    "sessions.csv": frozenset({"client_info_json", "export_scope_note"}),
    "turns.csv": frozenset(
        {
            "user_text",
            "asr_text",
            "assistant_text",
            "llm_attempts_json",
            "error_evaluator_result_json",
            "agent_state_json",
        }
    ),
    "artifacts.jsonl": frozenset({"payload"}),
    "api_call_logs.csv": frozenset({"error_message_summary"}),
}


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    include_test: bool
    generated_at: str
    row_counts: dict[str, int]


class ExportEvidenceError(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class IdentityRedactor:
    casefold_literals: tuple[str, ...]
    phone_patterns: tuple[re.Pattern[str], ...]

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            normalized = unicodedata.normalize("NFKC", value)
            spans = _casefold_literal_spans(
                normalized,
                literals=self.casefold_literals,
            )
            for pattern in self.phone_patterns:
                spans.extend(match.span() for match in pattern.finditer(normalized))
            return _replace_text_spans(normalized, spans=spans)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value


def _casefold_literal_spans(
    value: str,
    *,
    literals: Sequence[str],
) -> list[tuple[int, int]]:
    folded_parts: list[str] = []
    folded_source_indexes: list[int] = []
    for source_index, character in enumerate(value):
        folded_character = character.casefold()
        folded_parts.append(folded_character)
        folded_source_indexes.extend(source_index for _ in folded_character)
    folded_value = "".join(folded_parts)

    spans: list[tuple[int, int]] = []
    for literal in literals:
        search_start = 0
        while True:
            folded_start = folded_value.find(literal, search_start)
            if folded_start < 0:
                break
            folded_end = folded_start + len(literal)
            spans.append(
                (
                    folded_source_indexes[folded_start],
                    folded_source_indexes[folded_end - 1] + 1,
                )
            )
            search_start = folded_start + 1
    return spans


def _replace_text_spans(value: str, *, spans: Sequence[tuple[int, int]]) -> str:
    if not spans:
        return value
    merged_spans: list[tuple[int, int]] = []
    for start, end in sorted(set(spans)):
        if merged_spans and start <= merged_spans[-1][1]:
            previous_start, previous_end = merged_spans[-1]
            merged_spans[-1] = (previous_start, max(previous_end, end))
        else:
            merged_spans.append((start, end))

    parts: list[str] = []
    cursor = 0
    for start, end in merged_spans:
        parts.extend((value[cursor:start], "[REDACTED]"))
        cursor = end
    parts.append(value[cursor:])
    return "".join(parts)


@contextmanager
def _atomic_archive_path(output_path: Path) -> Iterator[Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_descriptor, temporary_name = mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(temporary_descriptor)
    temporary_path = Path(temporary_name)
    try:
        yield temporary_path
        with zipfile.ZipFile(temporary_path) as completed_archive:
            if completed_archive.testzip() is not None:
                raise zipfile.BadZipFile("Export archive validation failed.")
        os.replace(temporary_path, output_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass


@dataclass(frozen=True)
class ExportDateRange:
    start_date: str | None = None
    end_date: str | None = None


def normalize_export_filters(filters: dict[str, object] | None) -> ExportDateRange:
    filters = filters or {}
    start_date = _normalize_date_filter(filters.get("start_date"))
    end_date = _normalize_date_filter(filters.get("end_date"))
    if start_date is not None and end_date is not None and start_date > end_date:
        raise ValueError("Export start_date must be on or before end_date.")
    return ExportDateRange(start_date=start_date, end_date=end_date)


def _normalize_date_filter(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    try:
        datetime.strptime(normalized, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Export date filters must use YYYY-MM-DD.") from exc
    return normalized


def create_v2_export(
    conn: sqlite3.Connection,
    settings: Settings,
    output_path: Path,
    include_test: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ExportResult:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _atomic_archive_path(output_path) as temporary_output_path:
        payload = build_export_payload(
            conn,
            include_test=include_test,
            start_date=start_date,
            end_date=end_date,
        )
        with TemporaryDirectory(prefix="task13-export-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            for member_name in EXPORT_MEMBER_NAMES:
                rows = payload[member_name]
                target_path = tmp_root / member_name
                if member_name in CSV_EXPORTS:
                    _write_csv(target_path, rows=rows)
                else:
                    _write_jsonl(target_path, rows=rows)

            with zipfile.ZipFile(
                temporary_output_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for member_name in EXPORT_MEMBER_NAMES:
                    archive.write(tmp_root / member_name, arcname=member_name)
                _write_interface_export_members(
                    conn,
                    settings=settings,
                    archive=archive,
                    tmp_root=tmp_root,
                    include_test=include_test,
                    start_date=start_date,
                    end_date=end_date,
                )

    return ExportResult(
        output_path=output_path,
        include_test=include_test,
        generated_at=generated_at,
        row_counts={member_name: len(payload[member_name]) for member_name in EXPORT_MEMBER_NAMES},
    )


def create_clean_data_export(
    conn: sqlite3.Connection,
    settings: Settings,
    output_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ExportResult:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _atomic_archive_path(output_path) as temporary_output_path:
        effective_attempt_ids = _select_clean_data_attempt_ids(conn)
        payload = build_clean_data_export_payload(
            conn,
            start_date=start_date,
            end_date=end_date,
        )

        with TemporaryDirectory(prefix="task13-export-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            for member_name in EXPORT_MEMBER_NAMES:
                rows = payload[member_name]
                target_path = tmp_root / member_name
                if member_name in CSV_EXPORTS:
                    _write_csv(target_path, rows=rows)
                else:
                    _write_jsonl(target_path, rows=rows)

            with zipfile.ZipFile(
                temporary_output_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for member_name in EXPORT_MEMBER_NAMES:
                    archive.write(tmp_root / member_name, arcname=member_name)
                _write_interface_export_members(
                    conn,
                    settings=settings,
                    archive=archive,
                    tmp_root=tmp_root,
                    include_test=False,
                    effective_attempt_ids=effective_attempt_ids,
                    start_date=start_date,
                    end_date=end_date,
                )

    return ExportResult(
        output_path=output_path,
        include_test=False,
        generated_at=generated_at,
        row_counts={member_name: len(payload[member_name]) for member_name in EXPORT_MEMBER_NAMES},
    )


def create_reimbursement_export(
    conn: sqlite3.Connection,
    settings: Settings,
    output_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> ExportResult:
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    with _atomic_archive_path(output_path) as temporary_output_path:
        rows = build_reimbursement_export_rows(
            conn,
            start_date=start_date,
            end_date=end_date,
        )

        with TemporaryDirectory(prefix="task13-export-") as tmp_dir:
            tmp_root = Path(tmp_dir)
            target_path = tmp_root / REIMBURSEMENT_MEMBER_NAME
            _write_csv(target_path, rows=rows)

            with zipfile.ZipFile(
                temporary_output_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.write(target_path, arcname=REIMBURSEMENT_MEMBER_NAME)

    return ExportResult(
        output_path=output_path,
        include_test=False,
        generated_at=generated_at,
        row_counts={REIMBURSEMENT_MEMBER_NAME: len(rows)},
    )


def build_export_payload(
    conn: sqlite3.Connection,
    *,
    include_test: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    return _build_export_payload(
        conn,
        include_test=include_test,
        effective_attempt_ids=None,
        start_date=start_date,
        end_date=end_date,
    )


def build_clean_data_export_payload(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, list[dict[str, Any]]]:
    effective_attempt_ids = _select_clean_data_attempt_ids(conn)
    return _build_export_payload(
        conn,
        include_test=False,
        effective_attempt_ids=effective_attempt_ids,
        start_date=start_date,
        end_date=end_date,
    )


def _select_clean_data_attempt_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        int(row["attempt_id"])
        for row in conn.execute(
            """
            SELECT attempt_id
            FROM clean_data_audits
            WHERE status = 'eligible'
              AND attempt_id IS NOT NULL
            ORDER BY attempt_id
            """
        ).fetchall()
    ]


def build_reimbursement_export_rows(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    effective_attempt_rows = [
        row
        for row in _select_effective_attempt_rows(conn, effective_attempt_ids=None)
        if row["phone_hash"] != INTERNAL_TEST_PHONE_HASH
        and row["name"] != INTERNAL_TEST_PARTICIPANT_NAME
    ]
    raw_session_rows = _select_effective_export_sessions(
        conn,
        include_test=False,
        effective_attempt_rows=effective_attempt_rows,
        start_date=start_date,
        end_date=end_date,
    )
    completed_day_ids_by_attempt: dict[int, set[int]] = {}
    for session_row in raw_session_rows:
        completed_day_ids_by_attempt.setdefault(
            int(session_row["effective_attempt_id"]),
            set(),
        ).add(int(session_row["participant_day_id"]))

    rows: list[dict[str, Any]] = []
    for attempt_row in effective_attempt_rows:
        attempt_id = int(attempt_row["attempt_id"])
        completed_days = len(completed_day_ids_by_attempt.get(attempt_id, set()))
        target_days = int(attempt_row["target_days"])
        if completed_days < target_days:
            continue
        rows.append(
            {
                "name": attempt_row["name"],
                "phone": attempt_row["phone"],
                "id_number": _extract_reimbursement_id_number(
                    conn,
                    participant_id=int(attempt_row["participant_id"]),
                    effective_attempt_id=attempt_id,
                    source_attempt_id=_optional_int(attempt_row["source_attempt_id"]),
                )
                or "",
                "target_days": target_days,
                "completed_days": completed_days,
            }
        )
    return rows


def _build_export_payload(
    conn: sqlite3.Connection,
    *,
    include_test: bool,
    effective_attempt_ids: Sequence[int] | None,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, list[dict[str, Any]]]:
    effective_attempt_rows = _select_effective_attempt_rows(
        conn,
        effective_attempt_ids=effective_attempt_ids,
    )
    raw_session_rows = _select_effective_export_sessions(
        conn,
        include_test=include_test,
        effective_attempt_rows=effective_attempt_rows,
        start_date=start_date,
        end_date=end_date,
    )
    session_rows = [_serialize_session_row(row) for row in raw_session_rows]
    exported_attempt_ids = sorted({int(row["effective_attempt_id"]) for row in raw_session_rows})
    session_ids = sorted({int(row["session_id"]) for row in session_rows})
    session_uuid_lookup = {
        int(row["session_id"]): str(row["session_uuid"])
        for row in session_rows
    }

    participants = _select_participants(conn, attempt_ids=exported_attempt_ids)
    turns = _select_turns(conn, session_ids=session_ids)
    turn_ids = sorted({int(row["turn_id"]) for row in turns})
    ratings = _select_ratings(conn, turn_ids=turn_ids)
    artifacts = _select_artifacts(
        conn,
        turn_ids=turn_ids,
        session_uuid_lookup=session_uuid_lookup,
    )
    api_call_logs = _select_api_call_logs(
        conn,
        session_rows=session_rows,
    )
    pretest_responses = _select_pretest_responses(
        conn,
        effective_attempt_rows=[
            row for row in effective_attempt_rows if int(row["attempt_id"]) in exported_attempt_ids
        ],
    )
    turns = _project_turn_audio_paths(turns, raw_session_rows=raw_session_rows)
    payload = {
        "participants.csv": participants,
        "sessions.csv": session_rows,
        "turns.csv": turns,
        "ratings.csv": ratings,
        "artifacts.jsonl": artifacts,
        "api_call_logs.csv": api_call_logs,
        "pretest_responses.jsonl": pretest_responses,
    }
    identity_redactor = _identity_redactor_for_attempt_rows(
        conn,
        effective_attempt_rows,
    )
    return {
        member_name: [
            _redact_untrusted_export_fields(
                row,
                field_names=UNTRUSTED_EXPORT_FIELDS.get(member_name, frozenset()),
                identity_redactor=identity_redactor,
            )
            for row in rows
        ]
        for member_name, rows in payload.items()
    }


def _write_csv(path: Path, *, rows: Sequence[dict[str, Any]]) -> None:
    fieldnames = CSV_FIELDNAMES[path.name]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(_normalize_csv_row(row))


def _normalize_csv_row(row: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if value is None:
            normalized[key] = ""
        elif isinstance(value, bool):
            normalized[key] = "1" if value else "0"
        elif isinstance(value, (dict, list)):
            normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            normalized[key] = str(value)
    return normalized


def _write_jsonl(path: Path, *, rows: Sequence[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def _write_interface_export_members(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    archive: zipfile.ZipFile,
    tmp_root: Path,
    include_test: bool,
    effective_attempt_ids: Sequence[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    effective_attempt_rows = _select_effective_attempt_rows(
        conn,
        effective_attempt_ids=effective_attempt_ids,
    )
    identity_redactor = _identity_redactor_for_attempt_rows(
        conn,
        effective_attempt_rows,
    )
    raw_session_rows = _select_effective_export_sessions(
        conn,
        include_test=include_test,
        effective_attempt_rows=effective_attempt_rows,
        start_date=start_date,
        end_date=end_date,
    )
    if not raw_session_rows:
        integrated_path = tmp_root / "integrated.csv"
        _write_csv(integrated_path, rows=[])
        archive.write(integrated_path, arcname="integrated.csv")
        return

    effective_attempt_by_id = {
        int(row["attempt_id"]): row for row in effective_attempt_rows
    }
    session_by_id = {int(row["session_id"]): row for row in raw_session_rows}
    turn_rows = _select_interface_turn_rows(
        conn,
        session_ids=sorted(session_by_id),
    )

    integrated_path = tmp_root / "integrated.csv"
    json_dir = tmp_root / "json"
    json_dir.mkdir(parents=True, exist_ok=True)
    with integrated_path.open("w", encoding="utf-8", newline="") as csv_handle:
        writer = csv.DictWriter(
            csv_handle,
            fieldnames=CSV_FIELDNAMES["integrated.csv"],
        )
        writer.writeheader()
        for turn_row in turn_rows:
            session_row = session_by_id[int(turn_row["session_id"])]
            effective_attempt = effective_attempt_by_id[
                int(session_row["effective_attempt_id"])
            ]
            participant_type = str(effective_attempt["participant_type"])
            participant_export_id = _participant_export_id(turn_row["participant_id"])
            attempt_export_id = _attempt_export_id(
                session_row["effective_attempt_id"]
            )
            stem = _interface_member_stem(
                participant_export_id=participant_export_id,
                attempt_export_id=attempt_export_id,
                participant_type=participant_type,
                day_index=int(turn_row["day_index"]),
                turn_index=int(turn_row["turn_index"]),
                session_id=int(turn_row["session_id"]),
            )
            json_member = f"json/{stem}.json"
            audio_member = _write_interface_audio_member(
                settings=settings,
                archive=archive,
                turn_row=turn_row,
                stem=stem,
            )
            json_payload = _build_interface_turn_json(
                turn_row=turn_row,
                participant_export_id=participant_export_id,
                attempt_export_id=attempt_export_id,
                participant_type=participant_type,
                audio_member=audio_member,
                identity_redactor=identity_redactor,
            )
            json_path = json_dir / f"{stem}.json"
            json_path.write_text(
                json.dumps(json_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            archive.write(json_path, arcname=json_member)
            writer.writerow(
                _normalize_csv_row(
                    {
                        "json_file": json_member,
                        "audio_file": audio_member,
                        "participant_id": participant_export_id,
                        "attempt_id": attempt_export_id,
                        "participant_type": participant_type,
                        "day": int(turn_row["day_index"]),
                        "turn": int(turn_row["turn_index"]),
                        "session_id": turn_row["session_uuid"],
                        "user_text": identity_redactor.redact(turn_row["user_text"]),
                        "assistant_text": identity_redactor.redact(
                            turn_row["assistant_text"]
                        ),
                        "llm_provider": turn_row["llm_provider"],
                        "llm_model": turn_row["llm_model"],
                        "llm_route": turn_row["llm_route"],
                        "stance_score": turn_row["stance_score"],
                        "trust_score": turn_row["trust_score"],
                    }
                )
            )
    archive.write(integrated_path, arcname="integrated.csv")


def _select_interface_turn_rows(
    conn: sqlite3.Connection,
    *,
    session_ids: Sequence[int],
) -> list[sqlite3.Row]:
    if not session_ids:
        return []
    return conn.execute(
        f"""
        SELECT
            t.id AS turn_id,
            t.session_id,
            t.turn_index,
            t.user_text,
            t.user_input_mode,
            t.user_audio_path,
            t.user_audio_sha256,
            t.assistant_text,
            t.llm_provider,
            t.llm_model,
            t.llm_route,
            t.created_at AS turn_created_at,
            r.stance_score,
            r.trust_score,
            r.submitted_at AS rating_submitted_at,
            s.session_uuid,
            s.scenario_id,
            s.topic_key,
            s.started_at,
            s.completed_at,
            d.day_index,
            p.id AS participant_id,
            p.name,
            p.phone
        FROM conversation_turns t
        JOIN experiment_sessions s ON s.id = t.session_id
        JOIN participant_days d ON d.id = s.participant_day_id
        JOIN participants p ON p.id = s.participant_id
        LEFT JOIN turn_ratings r ON r.turn_id = t.id
        WHERE t.session_id IN ({_placeholders(session_ids)})
        ORDER BY s.id, t.turn_index
        """,
        tuple(session_ids),
    ).fetchall()


def _write_interface_audio_member(
    *,
    settings: Settings,
    archive: zipfile.ZipFile,
    turn_row: sqlite3.Row,
    stem: str,
) -> str:
    source_path_value = turn_row["user_audio_path"]
    if str(turn_row["user_input_mode"]) != "voice" and not source_path_value:
        return ""
    evidence = read_audio_evidence(
        settings=settings,
        audio_path_value=source_path_value,
        stored_sha256=turn_row["user_audio_sha256"],
    )
    if evidence.reason is not None:
        raise ExportEvidenceError(evidence.reason)
    if evidence.data is None:
        raise ExportEvidenceError("audio_path_invalid")
    suffix = Path(str(source_path_value)).suffix or ".bin"
    audio_member = f"audio/{stem}{suffix}"
    archive.writestr(audio_member, evidence.data)
    return audio_member


def _build_interface_turn_json(
    *,
    turn_row: sqlite3.Row,
    participant_export_id: str,
    attempt_export_id: str,
    participant_type: str,
    audio_member: str,
    identity_redactor: IdentityRedactor,
) -> dict[str, Any]:
    user_message = {
        "speaker": "user",
        "text": identity_redactor.redact(turn_row["user_text"]),
        "timestamp": turn_row["turn_created_at"],
        "submittedAt": turn_row["turn_created_at"],
        "turn": int(turn_row["turn_index"]),
    }
    assistant_message = {
        "speaker": "ai",
        "text": identity_redactor.redact(turn_row["assistant_text"]),
        "timestamp": turn_row["turn_created_at"],
        "turn": int(turn_row["turn_index"]),
    }
    return {
        "participantId": participant_export_id,
        "attemptId": attempt_export_id,
        "participantType": participant_type,
        "day": int(turn_row["day_index"]),
        "turn": int(turn_row["turn_index"]),
        "sessionId": turn_row["session_uuid"],
        "audioFile": audio_member,
        "completedAt": turn_row["completed_at"],
        "trials": [
            {
                "trialId": turn_row["scenario_id"],
                "topic": turn_row["topic_key"],
                "type": "normal",
                "maxTurns": 5,
                "round": 1,
                "llmProvider": turn_row["llm_provider"],
                "llmModel": turn_row["llm_model"],
                "llmRoute": turn_row["llm_route"],
                "conversationHistory": [user_message, assistant_message],
                "turnRatings": [
                    {
                        "turn": int(turn_row["turn_index"]),
                        "perception": turn_row["stance_score"],
                        "trust": turn_row["trust_score"],
                        "submittedAt": turn_row["rating_submitted_at"],
                    }
                ],
                "startTime": turn_row["started_at"],
                "endTime": turn_row["completed_at"],
            }
        ],
    }


def _write_multi_member_export(
    *,
    output_path: Path,
    include_test: bool,
    generated_at: str,
    payload: dict[str, list[dict[str, Any]]],
    member_names: Sequence[str],
) -> ExportResult:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with TemporaryDirectory(prefix="task13-export-") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for member_name in member_names:
            rows = payload[member_name]
            target_path = tmp_root / member_name
            if member_name in CSV_EXPORTS:
                _write_csv(target_path, rows=rows)
            else:
                _write_jsonl(target_path, rows=rows)

        with zipfile.ZipFile(
            output_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for member_name in member_names:
                archive.write(tmp_root / member_name, arcname=member_name)

    return ExportResult(
        output_path=output_path,
        include_test=include_test,
        generated_at=generated_at,
        row_counts={member_name: len(payload[member_name]) for member_name in member_names},
    )


def _select_effective_attempt_rows(
    conn: sqlite3.Connection,
    *,
    effective_attempt_ids: Sequence[int] | None,
) -> list[dict[str, Any]]:
    where_parts = ["pa.valid_for_export = 1"]
    parameters: list[Any] = []
    from_sql = """
        FROM participants p
        JOIN participant_attempts pa ON pa.id = p.current_attempt_id
    """
    if effective_attempt_ids is not None:
        if not effective_attempt_ids:
            return []
        from_sql = """
            FROM participant_attempts pa
            JOIN participants p ON p.id = pa.participant_id
        """
        where_parts.append(f"pa.id IN ({_placeholders(effective_attempt_ids)})")
        parameters.extend(effective_attempt_ids)
    where_sql = " AND ".join(where_parts)
    rows = conn.execute(
        f"""
        SELECT
            p.id AS participant_id,
            p.name,
            p.phone,
            p.phone_hash,
            pa.status AS current_status,
            p.created_at,
            p.updated_at,
            pa.id AS attempt_id,
            pa.attempt_no,
            pa.participant_type,
            pa.condition,
            pa.subcondition,
            pa.topic_key,
            pa.error_type_id,
            pa.target_days,
            pa.status AS attempt_status,
            pa.source_attempt_id,
            pa.export_role,
            pa.blocked_reason,
            CASE
                WHEN pa.export_role = 'converted_short' THEN 'day_1_only'
                WHEN pa.participant_type = 'long' THEN 'all_completed_days'
                ELSE 'day_1_only'
            END AS export_day_scope
        {from_sql}
        WHERE {where_sql}
        ORDER BY pa.id
        """,
        tuple(parameters),
    ).fetchall()
    return [dict(row) for row in rows]


def _select_effective_export_sessions(
    conn: sqlite3.Connection,
    *,
    include_test: bool,
    effective_attempt_rows: Sequence[dict[str, Any]],
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    if not effective_attempt_rows:
        return []
    selected_rows: list[dict[str, Any]] = []
    for attempt_row in effective_attempt_rows:
        attempt_id = int(attempt_row["attempt_id"])
        export_role = str(attempt_row["export_role"])
        source_attempt_id = _optional_int(attempt_row["source_attempt_id"])
        session_attempt_id = source_attempt_id if export_role == "converted_short" else attempt_id
        if session_attempt_id is None:
            continue
        parameters: list[Any] = [int(attempt_row["participant_id"]), session_attempt_id]
        where_parts = [
            "s.participant_id = ?",
            "s.attempt_id = ?",
            "s.status = 'completed'",
            "s.valid_for_export = 1",
            "d.status = 'completed'",
            "d.valid_for_export = 1",
        ]
        if not include_test:
            where_parts.append("s.is_test = 0")
        if str(attempt_row["export_day_scope"]) == "day_1_only":
            where_parts.append("d.day_index = 1")
        rows = conn.execute(
            f"""
            SELECT
                s.id AS session_id,
                s.participant_id,
                s.attempt_id AS source_attempt_id,
                s.participant_day_id,
                s.session_uuid,
                s.condition,
                s.subcondition,
                s.topic_key,
                s.scenario_id,
                s.agent_graph_version,
                s.error_type_id,
                s.planned_error_turn,
                s.status,
                s.manipulation_status,
                s.started_at,
                s.completed_at,
                s.client_info_json,
                s.is_test,
                s.valid_for_export,
                s.export_scope_note,
                s.created_at,
                s.updated_at,
                ? AS export_role,
                ? AS export_day_scope,
                ? AS effective_attempt_id
            FROM experiment_sessions s
            JOIN participant_days d ON d.id = s.participant_day_id
            WHERE {' AND '.join(where_parts)}
            ORDER BY s.id
            """,
            (
                str(attempt_row["export_role"]),
                str(attempt_row["export_day_scope"]),
                attempt_id,
                *parameters,
            ),
        ).fetchall()
        for row in rows:
            session_row = dict(row)
            if _session_matches_date_range(
                session_row,
                start_date=start_date,
                end_date=end_date,
            ):
                selected_rows.append(session_row)
    selected_rows.sort(key=lambda row: int(row["session_id"]))
    return selected_rows


def _session_matches_date_range(
    row: dict[str, Any],
    *,
    start_date: str | None,
    end_date: str | None,
) -> bool:
    if start_date is None and end_date is None:
        return True
    try:
        reporting_date = shanghai_date_from_timestamp(row["created_at"])
    except ValueError:
        raise ValueError(
            f"Session {row['session_uuid']} has an invalid created_at timestamp."
        ) from None
    return (start_date is None or reporting_date >= start_date) and (
        end_date is None or reporting_date <= end_date
    )


def _serialize_session_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": int(row["session_id"]),
        "participant_id": _participant_export_id(row["participant_id"]),
        "attempt_id": _attempt_export_id(row["effective_attempt_id"]),
        "source_attempt_id": _attempt_export_id(row["source_attempt_id"]),
        "participant_day_id": int(row["participant_day_id"]),
        "session_uuid": row["session_uuid"],
        "condition": row["condition"],
        "subcondition": row["subcondition"],
        "topic_key": row["topic_key"],
        "scenario_id": row["scenario_id"],
        "agent_graph_version": row["agent_graph_version"],
        "error_type_id": row["error_type_id"],
        "planned_error_turn": int(row["planned_error_turn"]),
        "status": row["status"],
        "manipulation_status": row["manipulation_status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "client_info_json": _json_loads(row["client_info_json"]),
        "is_test": bool(row["is_test"]),
        "valid_for_export": bool(row["valid_for_export"]),
        "export_role": row["export_role"],
        "export_day_scope": row["export_day_scope"],
        "export_scope_note": row["export_scope_note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _select_participants(
    conn: sqlite3.Connection,
    *,
    attempt_ids: Sequence[int],
) -> list[dict[str, Any]]:
    if not attempt_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT
            p.id AS participant_id,
            pa.id AS attempt_id,
            pa.attempt_no,
            pa.source_attempt_id,
            pa.status AS attempt_status,
            pa.export_role,
            CASE
                WHEN pa.export_role = 'converted_short' THEN 'day_1_only'
                WHEN pa.participant_type = 'long' THEN 'all_completed_days'
                ELSE 'day_1_only'
            END AS export_day_scope,
            p.name,
            p.phone,
            p.phone_hash,
            pa.participant_type,
            pa.condition,
            pa.subcondition,
            pa.topic_key,
            pa.error_type_id,
            pa.target_days,
            pa.status AS current_status,
            pa.blocked_reason,
            p.created_at,
            p.updated_at
        FROM participant_attempts pa
        JOIN participants p ON p.id = pa.participant_id
        WHERE pa.id IN ({_placeholders(attempt_ids)})
        ORDER BY pa.id
        """,
        tuple(attempt_ids),
    ).fetchall()
    return [
        {
            "participant_id": _participant_export_id(row["participant_id"]),
            "attempt_id": _attempt_export_id(row["attempt_id"]),
            "attempt_no": int(row["attempt_no"]),
            "source_attempt_id": _attempt_export_id(row["source_attempt_id"]),
            "attempt_status": row["attempt_status"],
            "export_role": row["export_role"],
            "export_day_scope": row["export_day_scope"],
            "participant_type": row["participant_type"],
            "condition": row["condition"],
            "subcondition": row["subcondition"],
            "topic_key": row["topic_key"],
            "error_type_id": row["error_type_id"],
            "target_days": int(row["target_days"]),
            "current_status": row["current_status"],
            "blocked_reason": row["blocked_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def _select_turns(
    conn: sqlite3.Connection,
    *,
    session_ids: Sequence[int],
) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT
            t.id AS turn_id,
            t.session_id,
            t.turn_index,
            t.user_text,
            t.user_input_mode,
            t.user_audio_path,
            t.user_audio_sha256,
            t.asr_provider,
            t.asr_status,
            t.asr_text,
            t.asr_latency_ms,
            t.assistant_text,
            t.response_latency_ms,
            t.client_message_sent_at,
            t.assistant_render_completed_at,
            t.client_response_latency_ms,
            t.client_timing_interrupted,
            t.render_timing_received_at,
            t.llm_provider,
            t.llm_model,
            t.llm_route,
            t.llm_attempts_json,
            t.error_planned,
            t.error_type_id,
            t.error_presented,
            t.error_presentation,
            t.error_evaluator_provider,
            t.error_evaluator_model,
            t.error_evaluator_result_json,
            t.error_mutation_json,
            t.error_semantic_attempt_count,
            t.error_failure_reason,
            t.error_attempts_json,
            t.agent_state_json,
            t.created_at
        FROM conversation_turns t
        WHERE t.session_id IN ({_placeholders(session_ids)})
        ORDER BY t.id
        """,
        tuple(session_ids),
    ).fetchall()
    return [
        {
            "turn_id": int(row["turn_id"]),
            "session_id": int(row["session_id"]),
            "turn_index": int(row["turn_index"]),
            "user_text": row["user_text"],
            "user_input_mode": row["user_input_mode"],
            "user_audio_path": row["user_audio_path"],
            "user_audio_sha256": row["user_audio_sha256"],
            "asr_provider": row["asr_provider"],
            "asr_status": row["asr_status"],
            "asr_text": row["asr_text"],
            "asr_latency_ms": row["asr_latency_ms"],
            "assistant_text": row["assistant_text"],
            "response_latency_ms": row["response_latency_ms"],
            "client_message_sent_at": row["client_message_sent_at"],
            "assistant_render_completed_at": row["assistant_render_completed_at"],
            "client_response_latency_ms": row["client_response_latency_ms"],
            "client_timing_interrupted": (
                None
                if row["client_timing_interrupted"] is None
                else bool(row["client_timing_interrupted"])
            ),
            "render_timing_received_at": row["render_timing_received_at"],
            "llm_provider": row["llm_provider"],
            "llm_model": row["llm_model"],
            "llm_route": row["llm_route"],
            "llm_attempts_json": _json_loads(row["llm_attempts_json"]),
            "error_planned": bool(row["error_planned"]),
            "error_type_id": row["error_type_id"],
            "error_presented": bool(row["error_presented"]),
            "error_presentation": row["error_presentation"],
            "error_evaluator_provider": row["error_evaluator_provider"],
            "error_evaluator_model": row["error_evaluator_model"],
            "error_evaluator_result_json": _json_loads(row["error_evaluator_result_json"]),
            "error_mutation_json": _json_loads(row["error_mutation_json"]),
            "error_semantic_attempt_count": int(row["error_semantic_attempt_count"]),
            "error_failure_reason": row["error_failure_reason"],
            "error_attempts_json": _json_loads(row["error_attempts_json"]),
            "agent_state_json": _json_loads(row["agent_state_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _select_ratings(
    conn: sqlite3.Connection,
    *,
    turn_ids: Sequence[int],
) -> list[dict[str, Any]]:
    if not turn_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT
            r.id AS rating_id,
            r.turn_id,
            r.stance_score,
            r.trust_score,
            r.submitted_at,
            r.client_elapsed_ms
        FROM turn_ratings r
        WHERE r.turn_id IN ({_placeholders(turn_ids)})
        ORDER BY r.id
        """,
        tuple(turn_ids),
    ).fetchall()
    return [
        {
            "rating_id": int(row["rating_id"]),
            "turn_id": int(row["turn_id"]),
            "stance_score": int(row["stance_score"]),
            "trust_score": int(row["trust_score"]),
            "submitted_at": row["submitted_at"],
            "client_elapsed_ms": row["client_elapsed_ms"],
        }
        for row in rows
    ]


def _select_artifacts(
    conn: sqlite3.Connection,
    *,
    turn_ids: Sequence[int],
    session_uuid_lookup: dict[int, str],
) -> list[dict[str, Any]]:
    if not turn_ids:
        return []
    rows = conn.execute(
        f"""
        SELECT
            a.id AS artifact_id,
            a.turn_id,
            a.artifact_type,
            a.status,
            a.payload_json,
            a.visible_to_participant,
            a.created_at,
            t.session_id
        FROM task_artifacts a
        JOIN conversation_turns t ON t.id = a.turn_id
        WHERE a.turn_id IN ({_placeholders(turn_ids)})
        ORDER BY a.id
        """,
        tuple(turn_ids),
    ).fetchall()
    return [
        {
            "artifact_id": int(row["artifact_id"]),
            "turn_id": int(row["turn_id"]),
            "session_id": int(row["session_id"]),
            "session_uuid": session_uuid_lookup[int(row["session_id"])],
            "artifact_type": row["artifact_type"],
            "status": row["status"],
            "payload": _json_loads(row["payload_json"]),
            "visible_to_participant": bool(row["visible_to_participant"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _select_api_call_logs(
    conn: sqlite3.Connection,
    *,
    session_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not session_rows:
        return []
    session_ids = [int(row["session_id"]) for row in session_rows]

    rows = conn.execute(
        f"""
        SELECT
            l.id AS api_call_log_id,
            l.request_id,
            l.session_id,
            l.turn_index,
            l.is_test,
            l.route,
            l.provider,
            l.model,
            l.status,
            l.http_status,
            l.error_code,
            l.latency_ms,
            l.cooldown_applied,
            l.created_at
        FROM api_call_logs l
        WHERE l.session_id IN ({_placeholders(session_ids)})
        ORDER BY l.id
        """,
        tuple(session_ids),
    ).fetchall()
    return [
        {
            "api_call_log_id": int(row["api_call_log_id"]),
            "request_id": row["request_id"],
            "session_id": int(row["session_id"]),
            "turn_index": int(row["turn_index"]),
            "is_test": bool(row["is_test"]),
            "route": row["route"],
            "provider": row["provider"],
            "model": row["model"],
            "status": row["status"],
            "http_status": row["http_status"],
            "error_code": normalize_provider_error_code(
                status=row["status"],
                error_code=row["error_code"],
            ),
            "error_message_summary": _safe_api_error_summary(
                status=row["status"],
                http_status=row["http_status"],
                error_code=row["error_code"],
            ),
            "latency_ms": row["latency_ms"],
            "cooldown_applied": bool(row["cooldown_applied"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _safe_api_error_summary(
    *,
    status: str,
    http_status: int | None,
    error_code: str | None,
) -> str | None:
    if status == "success":
        return None
    parts = [status]
    if http_status is not None:
        parts.append(str(http_status))
    normalized_error_code = normalize_provider_error_code(
        status=status,
        error_code=error_code,
    )
    if normalized_error_code:
        parts.append(normalized_error_code)
    return ":".join(parts)


def _select_pretest_responses(
    conn: sqlite3.Connection,
    *,
    effective_attempt_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not effective_attempt_rows:
        return []
    attempt_ids = [int(row["attempt_id"]) for row in effective_attempt_rows]
    rows = conn.execute(
        f"""
        SELECT
            pr.id AS pretest_response_id,
            pr.participant_id,
            pr.attempt_id,
            pr.day_index,
            pr.status,
            pr.payload_json,
            pr.autosave_count,
            pr.last_saved_at,
            pr.submitted_at,
            pr.source_pretest_response_id,
            pr.created_at,
            pr.updated_at
        FROM pretest_responses pr
        WHERE pr.attempt_id IN ({_placeholders(attempt_ids)})
        ORDER BY pr.id
        """,
        tuple(attempt_ids),
    ).fetchall()
    exported_rows = [
        {
            "pretest_response_id": int(row["pretest_response_id"]),
            "participant_id": _participant_export_id(row["participant_id"]),
            "attempt_id": _attempt_export_id(row["attempt_id"]),
            "day_index": int(row["day_index"]),
            "status": row["status"],
            "payload": _sanitize_pretest_payload(_json_loads(row["payload_json"])),
            "autosave_count": int(row["autosave_count"]),
            "last_saved_at": row["last_saved_at"],
            "submitted_at": row["submitted_at"],
            "source_pretest_response_id": row["source_pretest_response_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]
    copied_final_attempt_ids = {
        int(row["attempt_id"])
        for row in rows
        if int(row["day_index"]) == 1 and str(row["status"]) == "final"
    }
    source_fallbacks: list[dict[str, Any]] = []
    for attempt_row in effective_attempt_rows:
        if str(attempt_row["export_role"]) != "converted_short":
            continue
        attempt_id = int(attempt_row["attempt_id"])
        source_attempt_id = _optional_int(attempt_row["source_attempt_id"])
        if attempt_id in copied_final_attempt_ids or source_attempt_id is None:
            continue
        fallback_rows = conn.execute(
            """
            SELECT
                pr.id AS pretest_response_id,
                pr.participant_id,
                pr.attempt_id,
                pr.day_index,
                pr.status,
                pr.payload_json,
                pr.autosave_count,
                pr.last_saved_at,
                pr.submitted_at,
                pr.source_pretest_response_id,
                pr.created_at,
                pr.updated_at
            FROM pretest_responses pr
            WHERE pr.attempt_id = ?
              AND pr.day_index = 1
              AND pr.status = 'final'
            ORDER BY pr.id
            """,
            (source_attempt_id,),
        ).fetchall()
        for row in fallback_rows:
            source_fallbacks.append(
                {
                    "pretest_response_id": int(row["pretest_response_id"]),
                    "participant_id": _participant_export_id(row["participant_id"]),
                    "attempt_id": _attempt_export_id(attempt_id),
                    "day_index": int(row["day_index"]),
                    "status": row["status"],
                    "payload": _sanitize_pretest_payload(
                        _json_loads(row["payload_json"])
                    ),
                    "autosave_count": int(row["autosave_count"]),
                    "last_saved_at": row["last_saved_at"],
                    "submitted_at": row["submitted_at"],
                    "source_pretest_response_id": row["source_pretest_response_id"]
                    or int(row["pretest_response_id"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            )
    return exported_rows + source_fallbacks


def _extract_reimbursement_id_number(
    conn: sqlite3.Connection,
    *,
    participant_id: int,
    effective_attempt_id: int,
    source_attempt_id: int | None,
) -> str | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM pretest_responses
        WHERE participant_id = ?
          AND attempt_id = ?
          AND status = 'final'
        ORDER BY id DESC
        LIMIT 1
        """,
        (participant_id, effective_attempt_id),
    ).fetchone()
    if row is None and source_attempt_id is not None:
        row = conn.execute(
            """
            SELECT payload_json
            FROM pretest_responses
            WHERE participant_id = ?
              AND attempt_id = ?
              AND day_index = 1
              AND status = 'final'
            ORDER BY id DESC
            LIMIT 1
            """,
            (participant_id, source_attempt_id),
        ).fetchone()
    if row is None:
        return None

    payload = _json_loads(row["payload_json"])
    if not isinstance(payload, dict):
        return None
    demographics = payload.get("demographics")
    if not isinstance(demographics, dict):
        return None

    id_number = demographics.get("idNumber")
    if id_number is None:
        return None
    id_number_text = str(id_number).strip()
    return id_number_text or None


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _participant_export_id(value: object) -> str:
    return f"participant-{int(value):08d}"


def _attempt_export_id(value: object | None) -> str | None:
    if value is None:
        return None
    return f"attempt-{int(value):08d}"


def _interface_member_stem(
    *,
    participant_export_id: str,
    attempt_export_id: str | None,
    participant_type: str,
    day_index: int,
    turn_index: int,
    session_id: int,
) -> str:
    type_component = "long" if participant_type == "long" else "short"
    return "_".join(
        (
            safe_filename_component(participant_export_id),
            safe_filename_component(attempt_export_id),
            type_component,
            "day",
            str(day_index),
            "turn",
            str(turn_index),
            f"session-{session_id:08d}",
        )
    )


def _sanitize_pretest_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_pretest_payload(item)
            for key, item in value.items()
            if str(key).casefold() not in PRETEST_IDENTITY_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_pretest_payload(item) for item in value]
    return value


def _identity_redactor_for_attempt_rows(
    conn: sqlite3.Connection,
    attempt_rows: Sequence[dict[str, Any]],
) -> IdentityRedactor:
    exact_values: set[str] = set()
    names: set[str] = set()
    phones: set[str] = set()
    for attempt_row in attempt_rows:
        phone = str(attempt_row["phone"] or "")
        name = str(attempt_row["name"] or "")
        if name:
            names.add(unicodedata.normalize("NFKC", name))
        if phone:
            phones.add(phone)
        for value in (
            mask_phone(phone),
            attempt_row["phone_hash"],
            _extract_reimbursement_id_number(
                conn,
                participant_id=int(attempt_row["participant_id"]),
                effective_attempt_id=int(attempt_row["attempt_id"]),
                source_attempt_id=_optional_int(attempt_row["source_attempt_id"]),
            ),
        ):
            normalized = unicodedata.normalize("NFKC", str(value or ""))
            if normalized:
                exact_values.add(normalized)

    casefold_literals = tuple(
        sorted(
            {
                value.casefold()
                for value in (*names, *exact_values)
                if value.casefold()
            },
            key=len,
            reverse=True,
        )
    )
    phone_patterns: list[re.Pattern[str]] = []
    for phone in sorted(phones, key=len, reverse=True):
        separated_phone = r"[\s-]*".join(re.escape(digit) for digit in phone)
        phone_patterns.append(
            re.compile(
                rf"(?<![0-9])(?:(?:\+|00)86[\s-]*)?{separated_phone}(?![0-9])"
            )
        )
    return IdentityRedactor(
        casefold_literals=casefold_literals,
        phone_patterns=tuple(phone_patterns),
    )


def _redact_untrusted_export_fields(
    row: dict[str, Any],
    *,
    field_names: frozenset[str],
    identity_redactor: IdentityRedactor,
) -> dict[str, Any]:
    projected = dict(row)
    for field_name in field_names:
        if field_name in projected:
            projected[field_name] = identity_redactor.redact(projected[field_name])
    return projected


def _project_turn_audio_paths(
    turn_rows: Sequence[dict[str, Any]],
    *,
    raw_session_rows: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    session_by_id = {
        int(session_row["session_id"]): session_row
        for session_row in raw_session_rows
    }
    projected_rows: list[dict[str, Any]] = []
    for turn_row in turn_rows:
        projected_row = dict(turn_row)
        source_path = turn_row.get("user_audio_path")
        if source_path:
            session_row = session_by_id[int(turn_row["session_id"])]
            suffix = Path(str(source_path)).suffix or ".bin"
            projected_row["user_audio_path"] = str(
                Path("audio")
                / (
                    f"{_participant_export_id(session_row['participant_id'])}_"
                    f"{_attempt_export_id(session_row['effective_attempt_id'])}_"
                    f"session-{int(session_row['session_id']):08d}_"
                    f"turn-{int(turn_row['turn_index']):02d}{suffix}"
                )
            )
        projected_rows.append(projected_row)
    return projected_rows


def _placeholders(values: Iterable[object]) -> str:
    return ", ".join("?" for _ in values)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)
