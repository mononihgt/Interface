from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db import (
    ReadOnlyDatabaseError,
    get_connection,
    get_read_only_connection,
    run_migrations,
)
from backend.app.services.cleanup_attempts import (
    apply_attempt_cleanup,
    CleanupReconciliationError,
    plan_attempt_cleanup,
    reconcile_cleanup_operations,
)
from backend.app.settings import get_settings
from backend.app.time_utils import current_shanghai_date


def _parse_iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO date: {value!r} (expected YYYY-MM-DD)"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert long-term missed-day attempts into completed short attempts.",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="Apply database and audio cleanup changes.",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the cleanup plan without writing changes. This is the default.",
    )
    parser.add_argument(
        "--today",
        type=_parse_iso_date,
        help="Override the Asia/Shanghai calendar date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    today = args.today or current_shanghai_date()
    mode = "apply" if args.apply else "dry_run"

    try:
        conn = get_connection(settings) if args.apply else get_read_only_connection(settings)
    except (FileNotFoundError, ReadOnlyDatabaseError) as exc:
        payload = {
            "mode": mode,
            "today": today,
            "error": str(exc),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1

    try:
        if args.apply:
            run_migrations(conn)
            reconcile_cleanup_operations(conn, data_dir=settings.data_dir)
        plan = plan_attempt_cleanup(
            conn,
            today=today,
            data_dir=settings.data_dir,
        )
        if args.apply:
            summary = apply_attempt_cleanup(
                conn,
                plan=plan,
                data_dir=settings.data_dir,
            )
        else:
            summary = None
    except CleanupReconciliationError as exc:
        payload = {
            "mode": mode,
            "today": today,
            "error": "cleanup_reconciliation_failed",
            "operations": exc.operations,
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()

    payload: dict[str, object] = {
        "mode": mode,
        "today": today,
        "scanned_attempts": plan.scanned_attempts,
        "planned_converted_attempts": len(plan.convertible_attempts),
        "converted_attempts": 0 if summary is None else summary.converted_attempts,
        "deleted_sessions": 0 if summary is None else summary.deleted_sessions,
        "deleted_audio_files": 0 if summary is None else summary.deleted_audio_files,
        "skipped": plan.skipped if summary is None else summary.skipped,
        "failed_audio_paths": [] if summary is None else summary.failed_audio_paths,
    }
    if plan.convertible_attempts:
        payload["convertible_attempts"] = [
            {
                "participant_id": attempt.participant_id,
                "source_attempt_id": attempt.source_attempt_id,
                "missed_day_indexes": attempt.missed_day_indexes,
            }
            for attempt in plan.convertible_attempts
        ]

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(
            f"{mode}: today={today} scanned={payload['scanned_attempts']} "
            f"planned={payload['planned_converted_attempts']} "
            f"converted={payload['converted_attempts']} "
            f"deleted_sessions={payload['deleted_sessions']} "
            f"deleted_audio={payload['deleted_audio_files']}"
        )
        if payload["skipped"]:
            print(f"skipped={json.dumps(payload['skipped'], ensure_ascii=False)}")
        if payload["failed_audio_paths"]:
            print(
                "failed_audio_paths="
                + json.dumps(payload["failed_audio_paths"], ensure_ascii=False)
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
