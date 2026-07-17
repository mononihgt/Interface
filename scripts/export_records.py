from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.app.db import get_connection, run_migrations
from backend.app.models.domain import EXPORT_TYPES
from backend.app.services.export import (
    create_clean_data_export,
    create_reimbursement_export,
    create_v2_export,
)
from backend.app.settings import get_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export sanitized v2 experiment records as a zip archive.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/exports/latest.zip"),
        help="Archive path to write. Default: data/exports/latest.zip",
    )
    parser.add_argument(
        "--include-test",
        action="store_true",
        help="Include test-session rows in the export.",
    )
    parser.add_argument(
        "--export-type",
        choices=list(EXPORT_TYPES),
        default="experiment_data",
        help="Export variant to generate. Default: experiment_data.",
    )
    args = parser.parse_args()
    if args.export_type == "reimbursement" and args.include_test:
        parser.error("reimbursement exports do not support --include-test.")
    return args


def main() -> int:
    args = parse_args()
    settings = get_settings()
    output_path = args.output
    if not output_path.is_absolute():
        output_path = PROJECT_ROOT / output_path

    conn = get_connection(settings)
    try:
        run_migrations(conn)
        if args.export_type == "experiment_data":
            result = create_v2_export(
                conn,
                settings,
                output_path,
                include_test=args.include_test,
            )
        elif args.export_type == "complete_no_external_error_data":
            result = create_clean_data_export(
                conn,
                settings,
                output_path,
            )
        else:
            result = create_reimbursement_export(conn, settings, output_path)
    finally:
        conn.close()

    if args.export_type == "reimbursement":
        size_summary = f"rows={result.row_counts['reimbursement.csv']}"
    else:
        size_summary = (
            f"sessions={result.row_counts['sessions.csv']}, "
            f"turns={result.row_counts['turns.csv']}"
        )

    print(
        f"exported {result.output_path} "
        f"(export_type={args.export_type}, "
        f"include_test={str(result.include_test).lower()}, "
        f"{size_summary})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
