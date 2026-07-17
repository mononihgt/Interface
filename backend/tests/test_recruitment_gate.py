from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.db import get_connection
from backend.app.main import create_app
from backend.app.settings import Settings


ADMIN_PASSWORD = "publication-admin-password"
ADMIN_SALT = "publication-gate-salt"


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


def _settings(tmp_path: Path, *, app_env: str, test_override: bool = False) -> Settings:
    return Settings(
        app_env=app_env,
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'recruitment.db'}",
        app_secret_key="publication-gate-session-secret",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        recruitment_test_override_open=test_override,
    )


def _login(client: TestClient, *, name: str, phone: str):
    return client.post("/api/auth/login", json={"name": name, "phone": phone})


def test_fresh_production_database_rejects_new_enrollment_without_writes(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, app_env="production", test_override=True)

    with TestClient(create_app(settings=settings)) as client:
        status_response = client.get("/api/recruitment-status")
        login_response = _login(
            client,
            name="Closed Recruitment",
            phone="13800138001",
        )
        readiness_response = client.get("/api/readiness")

    assert login_response.status_code == 503
    assert login_response.json() == {
        "detail": {
            "code": "recruitment_closed",
            "message": "正式实验招募暂未开放，请稍后再试。",
        }
    }
    assert status_response.status_code == 200
    assert status_response.json() == {
        "status": "closed",
        "accepting_new_participants": False,
    }
    assert readiness_response.status_code in {200, 503}
    assert "recruitment" not in readiness_response.json()["components"]

    conn = get_connection(settings)
    try:
        assert conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM participant_attempts").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("existing_identity", [False, True])
def test_paused_recruitment_rejects_identity_that_needs_initial_assignment_without_writes(
    tmp_path: Path,
    existing_identity: bool,
) -> None:
    from backend.app.db import run_migrations, transaction
    from backend.app.repositories.participants import insert_participant_identity

    settings = _settings(tmp_path, app_env="production")
    credentials = {
        "name": "Unassigned Existing" if existing_identity else "Unseen Identity",
        "phone": "13800138012" if existing_identity else "13800138011",
    }
    conn = get_connection(settings)
    try:
        run_migrations(conn)
        if existing_identity:
            with transaction(conn):
                insert_participant_identity(
                    conn,
                    name=credentials["name"],
                    phone=credentials["phone"],
                    phone_hash="unassigned-existing-hash",
                )
        counts_before = {
            table_name: conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
            for table_name in (
                "participants",
                "participant_attempts",
                "participant_days",
            )
        }
    finally:
        conn.close()

    with TestClient(create_app(settings=settings)) as client:
        response = client.post("/api/auth/login", json=credentials)

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "recruitment_closed"

    conn = get_connection(settings)
    try:
        counts_after = {
            table_name: conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
            for table_name in counts_before
        }
    finally:
        conn.close()

    assert counts_after == counts_before


@pytest.mark.parametrize("attempt_status", ["active", "completed"])
def test_closed_recruitment_allows_existing_participant_recovery(
    tmp_path: Path,
    attempt_status: str,
) -> None:
    bootstrap_settings = _settings(tmp_path, app_env="test", test_override=True)
    credentials = {"name": "Existing Participant", "phone": "13800138002"}
    with TestClient(create_app(settings=bootstrap_settings)) as client:
        first_response = client.post("/api/auth/login", json=credentials)
    assert first_response.status_code == 200

    if attempt_status == "completed":
        conn = get_connection(bootstrap_settings)
        try:
            conn.execute(
                "UPDATE participant_attempts SET status = 'completed' WHERE id = ?",
                (first_response.json()["attempt_id"],),
            )
        finally:
            conn.close()

    production_settings = _settings(tmp_path, app_env="production", test_override=True)
    with TestClient(create_app(settings=production_settings)) as client:
        recovery_response = client.post("/api/auth/login", json=credentials)

    assert recovery_response.status_code == 200
    assert recovery_response.json()["participant_id"] == first_response.json()["participant_id"]
    assert recovery_response.json()["attempt_id"] == first_response.json()["attempt_id"]


def test_admin_explicitly_opens_recruitment_with_durable_audit(tmp_path: Path) -> None:
    settings = _settings(tmp_path, app_env="production")
    with TestClient(create_app(settings=settings)) as client:
        admin_login = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )
        open_response = client.post(
            "/api/admin/recruitment",
            json={"open": True},
        )
        duplicate_open_response = client.post(
            "/api/admin/recruitment",
            json={"open": True},
        )
        status_response = client.get("/api/recruitment-status")
        enrollment_response = _login(
            client,
            name="Opened Recruitment",
            phone="13800138003",
        )
        close_response = client.post(
            "/api/admin/recruitment",
            json={"open": False},
        )
        duplicate_close_response = client.post(
            "/api/admin/recruitment",
            json={"open": False},
        )
        closed_status_response = client.get("/api/recruitment-status")

    assert admin_login.status_code == 200
    assert open_response.status_code == 200
    assert open_response.json() == {
        "status": "open",
        "accepting_new_participants": True,
    }
    assert status_response.json() == open_response.json()
    assert duplicate_open_response.json() == open_response.json()
    assert enrollment_response.status_code == 200
    assert close_response.status_code == 200
    assert close_response.json() == {
        "status": "closed",
        "accepting_new_participants": False,
    }
    assert closed_status_response.json() == close_response.json()
    assert duplicate_close_response.json() == close_response.json()

    conn = get_connection(settings)
    try:
        control_row = conn.execute(
            "SELECT status, updated_by FROM recruitment_control WHERE id = 1"
        ).fetchone()
        event_rows = conn.execute(
            """
            SELECT action, target_type, target_id, payload_json
            FROM admin_events
            WHERE action = 'set_recruitment'
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    assert dict(control_row) == {"status": "closed", "updated_by": "admin"}
    assert [row["target_type"] for row in event_rows] == [
        "recruitment",
        "recruitment",
    ]
    assert [row["target_id"] for row in event_rows] == ["formal", "formal"]
    assert [json.loads(row["payload_json"]) for row in event_rows] == [
        {"status": "open"},
        {"status": "closed"},
    ]
