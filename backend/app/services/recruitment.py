from __future__ import annotations

import sqlite3

from backend.app.settings import Settings


class RecruitmentClosedError(RuntimeError):
    pass


def recruitment_is_open(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
) -> bool:
    if settings.recruitment_test_override_open and not settings.is_production:
        return True
    row = conn.execute(
        "SELECT status FROM recruitment_control WHERE id = 1"
    ).fetchone()
    return row is not None and str(row["status"]) == "open"


def recruitment_status(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
) -> dict[str, object]:
    is_open = recruitment_is_open(conn, settings=settings)
    return {
        "status": "open" if is_open else "closed",
        "accepting_new_participants": is_open,
    }


def set_recruitment_status(
    conn: sqlite3.Connection,
    *,
    admin_user: str,
    is_open: bool,
) -> bool:
    desired = "open" if is_open else "closed"
    row = conn.execute(
        "SELECT status FROM recruitment_control WHERE id = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("Recruitment control is unavailable.")
    if str(row["status"]) == desired:
        return False
    conn.execute(
        """
        UPDATE recruitment_control
        SET status = ?,
            updated_by = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """,
        (desired, admin_user),
    )
    return True
