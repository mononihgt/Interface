from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import builtins
import sys
import threading
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app.admin.auth import admin_username_throttle_key, hash_admin_password
from backend.app.admin.gradio_app import (
    AssignmentControlValidationError,
    get_assignment_form_values,
    parse_cap_input,
)
from backend.app.db import get_connection, run_migrations, transaction
from backend.app.repositories.attempts import create_attempt, set_current_attempt
from backend.app.settings import Settings


ADMIN_PASSWORD = "admin-pass-123"
ADMIN_SALT = "task12-salt"


def _password_hash(password: str) -> str:
    return hashlib.sha256(f"{ADMIN_SALT}{password}".encode("utf-8")).hexdigest()


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "admin.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        yizhan_api_key="YIZHAN_SENTINEL_VALUE",
        aabao_api_key="AABAO_SENTINEL_VALUE",
        tencent_secret_key="TENCENT_SENTINEL_VALUE",
    )


@pytest.fixture
def client(sqlite_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from backend.app import services
    from backend.app.main import create_app

    monkeypatch.setattr(
        services.participants,
        "current_shanghai_date",
        lambda: "2026-07-02",
    )

    return TestClient(create_app(settings=sqlite_settings))


def _login_participant(client: TestClient, *, name: str, phone: str) -> dict[str, object]:
    response = client.post(
        "/api/auth/login",
        json={
            "name": name,
            "phone": phone,
            "participant_type": "short",
        },
    )
    assert response.status_code == 200
    return response.json()


def _admin_login(client: TestClient) -> TestClient:
    response = client.post(
        "/api/admin/login",
        json={"username": "admin", "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    return client


def _patch_frontend_dist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    title: str = "admin-spa",
):
    from backend.app import main as app_main

    dist_dir = tmp_path / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        f"<!doctype html><title>{title}</title><div id=\"root\"></div>",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: dist_dir)
    return app_main


def test_admin_requires_authenticated_session(client: TestClient):
    with client:
        response = client.get("/api/admin/overview")

    assert response.status_code == 401
    assert response.json() == {"detail": "Admin login required."}


def test_admin_session_and_logout_endpoints(client: TestClient):
    with client:
        anonymous_response = client.get("/api/admin/session")
        _admin_login(client)
        authenticated_response = client.get("/api/admin/session")
        logout_response = client.post("/api/admin/logout")
        after_logout_response = client.get("/api/admin/overview")

    assert anonymous_response.status_code == 200
    assert anonymous_response.json() == {"authenticated": False, "admin_user": None}
    assert authenticated_response.status_code == 200
    assert authenticated_response.json() == {
        "authenticated": True,
        "admin_user": "admin",
    }
    assert logout_response.status_code == 200
    assert logout_response.json() == {"ok": True}
    assert any(
        "aitrust_v2_admin_sid=" in header and "Max-Age=0" in header
        for header in logout_response.headers.get_list("set-cookie")
    )
    assert after_logout_response.status_code == 401


def test_admin_data_metrics_endpoint_handles_empty_database(client: TestClient):
    with client:
        _admin_login(client)
        response = client.get("/api/admin/data-metrics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["metrics"]["total_participants"] == 0
    assert payload["incomplete_sessions"] == []
    assert payload["recent_sessions"] == []
    assert payload["risk_sessions"] == []


def test_admin_participant_search_requires_query_before_returning_rows(
    client: TestClient,
):
    with client:
        _login_participant(client, name="Searchable User", phone="13900001111")
        _admin_login(client)
        default_response = client.get("/api/admin/participants")
        search_response = client.get("/api/admin/participants?query=Searchable")

    assert default_response.status_code == 200
    assert default_response.json() == {"query": "", "count": 0, "items": []}
    assert search_response.status_code == 200
    payload = search_response.json()
    assert payload["query"] == "Searchable"
    assert payload["count"] == 1
    assert payload["items"][0]["name"] == "Searchable User"


def test_test_session_requires_admin_auth(client: TestClient):
    response = client.post(
        "/api/test/sessions/start",
        json={
            "is_test": True,
            "condition": "human",
            "subcondition": "qa",
            "topic_key": "advice",
            "error_type_id": "factual_minor",
            "planned_error_turn": 2,
            "client_info": {
                "device_type": "desktop",
                "viewport_width": 1280,
                "is_secure_context": True,
                "browser_name": "chrome",
                "browser_version": "120",
                "microphone_available": False,
                "microphone_permission": "unavailable",
            },
        },
    )

    assert response.status_code == 401


def test_admin_can_start_test_session(client: TestClient):
    with client:
        admin_client = _admin_login(client)
        response = admin_client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "condition": "human",
                "subcondition": "qa",
                "topic_key": "advice",
                "error_type_id": "factual_minor",
                "planned_error_turn": 2,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "chrome",
                    "browser_version": "120",
                    "microphone_available": False,
                    "microphone_permission": "unavailable",
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["is_test"] is True


def test_test_session_start_respects_test_channel_enabled(
    sqlite_settings: Settings,
) -> None:
    from backend.app.main import create_app

    disabled_settings = sqlite_settings.model_copy(
        update={"test_channel_enabled": False}
    )
    with TestClient(create_app(settings=disabled_settings)) as client:
        _admin_login(client)
        response = client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "condition": "human",
                "subcondition": "qa",
                "topic_key": "advice",
                "error_type_id": "factual_minor",
                "planned_error_turn": 2,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "chrome",
                    "browser_version": "120",
                    "microphone_available": False,
                    "microphone_permission": "unavailable",
                },
            },
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Test channel is disabled."}

    conn = get_connection(disabled_settings)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM experiment_sessions"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_admin_can_read_test_session_without_participant_cookie(client: TestClient):
    with client:
        _admin_login(client)
        start_response = client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "condition": "human",
                "subcondition": "qa",
                "topic_key": "advice",
                "error_type_id": "factual_minor",
                "planned_error_turn": 2,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "chrome",
                    "browser_version": "120",
                    "microphone_available": False,
                    "microphone_permission": "unavailable",
                },
            },
        )
        session_id = start_response.json()["session_id"]
        me_response = client.get("/api/me")
        session_response = client.get(f"/api/sessions/{session_id}")

    assert start_response.status_code == 200
    assert me_response.status_code == 401
    assert session_response.status_code == 200
    assert session_response.json()["is_test"] is True
    assert session_response.json()["session_id"] == session_id


def test_formal_session_start_rejects_test_flag_for_participant(client: TestClient):
    with client:
        _login_participant(
            client,
            name="Formal Start Guard",
            phone="13800138009",
        )
        response = client.post(
            "/api/sessions/start",
            json={
                "is_test": True,
                "condition": "human",
                "subcondition": "qa",
                "topic_key": "advice",
                "error_type_id": "factual_minor",
                "planned_error_turn": 2,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "chrome",
                    "browser_version": "120",
                    "microphone_available": False,
                    "microphone_permission": "unavailable",
                },
            },
        )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Formal session start cannot create test sessions."
    }


def test_admin_test_session_start_does_not_override_formal_participant_cookie(
    client: TestClient,
):
    with client:
        participant_payload = _login_participant(
            client,
            name="Formal Cookie Owner",
            phone="13800138001",
        )
        me_before = client.get("/api/me")
        assert me_before.status_code == 200
        assert me_before.json()["participant_id"] == participant_payload["participant_id"]

        _admin_login(client)
        response = client.post(
            "/api/test/sessions/start",
            json={
                "is_test": True,
                "condition": "human",
                "subcondition": "qa",
                "topic_key": "advice",
                "error_type_id": "factual_minor",
                "planned_error_turn": 2,
                "client_info": {
                    "device_type": "desktop",
                    "viewport_width": 1280,
                    "is_secure_context": True,
                    "browser_name": "chrome",
                    "browser_version": "120",
                    "microphone_available": False,
                    "microphone_permission": "unavailable",
                },
            },
        )
        me_after = client.get("/api/me")

    assert response.status_code == 200
    assert response.json()["is_test"] is True
    set_cookie_headers = response.headers.get_list("set-cookie")
    assert not any("aitrust_v2_sid=" in header for header in set_cookie_headers)
    assert me_after.status_code == 200
    assert me_after.json()["participant_id"] == participant_payload["participant_id"]


def test_admin_route_serves_react_dashboard_without_admin_cookie(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app_main = _patch_frontend_dist(
        monkeypatch,
        tmp_path,
        title="react-admin",
    )
    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        response = client.get("/admin")
        api_response = client.get("/api/admin/overview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "react-admin" in response.text
    assert "/api/admin/login" not in response.text
    assert api_response.status_code == 401


def test_admin_console_redirects_to_react_dashboard_without_admin_cookie(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app_main = _patch_frontend_dist(monkeypatch, tmp_path)
    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        response = client.get("/admin/console", follow_redirects=False)
        slash_response = client.get("/admin/console/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert slash_response.status_code == 303
    assert slash_response.headers["location"] == "/admin"


def test_authenticated_admin_route_stays_on_react_dashboard(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app_main = _patch_frontend_dist(
        monkeypatch,
        tmp_path,
        title="authenticated-react-admin",
    )
    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        _admin_login(client)
        admin_response = client.get("/admin", follow_redirects=False)
        bare_console_response = client.get("/admin/console", follow_redirects=False)
        console_response = client.get("/admin/console/", follow_redirects=False)

    assert admin_response.status_code == 200
    assert "authenticated-react-admin" in admin_response.text
    assert bare_console_response.status_code == 303
    assert bare_console_response.headers["location"] == "/admin"
    assert console_response.status_code == 303
    assert console_response.headers["location"] == "/admin"


def test_admin_views_mask_phone_and_hide_keys(
    client: TestClient,
    sqlite_settings: Settings,
):
    raw_phone = "13800138000"
    raw_secret_values = {
        sqlite_settings.yizhan_api_key,
        sqlite_settings.aabao_api_key,
        sqlite_settings.tencent_secret_key,
    }

    with client:
        participant_payload = _login_participant(
            client,
            name="Admin Visible Participant",
            phone=raw_phone,
        )
        _admin_login(client)

        overview_response = client.get("/api/admin/overview")
        participants_response = client.get("/api/admin/participants", params={"query": "8000"})
        logs_response = client.get("/api/admin/system-logs")

    assert overview_response.status_code == 200
    assert participants_response.status_code == 200
    assert logs_response.status_code == 200

    search_payload = participants_response.json()
    assert search_payload["items"], "expected at least one sanitized participant row"
    participant_row = search_payload["items"][0]
    assert participant_row["participant_id"] == participant_payload["participant_id"]
    assert participant_row["masked_phone"] == "138****8000"
    assert participant_row["phone_hash"]
    assert participant_row["phone_hash"] != raw_phone
    assert raw_phone not in json.dumps(search_payload, ensure_ascii=False)
    assert "phone" not in participant_row

    combined = "\n".join(
        [
            overview_response.text,
            participants_response.text,
            logs_response.text,
        ]
    )
    assert participant_row["phone_hash"] in combined
    assert participant_payload["masked_phone"] in combined
    for secret in raw_secret_values:
        assert secret is not None
        assert secret not in combined
    assert raw_phone not in combined


def test_admin_participant_search_treats_none_query_as_empty(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.repositories.admin import AdminRepository

    with client:
        participant_payload = _login_participant(
            client,
            name="None Query Participant",
            phone="13900139000",
        )

    conn = get_connection(sqlite_settings)
    try:
        repository = AdminRepository(conn, settings=sqlite_settings)
        search_payload = repository.search_participants(query=None)
        explicit_search_payload = repository.search_participants(
            query="None Query Participant"
        )
    finally:
        conn.close()

    assert search_payload["query"] == ""
    assert search_payload["items"] == []
    assert explicit_search_payload["items"]
    assert (
        explicit_search_payload["items"][0]["participant_id"]
        == participant_payload["participant_id"]
    )


def test_admin_overview_search_and_status_counts_use_current_attempt_state(
    sqlite_settings: Settings,
):
    from backend.app.repositories.admin import AdminRepository

    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO participants (
                name,
                phone,
                phone_hash,
                participant_type,
                condition,
                subcondition,
                topic_key,
                error_type_id,
                target_days,
                current_status,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, 'short', 'human', 'qa', 'legacy-topic', 'factual_minor', 1, 'active', '2026-07-02T09:00:00+08:00', '2026-07-02T09:00:00+08:00')
            """,
            (
                "Attempt Source Participant",
                "13800138111",
                "hash-attempt-source",
            ),
        )
        participant_id = int(
            conn.execute(
                "SELECT id FROM participants WHERE phone_hash = ?",
                ("hash-attempt-source",),
            ).fetchone()["id"]
        )
        attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="long",
            condition="tool",
            subcondition="planning",
            topic_key="goalPlan",
            error_type_id="logic_major",
            target_days=3,
            status="completed",
        )
        set_current_attempt(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
        )

        repository = AdminRepository(conn, settings=sqlite_settings)
        overview = repository.get_overview_metrics()
        search_payload = repository.search_participants(query="Attempt Source Participant")
        log_summary = repository.get_system_logs_summary()
    finally:
        conn.close()

    assert overview["completion_by_type"] == {"short": 0, "long": 1}
    tool_planning_cell = next(
        cell
        for row in overview["assignment_matrix"]
        if row["condition"] == "tool"
        for cell in row["cells"]
        if cell["subcondition"] == "planning"
    )
    assert tool_planning_cell["count"] == 1

    assert search_payload["count"] == 1
    row = search_payload["items"][0]
    assert row["participant_type"] == "long"
    assert row["condition"] == "tool"
    assert row["subcondition"] == "planning"
    assert row["topic_key"] == "goalPlan"
    assert row["error_type_id"] == "logic_major"
    assert row["current_status"] == "completed"

    assert log_summary["backend_status_counts"] == {"completed": 1}


def test_admin_export_and_api_health_stay_sanitized(
    client: TestClient,
    sqlite_settings: Settings,
):
    raw_phone = "13800138000"
    with client:
        _login_participant(
            client,
            name="Export Participant",
            phone=raw_phone,
        )
        _admin_login(client)

        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                INSERT INTO api_call_logs (
                    request_id,
                    route,
                    provider,
                    model,
                    status,
                    error_code,
                    error_message_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "health-redaction",
                    "chat",
                    "yi-zhan",
                    "gpt-5.1",
                    "http_error",
                    "unauthorized",
                    "should-not-leak-admin-pass-123-or-13800138000",
                ),
            )
        finally:
            conn.close()

        health_response = client.get("/api/admin/api-health")
        export_response = client.post("/api/admin/export")
        logs_response = client.get("/api/admin/system-logs")

    assert health_response.status_code == 200
    assert export_response.status_code == 200
    assert logs_response.status_code == 200

    combined = "\n".join(
        [
            health_response.text,
            export_response.text,
            logs_response.text,
        ]
    )
    assert raw_phone not in combined
    assert sqlite_settings.yizhan_api_key not in combined
    assert sqlite_settings.aabao_api_key not in combined
    assert sqlite_settings.tencent_secret_key not in combined
    assert "admin-pass-123" not in combined
    assert "should-not-leak-admin-pass-123-or-13800138000" not in combined

    export_payload = export_response.json()
    assert export_payload["notes"] == [
        "Structured participant identifiers are pseudonymized; validated raw audio remains controlled sensitive research data. Secret values, environment files, and raw log dumps are excluded."
    ]
    export_path = Path(export_payload["path"])
    assert export_path.exists()
    with zipfile.ZipFile(export_path) as archive:
        archive_text = "\n".join(
            archive.read(name).decode("utf-8", errors="ignore")
            for name in archive.namelist()
        )
    assert sqlite_settings.yizhan_api_key not in archive_text
    assert sqlite_settings.tencent_secret_key not in archive_text

    logs_payload = logs_response.json()
    package_path = Path(logs_payload["sanitized_package_path"])
    assert package_path.exists()
    package_text = package_path.read_text(encoding="utf-8")
    assert raw_phone not in package_text
    assert sqlite_settings.yizhan_api_key not in package_text
    assert sqlite_settings.tencent_secret_key not in package_text


def test_provider_model_usage_reports_all_time_and_last_24h(
    client: TestClient,
    sqlite_settings: Settings,
):
    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        recent_success_time = conn.execute(
            "SELECT datetime('now', '-2 hours') AS created_at"
        ).fetchone()["created_at"]
        recent_failure_time = conn.execute(
            "SELECT datetime('now', '-1 hour') AS created_at"
        ).fetchone()["created_at"]
        old_success_time = conn.execute(
            "SELECT datetime('now', '-2 days') AS created_at"
        ).fetchone()["created_at"]
        conn.executemany(
            """
            INSERT INTO api_call_logs (
                request_id,
                route,
                provider,
                model,
                status,
                error_code,
                error_message_summary,
                latency_ms,
                cooldown_applied,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "usage-success-recent",
                    "chat",
                    "yi-zhan",
                    "gpt-5.1",
                    "success",
                    None,
                    None,
                    120,
                    0,
                    recent_success_time,
                ),
                (
                    "usage-failure-recent",
                    "chat",
                    "yi-zhan",
                    "gpt-5.1",
                    "http_error",
                    "rate_limit",
                    "rate-limited-without-secrets",
                    240,
                    1,
                    recent_failure_time,
                ),
                (
                    "usage-old-success",
                    "asr",
                    "tencent",
                    "asr-default",
                    "success",
                    None,
                    None,
                    300,
                    0,
                    old_success_time,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        unauthenticated_response = client.get("/api/admin/provider-model-usage")
        _admin_login(client)
        response = client.get("/api/admin/provider-model-usage")

    assert unauthenticated_response.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert {window["window"] for window in payload["windows"]} == {
        "all_time",
        "last_24h",
    }

    all_time = next(window for window in payload["windows"] if window["window"] == "all_time")
    last_24h = next(window for window in payload["windows"] if window["window"] == "last_24h")
    all_time_yizhan = next(
        row
        for row in all_time["provider_model_rows"]
        if row["provider"] == "yi-zhan" and row["model"] == "gpt-5.1"
    )
    recent_yizhan = next(
        row
        for row in last_24h["provider_model_rows"]
        if row["provider"] == "yi-zhan" and row["model"] == "gpt-5.1"
    )

    assert all_time["total_calls"] == 3
    assert all_time_yizhan["calls"] == 2
    assert all_time_yizhan["successes"] == 1
    assert all_time_yizhan["failures"] == 1
    assert all_time_yizhan["success_rate"] == 50.0
    assert all_time_yizhan["avg_latency_ms"] == 180.0
    assert all_time_yizhan["p95_latency_ms"] == 240
    assert all_time_yizhan["cooldown_applied_count"] == 1
    assert all_time_yizhan["last_failure_code"] == "http_error"
    assert all_time_yizhan["last_failure_summary"] == "http_error:http_error"

    assert last_24h["total_calls"] == 2
    assert len(last_24h["provider_model_rows"]) == 1
    assert recent_yizhan["calls"] == 2
    assert any(
        row["route"] == "chat"
        and row["provider"] == "yi-zhan"
        and row["model"] == "gpt-5.1"
        and row["calls"] == 2
        for row in last_24h["route_rows"]
    )


def test_admin_provider_usage_and_health_handle_null_models(client: TestClient, sqlite_settings: Settings):
    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        created_at = conn.execute("SELECT datetime('now') AS created_at").fetchone()[
            "created_at"
        ]
        conn.executemany(
            """
            INSERT INTO api_call_logs (
                request_id,
                route,
                provider,
                model,
                status,
                error_code,
                error_message_summary,
                latency_ms,
                cooldown_applied,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "null-model-success",
                    "chat",
                    "yi-zhan",
                    None,
                    "success",
                    None,
                    None,
                    101,
                    0,
                    created_at,
                ),
                (
                    "string-model-success",
                    "chat",
                    "yi-zhan",
                    "gpt-5.1",
                    "success",
                    None,
                    None,
                    102,
                    0,
                    created_at,
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        _admin_login(client)
        usage_response = client.get("/api/admin/provider-model-usage")
        health_response = client.get("/api/admin/api-health")

    assert usage_response.status_code == 200
    assert health_response.status_code == 200
    usage_rows = usage_response.json()["windows"][0]["provider_model_rows"]
    assert any(row["provider"] == "yi-zhan" and row["model"] is None for row in usage_rows)
    assert any(row["provider"] == "yi-zhan" and row["model"] == "gpt-5.1" for row in usage_rows)


def test_admin_system_metrics_hide_absolute_storage_paths(client: TestClient):
    with client:
        _admin_login(client)
        response = client.get("/api/admin/system-metrics")

    assert response.status_code == 200
    payload = response.json()
    assert not Path(payload["database"]["path"]).is_absolute()
    assert not Path(payload["data_directory"]["path"]).is_absolute()


def test_gradio_export_tab_does_not_expose_synchronous_export_button():
    gradio_source = Path("backend/app/admin/gradio_app.py").read_text(encoding="utf-8")

    assert "Generate sanitized export" not in gradio_source
    assert "export_sanitized_data(" not in gradio_source
    assert 'gr.Button("Export all data"' in gradio_source
    assert 'gr.Button("Export complete_no_external_error_data"' in gradio_source
    assert "export_job_type = gr.Dropdown" not in gradio_source


def test_clean_data_audit_requires_admin(client: TestClient):
    response = client.get("/api/admin/clean-data-audits")

    assert response.status_code == 401
    assert response.json() == {"detail": "Admin login required."}


def test_admin_can_list_clean_data_audits(client: TestClient):
    with client:
        _admin_login(client)
        response = client.get("/api/admin/clean-data-audits")

    assert response.status_code == 200
    assert "items" in response.json()


def test_admin_can_recompute_clean_data_audits_and_filter_by_status(
    client: TestClient,
    sqlite_settings: Settings,
):
    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO participants (
                name,
                phone,
                phone_hash,
                participant_type,
                condition,
                subcondition,
                topic_key,
                error_type_id,
                target_days,
                current_status
            ) VALUES (?, ?, ?, 'short', 'human', 'qa', 'advice', 'factual_minor', 1, 'active')
            """,
            (
                "Clean Audit Participant",
                "13800138002",
                "hash-clean-audit",
            ),
        )
        participant_id = int(
            conn.execute(
                "SELECT id FROM participants WHERE phone_hash = ?",
                ("hash-clean-audit",),
            ).fetchone()["id"]
        )
        attempt_id = create_attempt(
            conn,
            participant_id=participant_id,
            participant_type="short",
            condition="human",
            subcondition="qa",
            topic_key="advice",
            error_type_id="factual_minor",
            target_days=1,
        )
        set_current_attempt(
            conn,
            participant_id=participant_id,
            attempt_id=attempt_id,
        )
    finally:
        conn.close()

    with client:
        _admin_login(client)
        recompute_response = client.post("/api/admin/clean-data-audits/recompute")
        all_response = client.get("/api/admin/clean-data-audits")
        excluded_response = client.get(
            "/api/admin/clean-data-audits",
            params={"status": "excluded"},
        )
        eligible_response = client.get(
            "/api/admin/clean-data-audits",
            params={"status": "eligible"},
        )

    assert recompute_response.status_code == 200
    recompute_payload = recompute_response.json()
    assert recompute_payload["summary"]["scanned"] == 1
    assert recompute_payload["summary"]["persisted"] == 1
    assert "incomplete_formal_days" in recompute_payload["items"][0]["reasons"]

    assert all_response.status_code == 200
    assert excluded_response.status_code == 200
    assert eligible_response.status_code == 200
    assert len(all_response.json()["items"]) == 1
    assert len(excluded_response.json()["items"]) == 1
    assert eligible_response.json()["items"] == []

    row = all_response.json()["items"][0]
    assert row["name"] == "Clean Audit Participant"
    assert row["participant_type"] == "short"
    assert row["status"] == "excluded"
    assert "incomplete_formal_days" in row["reasons"]


def test_admin_assignment_contract_exposes_only_cells_and_runtime_flag(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        _admin_login(client)
        update_response = client.post(
            "/api/admin/assignment-control",
            json={
                "operation": "cell",
                "participant_type": "short",
                "condition": "human",
                "subcondition": "qa",
                "error_type_id": "factual_minor",
                "cap": 2,
                "enabled": False,
                "pause_new_participants": True,
                "test_channel_only": True,
            },
        )
        read_response = client.get("/api/admin/assignment-control")

    assert update_response.status_code == 200
    payload = read_response.json()
    short_cells = payload["participant_types"]["short"]["cells"]
    human_qa = next(
        cell
        for cell in short_cells
        if cell["condition"] == "human" and cell["subcondition"] == "qa"
    )
    assert human_qa["cap"] == 2
    assert human_qa["enabled"] is False
    assert "global_controls" not in payload
    assert payload["current_flags"] == {"test_channel_enabled": True}
    assert all(
        "pause_new_participants" not in note and "test_channel_only" not in note
        for note in payload["notes"]
    )

    conn = get_connection(sqlite_settings)
    try:
        legacy_rows = conn.execute(
            """
            SELECT key FROM admin_global_controls
            WHERE key IN ('pause_new_participants', 'test_channel_only')
            """
        ).fetchall()
    finally:
        conn.close()

    assert legacy_rows == []


def _assignment_cell(
    *,
    participant_type: str = "short",
    condition: str = "human",
    subcondition: str = "qa",
    error_type_id: str = "factual_minor",
) -> dict[str, str]:
    return {
        "participant_type": participant_type,
        "condition": condition,
        "subcondition": subcondition,
        "error_type_id": error_type_id,
    }


def _assignment_batch_request() -> dict[str, object]:
    return {
        "scope": {
            "cells": [
                _assignment_cell(),
                _assignment_cell(
                    participant_type="long",
                    condition="tool",
                    subcondition="planning",
                    error_type_id="logic_major",
                ),
            ]
        },
        "changes": {"cap": 4, "enabled": False},
    }


def test_assignment_batch_rejects_empty_unbounded_and_partially_invalid_scopes(
    client: TestClient,
    sqlite_settings: Settings,
):
    valid_request = _assignment_batch_request()
    partially_invalid_request = {
        **valid_request,
        "scope": {
            "cells": [
                _assignment_cell(),
                _assignment_cell(error_type_id="not-an-error-type"),
            ]
        },
    }

    with client:
        _admin_login(client)
        empty_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json={"scope": {"cells": []}, "changes": {"enabled": False}},
        )
        unbounded_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json={"scope": {"filter": {}}, "changes": {"enabled": False}},
        )
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=valid_request,
        )
        invalid_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **partially_invalid_request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert empty_response.status_code == 400
    assert unbounded_response.status_code == 400
    assert invalid_response.status_code == 400

    conn = get_connection(sqlite_settings)
    try:
        assignment_count = conn.execute(
            "SELECT COUNT(*) AS count FROM admin_assignment_units"
        ).fetchone()["count"]
        batch_event_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM admin_events
            WHERE action = 'update_assignment_cap'
              AND target_type = 'assignment_batch'
            """
        ).fetchone()["count"]
    finally:
        conn.close()

    assert assignment_count == 0
    assert batch_event_count == 0


def test_assignment_batch_requires_admin_before_preview_or_mutation(
    client: TestClient,
    sqlite_settings: Settings,
):
    request = _assignment_batch_request()

    with client:
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        mutation_response = client.post(
            "/api/admin/assignment-control/batch",
            json={**request, "scope_version": "unauthorized-version"},
        )

    assert preview_response.status_code == 401
    assert mutation_response.status_code == 401

    conn = get_connection(sqlite_settings)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM admin_assignment_units"
        ).fetchone()["count"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM admin_events"
        ).fetchone()["count"] == 0
    finally:
        conn.close()


def test_assignment_batch_applies_atomically_with_one_bounded_audit_event(
    client: TestClient,
    sqlite_settings: Settings,
):
    request = _assignment_batch_request()

    with client:
        _admin_login(client)
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        mutation_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["affected_count"] == 2
    assert preview["scope"] == {
        "kind": "explicit_cells",
        "description": "2 explicit assignment cells",
        "selected_cells": [
            _assignment_cell(
                participant_type="long",
                condition="tool",
                subcondition="planning",
                error_type_id="logic_major",
            ),
            _assignment_cell(),
        ],
    }

    assert mutation_response.status_code == 200
    result = mutation_response.json()
    assert result["affected_count"] == 2
    assert result["result"] == {"updated_cells": 2}
    assert "global_controls" not in preview

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT participant_type, condition, subcondition, error_type_id, cap, enabled
            FROM admin_assignment_units
            ORDER BY participant_type, condition, subcondition, error_type_id
            """
        ).fetchall()
        events = conn.execute(
            """
            SELECT target_type, target_id, payload_json
            FROM admin_events
            WHERE action = 'update_assignment_cap'
            """
        ).fetchall()
    finally:
        conn.close()

    assert [tuple(row) for row in rows] == [
        ("long", "tool", "planning", "logic_major", 4, 0),
        ("short", "human", "qa", "factual_minor", 4, 0),
    ]
    assert len(events) == 1
    assert events[0]["target_type"] == "assignment_batch"
    assert events[0]["target_id"] == preview["scope_version"]
    audit_payload = json.loads(events[0]["payload_json"])
    assert audit_payload == {
        "affected_count": 2,
        "changes": {"cap": 4, "enabled": False},
        "operation": "batch",
        "result": {"updated_cells": 2},
        "scope": preview["scope"],
        "scope_version": preview["scope_version"],
    }


def test_legacy_global_rows_do_not_affect_batch_scope_versions_or_mutations(
    client: TestClient,
    sqlite_settings: Settings,
):
    cells = _assignment_batch_request()["scope"]["cells"]
    request = {
        "scope": {"cells": cells},
        "changes": {},
        "cell_updates": [
            {**cells[0], "cap": 2, "enabled": False},
            {**cells[1], "cap": 7, "enabled": True},
        ],
    }

    with client:
        _admin_login(client)
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        assert preview_response.status_code == 200
        conn = get_connection(sqlite_settings)
        try:
            conn.executemany(
                "INSERT INTO admin_global_controls (key, value) VALUES (?, ?)",
                [
                    ("pause_new_participants", "true"),
                    ("test_channel_only", "true"),
                ],
            )
        finally:
            conn.close()
        repeated_preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        mutation_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert mutation_response.status_code == 200
    assert repeated_preview_response.status_code == 200
    assert (
        repeated_preview_response.json()["scope_version"]
        == preview_response.json()["scope_version"]
    )
    assert "global_controls" not in repeated_preview_response.json()
    assert mutation_response.json()["result"] == {"updated_cells": 2}

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT participant_type, cap, enabled
            FROM admin_assignment_units
            ORDER BY participant_type
            """
        ).fetchall()
        batch_event = conn.execute(
            """
            SELECT payload_json
            FROM admin_events
            WHERE target_type = 'assignment_batch'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert [tuple(row) for row in rows] == [
        ("long", 7, 1),
        ("short", 2, 0),
    ]
    assert batch_event is not None
    assert json.loads(batch_event["payload_json"]) == {
        "affected_count": 2,
        "changes": {
            "cell_updates": [
                {
                    **cells[1],
                    "cap": 7,
                    "enabled": True,
                },
                {
                    **cells[0],
                    "cap": 2,
                    "enabled": False,
                },
            ],
        },
        "operation": "batch",
        "result": {"updated_cells": 2},
        "scope": preview_response.json()["scope"],
        "scope_version": preview_response.json()["scope_version"],
    }


def test_assignment_batch_rejects_stale_preview_without_writes(
    client: TestClient,
    sqlite_settings: Settings,
):
    request = _assignment_batch_request()

    with client:
        _admin_login(client)
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        intervening_response = client.post(
            "/api/admin/assignment-control",
            json={
                "operation": "cell",
                **_assignment_cell(),
                "cap": 1,
                "enabled": True,
            },
        )
        stale_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert intervening_response.status_code == 200
    assert stale_response.status_code == 409
    assert "preview" in stale_response.json()["detail"].lower()

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT participant_type, cap, enabled
            FROM admin_assignment_units
            ORDER BY participant_type
            """
        ).fetchall()
        batch_event_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM admin_events
            WHERE target_type = 'assignment_batch'
            """
        ).fetchone()["count"]
    finally:
        conn.close()

    assert [tuple(row) for row in rows] == [("short", 1, 1)]
    assert batch_event_count == 0


def test_assignment_batch_filter_that_becomes_empty_returns_stale_conflict(
    client: TestClient,
    sqlite_settings: Settings,
):
    cell = _assignment_cell()
    request = {
        "scope": {"filter": {"enabled": False}},
        "changes": {"cap": 8},
    }

    with client:
        _admin_login(client)
        disable_response = client.post(
            "/api/admin/assignment-control",
            json={"operation": "cell", **cell, "cap": 1, "enabled": False},
        )
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        enable_response = client.post(
            "/api/admin/assignment-control",
            json={"operation": "cell", **cell, "cap": 1, "enabled": True},
        )
        stale_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert disable_response.status_code == 200
    assert preview_response.status_code == 200
    assert preview_response.json()["affected_count"] == 1
    assert enable_response.status_code == 200
    assert stale_response.status_code == 409
    assert "stale" in stale_response.json()["detail"].lower()

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT cap, enabled
            FROM admin_assignment_units
            WHERE participant_type = 'short'
              AND condition = 'human'
              AND subcondition = 'qa'
              AND error_type_id = 'factual_minor'
            """
        ).fetchone()
        batch_event_count = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM admin_events
            WHERE target_type = 'assignment_batch'
            """
        ).fetchone()["count"]
    finally:
        conn.close()

    assert tuple(row) == (1, 1)
    assert batch_event_count == 0


def test_assignment_batch_preview_rejects_incomplete_per_cell_filter_updates(
    client: TestClient,
    sqlite_settings: Settings,
):
    first_cell = _assignment_cell()
    second_cell = _assignment_cell(
        participant_type="long",
        condition="tool",
        subcondition="planning",
        error_type_id="logic_major",
    )
    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled
            ) VALUES (:participant_type, :condition, :subcondition, :error_type_id, 1, 0)
            """,
            (first_cell, second_cell),
        )
    finally:
        conn.close()

    with client:
        _admin_login(client)
        response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json={
                "scope": {"filter": {"enabled": False}},
                "changes": {},
                "cell_updates": [{**first_cell, "cap": 2}],
            },
        )

    assert response.status_code == 400
    assert "complete selected scope" in response.json()["detail"].lower()


def test_assignment_batch_filter_shrink_with_per_cell_updates_is_stale_conflict(
    client: TestClient,
    sqlite_settings: Settings,
):
    first_cell = _assignment_cell()
    second_cell = _assignment_cell(
        participant_type="long",
        condition="tool",
        subcondition="planning",
        error_type_id="logic_major",
    )
    conn = get_connection(sqlite_settings)
    try:
        run_migrations(conn)
        conn.executemany(
            """
            INSERT INTO admin_assignment_units (
                participant_type,
                condition,
                subcondition,
                error_type_id,
                cap,
                enabled
            ) VALUES (:participant_type, :condition, :subcondition, :error_type_id, 1, 0)
            """,
            (first_cell, second_cell),
        )
    finally:
        conn.close()

    request = {
        "scope": {"filter": {"enabled": False}},
        "changes": {},
        "cell_updates": [
            {**first_cell, "cap": 2, "enabled": False},
            {**second_cell, "cap": 7, "enabled": False},
        ],
    }
    with client:
        _admin_login(client)
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )
        concurrent_response = client.post(
            "/api/admin/assignment-control",
            json={
                "operation": "cell",
                **second_cell,
                "cap": 3,
                "enabled": True,
            },
        )
        stale_response = client.post(
            "/api/admin/assignment-control/batch",
            json={
                **request,
                "scope_version": preview_response.json()["scope_version"],
            },
        )

    assert preview_response.status_code == 200
    assert preview_response.json()["affected_count"] == 2
    assert concurrent_response.status_code == 200
    assert stale_response.status_code == 409
    assert "stale" in stale_response.json()["detail"].lower()

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT participant_type, condition, subcondition, error_type_id, cap, enabled
            FROM admin_assignment_units
            ORDER BY participant_type, condition, subcondition, error_type_id
            """
        ).fetchall()
        event_rows = conn.execute(
            """
            SELECT target_type, target_id
            FROM admin_events
            WHERE action = 'update_assignment_cap'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert [tuple(row) for row in rows] == [
        ("long", "tool", "planning", "logic_major", 3, 1),
        ("short", "human", "qa", "factual_minor", 1, 0),
    ]
    assert [tuple(row) for row in event_rows] == [
        ("assignment_unit", "long:tool:planning:logic_major")
    ]


def test_assignment_batch_preview_uses_one_deferred_read_transaction(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    statements: list[str] = []
    original_get_connection = app_main.get_connection

    def get_traced_connection(settings: Settings):
        conn = original_get_connection(settings)
        conn.set_trace_callback(statements.append)
        return conn

    with client:
        _admin_login(client)
        monkeypatch.setattr(app_main, "get_connection", get_traced_connection)
        response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=_assignment_batch_request(),
        )

    assert response.status_code == 200
    transaction_statements = [
        statement.strip().upper()
        for statement in statements
        if statement.strip().upper().startswith("BEGIN")
    ]
    assert transaction_statements == ["BEGIN"]


def test_assignment_batch_rolls_back_cell_writes_when_audit_fails(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    request = _assignment_batch_request()

    with client:
        _admin_login(client)
        preview_response = client.post(
            "/api/admin/assignment-control/batch/preview",
            json=request,
        )

        def fail_record_event(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(
            "backend.app.repositories.admin.AdminRepository.record_event",
            fail_record_event,
        )
        with pytest.raises(RuntimeError, match="audit unavailable"):
            client.post(
                "/api/admin/assignment-control/batch",
                json={
                    **request,
                    "scope_version": preview_response.json()["scope_version"],
                },
            )

    conn = get_connection(sqlite_settings)
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM admin_assignment_units"
        ).fetchone()["count"] == 0
    finally:
        conn.close()


def test_admin_assignment_preview_is_per_participant_type(client: TestClient):
    with client:
        _admin_login(client)
        response = client.get("/api/admin/assignment-control")

    assert response.status_code == 200
    previews = response.json()["next_assignment_preview"]
    assert previews["short"]["available"] is True
    assert previews["short"]["participant_type"] == "short"
    assert previews["long"]["available"] is True
    assert previews["long"]["participant_type"] == "long"


def test_assignment_control_counts_current_attempt_instead_of_legacy_participant_columns(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        participant_payload = _login_participant(
            client,
            name="Assignment Source Participant",
            phone="13800138077",
        )
        _admin_login(client)

    conn = get_connection(sqlite_settings)
    try:
        participant_id = int(participant_payload["participant_id"])
        current_attempt = conn.execute(
            """
            SELECT id
            FROM participant_attempts
            WHERE participant_id = ?
            ORDER BY attempt_no DESC
            LIMIT 1
            """,
            (participant_id,),
        ).fetchone()
        assert current_attempt is not None
        conn.execute(
            """
            UPDATE participant_attempts
            SET
                participant_type = 'long',
                condition = 'tool',
                subcondition = 'planning',
                topic_key = 'goalPlan',
                error_type_id = 'logic_major',
                target_days = 3,
                status = 'completed',
                valid_for_export = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (int(current_attempt["id"]),),
        )
        conn.execute(
            """
            UPDATE participants
            SET
                participant_type = 'short',
                condition = 'human',
                subcondition = 'qa',
                topic_key = 'advice',
                error_type_id = 'factual_minor',
                target_days = 1,
                current_status = 'active',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (participant_id,),
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        _admin_login(client)
        payload = client.get("/api/admin/assignment-control").json()

    long_planning_cell = next(
        cell
        for cell in payload["participant_types"]["long"]["cells"]
        if cell["condition"] == "tool"
        and cell["subcondition"] == "planning"
        and cell["error_type_id"] == "logic_major"
    )
    short_human_qa_cell = next(
        cell
        for cell in payload["participant_types"]["short"]["cells"]
        if cell["condition"] == "human"
        and cell["subcondition"] == "qa"
        and cell["error_type_id"] == "factual_minor"
    )

    assert long_planning_cell["count"] == 1
    assert short_human_qa_cell["count"] == 0


def test_assignment_control_exposes_clean_and_active_counts_separately(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        active_payload = _login_participant(
            client,
            name="Active Assignment Count",
            phone="13800138078",
        )
        clean_payload = _login_participant(
            client,
            name="Clean Assignment Count",
            phone="13800138079",
        )

    conn = get_connection(sqlite_settings)
    try:
        active_participant_id = int(active_payload["participant_id"])
        clean_participant_id = int(clean_payload["participant_id"])
        rows = conn.execute(
            """
            SELECT id AS participant_id, current_attempt_id
            FROM participants
            WHERE id IN (?, ?)
            """,
            (active_participant_id, clean_participant_id),
        ).fetchall()
        attempt_ids = {
            int(row["participant_id"]): int(row["current_attempt_id"])
            for row in rows
        }
        cell_values = (
            "short",
            "human",
            "qa",
            "advice",
            "factual_minor",
            1,
        )
        conn.execute(
            """
            UPDATE participant_attempts
            SET
                participant_type = ?,
                condition = ?,
                subcondition = ?,
                topic_key = ?,
                error_type_id = ?,
                target_days = ?,
                status = 'active',
                valid_for_export = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (*cell_values, attempt_ids[active_participant_id]),
        )
        conn.execute(
            """
            UPDATE participant_attempts
            SET
                participant_type = ?,
                condition = ?,
                subcondition = ?,
                topic_key = ?,
                error_type_id = ?,
                target_days = ?,
                status = 'completed',
                valid_for_export = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (*cell_values, attempt_ids[clean_participant_id]),
        )
        conn.execute(
            """
            INSERT INTO clean_data_audits (
                participant_id,
                attempt_id,
                status,
                reasons_json
            ) VALUES (?, ?, 'eligible', '[]')
            """,
            (clean_participant_id, attempt_ids[clean_participant_id]),
        )
        conn.commit()
    finally:
        conn.close()

    with client:
        _admin_login(client)
        response = client.get("/api/admin/assignment-control")

    assert response.status_code == 200
    cell = next(
        item
        for item in response.json()["participant_types"]["short"]["cells"]
        if item["condition"] == "human"
        and item["subcondition"] == "qa"
        and item["error_type_id"] == "factual_minor"
    )
    assert cell["count"] == 2
    assert cell["active_assignment_count"] == 1
    assert cell["complete_no_external_error_count"] == 1


def test_retired_assignment_admission_fields_cannot_mutate_controls(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        _admin_login(client)
        update_response = client.post(
            "/api/admin/assignment-control",
            json={
                "operation": "global",
                "pause_new_participants": False,
                "test_channel_only": True,
            },
        )

    assert update_response.status_code in {400, 422}

    conn = get_connection(sqlite_settings)
    try:
        legacy_rows = conn.execute(
            """
            SELECT key FROM admin_global_controls
            WHERE key IN ('pause_new_participants', 'test_channel_only')
            """
        ).fetchall()
        event_count = conn.execute(
            "SELECT COUNT(*) FROM admin_events WHERE action = 'update_assignment_cap'"
        ).fetchone()[0]
    finally:
        conn.close()

    assert legacy_rows == []
    assert event_count == 0


def test_assignment_form_values_hydrate_from_persisted_summary(client: TestClient):
    with client:
        _admin_login(client)
        update_response = client.post(
            "/api/admin/assignment-control",
            json={
                "operation": "cell",
                "participant_type": "long",
                "condition": "tool",
                "subcondition": "planning",
                "error_type_id": "logic_major",
                "cap": 7,
                "enabled": False,
                "pause_new_participants": True,
                "test_channel_only": True,
            },
        )
        assert update_response.status_code == 200
        summary = client.get("/api/admin/assignment-control").json()

    form_values = get_assignment_form_values(
        summary,
        participant_type="long",
        condition="tool",
        subcondition="planning",
        error_type_id="logic_major",
    )

    assert form_values == {
        "cap_text": "7",
        "enabled": False,
    }


def test_parse_cap_input_rejects_invalid_values_with_clear_message():
    assert parse_cap_input(None) is None
    assert parse_cap_input("") is None
    assert parse_cap_input(" 5 ") == 5

    with pytest.raises(
        AssignmentControlValidationError,
        match="Cap must be a non-negative integer or left blank.",
    ):
        parse_cap_input("-1")

    with pytest.raises(
        AssignmentControlValidationError,
        match="Cap must be a non-negative integer or left blank.",
    ):
        parse_cap_input("abc")


def test_admin_deepseek_test_requires_auth_and_handles_unconfigured_key_without_network(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.main import create_app

    async def _unexpected_network(*args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("network adapter must not run without a DeepSeek key")

    from backend.app.services import providers

    monkeypatch.setattr(providers.HttpxProviderAdapter, "generate", _unexpected_network)

    client = TestClient(create_app(settings=sqlite_settings))
    with client:
        unauthenticated_response = client.post("/api/admin/providers/deepseek/test")
        _admin_login(client)
        response = client.post("/api/admin/providers/deepseek/test")

    assert unauthenticated_response.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "status": "not_configured",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "latency_ms": None,
        "error_code": "not_configured",
    }

    conn = get_connection(sqlite_settings)
    try:
        api_log_count = conn.execute(
            "SELECT COUNT(*) AS count FROM api_call_logs"
        ).fetchone()["count"]
        row = conn.execute(
            """
            SELECT action, target_type, target_id, payload_json
            FROM admin_events
            WHERE action = 'test_agent'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert api_log_count == 0
    assert row is not None
    assert (row["action"], row["target_type"], row["target_id"]) == (
        "test_agent",
        "provider",
        "deepseek",
    )
    assert json.loads(row["payload_json"]) == response.json()


def test_admin_deepseek_test_uses_fixed_safe_request_and_persists_sanitized_success(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.main import create_app
    from backend.app.services import providers
    from backend.app.services.providers import ProviderMessage

    configured_settings = sqlite_settings.model_copy(
        update={"deepseek_api_key": "DEEPSEEK_KEY_SENTINEL"}
    )
    observed: dict[str, object] = {}

    async def _successful_generate(self, **kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {"text": "PRIVATE_PROVIDER_RESPONSE_SENTINEL"}

    monkeypatch.setattr(
        providers.HttpxProviderAdapter,
        "generate",
        _successful_generate,
    )

    with TestClient(create_app(settings=configured_settings)) as client:
        _admin_login(client)
        response = client.post(
            "/api/admin/providers/deepseek/test",
            json={
                "prompt": "PRIVATE_PROMPT_SENTINEL",
                "api_key": "CLIENT_KEY_OVERRIDE_SENTINEL",
                "model": "client-model-override",
            },
        )
        health_response = client.get("/api/admin/api-health")

    assert response.status_code == 200
    assert health_response.status_code == 200
    assert response.json()["status"] == "success"
    assert response.json()["provider"] == "deepseek"
    assert response.json()["model"] == "deepseek-v4-pro"
    assert isinstance(response.json()["latency_ms"], int)
    assert observed["messages"] == [
        ProviderMessage(role="user", content="health-check")
    ]
    assert observed["model"] == "deepseek-v4-pro"
    assert observed["api_key"] == "DEEPSEEK_KEY_SENTINEL"
    assert observed["extra_body"] == {"thinking": {"type": "disabled"}}
    manual_test = health_response.json()["manual_test_runs"][0]
    assert {
        key: manual_test[key]
        for key in ("status", "provider", "model", "latency_ms", "error_code")
    } == response.json()

    conn = get_connection(configured_settings)
    try:
        attempt_rows = conn.execute(
            """
            SELECT route, provider, model, status, is_test, session_id
            FROM api_call_logs
            WHERE provider = 'deepseek'
            ORDER BY id
            """
        ).fetchall()
        event = conn.execute(
            """
            SELECT target_id, payload_json
            FROM admin_events
            WHERE action = 'test_agent'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert [dict(row) for row in attempt_rows] == [
        {
            "route": "chat",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "status": "success",
            "is_test": 1,
            "session_id": None,
        }
    ]
    assert event["target_id"] == "deepseek"
    audit_payload = json.loads(event["payload_json"])
    assert audit_payload == response.json()
    serialized = json.dumps(audit_payload, ensure_ascii=False)
    for sentinel in (
        "PRIVATE_PROVIDER_RESPONSE_SENTINEL",
        "PRIVATE_PROMPT_SENTINEL",
        "DEEPSEEK_KEY_SENTINEL",
        "CLIENT_KEY_OVERRIDE_SENTINEL",
    ):
        assert sentinel not in response.text
        assert sentinel not in serialized


def test_admin_deepseek_test_uses_configured_hard_deadline(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.main import create_app
    from backend.app.services import providers

    configured_settings = sqlite_settings.model_copy(
        update={
            "deepseek_api_key": "DEEPSEEK_KEY_SENTINEL",
            "deepseek_timeout_seconds": 0.01,
        }
    )
    cancelled = threading.Event()

    async def _never_returns(self, **kwargs: object) -> dict[str, object]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(providers.HttpxProviderAdapter, "generate", _never_returns)

    started_at = time.perf_counter()
    with TestClient(create_app(settings=configured_settings)) as client:
        _admin_login(client)
        response = client.post("/api/admin/providers/deepseek/test")
    elapsed = time.perf_counter() - started_at

    assert response.status_code == 200
    assert response.json() == {
        "status": "timeout",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "latency_ms": response.json()["latency_ms"],
        "error_code": "timeout",
    }
    assert isinstance(response.json()["latency_ms"], int)
    assert elapsed < 1
    assert cancelled.wait(timeout=1)

    conn = get_connection(configured_settings)
    try:
        rows = conn.execute(
            """
            SELECT provider, model, status, error_code
            FROM api_call_logs
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()
    assert [dict(row) for row in rows] == [
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "status": "timeout",
            "error_code": "timeout",
        }
    ]


@pytest.mark.parametrize(
    ("failure_kind", "expected_status", "expected_error_code"),
    [
        ("transport", "http_error", "transport_error"),
        ("invalid_response", "invalid_response", "invalid_response"),
    ],
)
def test_admin_deepseek_test_transport_and_invalid_response_return_safe_deepseek_result(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_status: str,
    expected_error_code: str,
) -> None:
    from backend.app.main import create_app
    from backend.app.services import providers
    from backend.app.services.providers import (
        InvalidProviderResponseError,
        ProviderTransportError,
    )

    configured_settings = sqlite_settings.model_copy(
        update={"deepseek_api_key": "DEEPSEEK_KEY_SENTINEL"}
    )
    raw_failure = (
        "RAW_EXCEPTION private-provider.invalid PRIVATE_PROMPT_SENTINEL "
        "DEEPSEEK_KEY_SENTINEL"
    )

    async def _failed_generate(self, **kwargs: object) -> dict[str, object]:
        if failure_kind == "transport":
            raise ProviderTransportError(raw_failure)
        raise InvalidProviderResponseError(raw_failure)

    monkeypatch.setattr(providers.HttpxProviderAdapter, "generate", _failed_generate)

    with TestClient(create_app(settings=configured_settings)) as client:
        _admin_login(client)
        response = client.post("/api/admin/providers/deepseek/test")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == expected_status
    assert payload["provider"] == "deepseek"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["error_code"] == expected_error_code
    for sentinel in (
        "RAW_EXCEPTION",
        "private-provider.invalid",
        "PRIVATE_PROMPT_SENTINEL",
        "DEEPSEEK_KEY_SENTINEL",
    ):
        assert sentinel not in response.text

    conn = get_connection(configured_settings)
    try:
        rows = conn.execute(
            """
            SELECT provider, model, status, error_code, error_message_summary
            FROM api_call_logs
            ORDER BY id
            """
        ).fetchall()
        event_payload = conn.execute(
            """
            SELECT payload_json
            FROM admin_events
            WHERE action = 'test_agent'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()["payload_json"]
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0]["provider"] == "deepseek"
    assert rows[0]["model"] == "deepseek-v4-pro"
    assert rows[0]["status"] == expected_status
    assert rows[0]["error_code"] == (
        "transport_error" if failure_kind == "transport" else None
    )
    safe_persistence = json.dumps([dict(row) for row in rows]) + event_payload
    for sentinel in (
        "RAW_EXCEPTION",
        "private-provider.invalid",
        "PRIVATE_PROMPT_SENTINEL",
        "DEEPSEEK_KEY_SENTINEL",
    ):
        assert sentinel not in safe_persistence


def test_admin_deepseek_test_normalizes_provider_http_error_before_persistence_and_refresh(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.main import create_app
    from backend.app.services import providers
    from backend.app.services.providers import ProviderHTTPError

    configured_key = "DEEPSEEK_KEY_SENTINEL"
    raw_host = "private-provider.invalid"
    raw_prompt = "PRIVATE_PROMPT_SENTINEL"
    raw_token = "RAW_BEARER_TOKEN_SENTINEL"
    configured_settings = sqlite_settings.model_copy(
        update={"deepseek_api_key": configured_key}
    )
    raw_error_code = (
        f"upstream_code host={raw_host} prompt={raw_prompt} "
        f"token={raw_token} key={configured_key}"
    )

    async def _failed_generate(self, **kwargs: object) -> dict[str, object]:
        raise ProviderHTTPError(
            status_code=503,
            message=(
                f"upstream message host={raw_host} prompt={raw_prompt} "
                f"token={raw_token} key={configured_key}"
            ),
            error_code=raw_error_code,
        )

    monkeypatch.setattr(providers.HttpxProviderAdapter, "generate", _failed_generate)

    with TestClient(create_app(settings=configured_settings)) as client:
        _admin_login(client)
        response = client.post("/api/admin/providers/deepseek/test")
        health_response = client.get("/api/admin/api-health")
        usage_response = client.get("/api/admin/provider-model-usage")

    assert response.status_code == 200
    assert response.json() == {
        "status": "http_error",
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "latency_ms": response.json()["latency_ms"],
        "error_code": "http_error",
    }
    assert health_response.status_code == 200
    assert usage_response.status_code == 200

    conn = get_connection(configured_settings)
    try:
        attempt = conn.execute(
            """
            SELECT status, http_status, error_code, error_message_summary
            FROM api_call_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        audit_payload = conn.execute(
            """
            SELECT payload_json
            FROM admin_events
            WHERE action = 'test_agent'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()["payload_json"]
    finally:
        conn.close()

    assert dict(attempt) == {
        "status": "http_error",
        "http_status": 503,
        "error_code": "http_error",
        "error_message_summary": "http_error:503:http_error",
    }
    assert json.loads(audit_payload) == response.json()

    health_payload = health_response.json()
    assert health_payload["failure_reasons"] == [
        {
            "route": "chat",
            "status": "http_error",
            "error_code": "http_error",
            "count": 1,
        }
    ]
    assert {
        key: health_payload["manual_test_runs"][0][key]
        for key in ("status", "provider", "model", "latency_ms", "error_code")
    } == response.json()

    all_time = next(
        window
        for window in usage_response.json()["windows"]
        if window["window"] == "all_time"
    )
    provider_row = next(
        row
        for row in all_time["provider_model_rows"]
        if row["provider"] == "deepseek" and row["model"] == "deepseek-v4-pro"
    )
    route_row = next(
        row
        for row in all_time["route_rows"]
        if row["route"] == "chat"
        and row["provider"] == "deepseek"
        and row["model"] == "deepseek-v4-pro"
    )
    for row in (provider_row, route_row):
        assert row["last_failure_code"] == "http_error"
        assert row["last_failure_summary"] == "http_error:503:http_error"

    frontend_refresh_data = json.dumps(
        {
            "response": response.json(),
            "health": health_payload,
            "usage": usage_response.json(),
        },
        ensure_ascii=False,
    )
    persisted_data = json.dumps(dict(attempt), ensure_ascii=False) + audit_payload
    for sentinel in (
        raw_error_code,
        raw_host,
        raw_prompt,
        raw_token,
        configured_key,
    ):
        assert sentinel not in frontend_refresh_data
        assert sentinel not in persisted_data


def test_admin_deepseek_test_releases_write_lock_during_provider_call(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.main import create_app
    from backend.app.services import providers

    configured_settings = sqlite_settings.model_copy(
        update={"deepseek_api_key": "DEEPSEEK_KEY_SENTINEL"}
    )
    client = TestClient(create_app(settings=configured_settings))

    provider_started = threading.Event()
    release_provider = threading.Event()

    async def _blocked_generate(self, **kwargs: object) -> dict[str, object]:
        provider_started.set()
        assert await asyncio.to_thread(release_provider.wait, 5)
        return {"text": "provider result"}

    monkeypatch.setattr(providers.HttpxProviderAdapter, "generate", _blocked_generate)

    with client:
        _admin_login(client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                "/api/admin/providers/deepseek/test",
            )
            assert provider_started.wait(timeout=5)
            writer = get_connection(configured_settings)
            try:
                writer.execute("PRAGMA busy_timeout = 0")
                with transaction(writer):
                    writer.execute(
                        "INSERT OR REPLACE INTO admin_global_controls (key, value) VALUES ('deepseek_test_lock_probe', 'ok')"
                    )
            finally:
                writer.close()
                release_provider.set()
            response = future.result(timeout=5)

    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_obsolete_admin_test_agent_route_is_removed(client: TestClient) -> None:
    with client:
        _admin_login(client)
        response = client.post("/api/admin/test-agent", json={"prompt": "legacy"})

    assert response.status_code == 405


def test_compatibility_export_releases_write_lock_during_archive_build(
    client: TestClient,
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from threading import Event

    from backend.app.repositories import admin as admin_repository
    from backend.app.services.export import ExportResult

    build_started = Event()
    release_build = Event()

    def _blocked_export(conn, settings, output_path, include_test=False):
        build_started.set()
        assert release_build.wait(timeout=5)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"archive")
        return ExportResult(
            output_path=output_path,
            include_test=include_test,
            generated_at="2026-07-12T00:00:00+00:00",
            row_counts={},
        )

    monkeypatch.setattr(admin_repository, "create_v2_export", _blocked_export)

    with client:
        _admin_login(client)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, "/api/admin/export")
            assert build_started.wait(timeout=5)
            writer = get_connection(sqlite_settings)
            try:
                writer.execute("PRAGMA busy_timeout = 0")
                writer.execute(
                    "INSERT OR REPLACE INTO admin_global_controls (key, value) VALUES ('compat_export_lock_probe', 'ok')"
                )
            finally:
                writer.close()
                release_build.set()
            response = future.result(timeout=5)

    assert response.status_code == 200


def test_admin_route_does_not_depend_on_gradio(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    app_main = _patch_frontend_dist(
        monkeypatch,
        tmp_path,
        title="react-admin-without-legacy-dependency",
    )
    real_import = builtins.__import__

    def _failing_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "gradio":
            raise ModuleNotFoundError("No module named 'gradio'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _failing_import)
    sys.modules.pop("gradio", None)

    client = TestClient(app_main.create_app(settings=sqlite_settings))
    with client:
        _admin_login(client)
        response = client.get("/admin")

    assert response.status_code == 200
    assert "react-admin-without-legacy-dependency" in response.text
    assert "gradio" not in response.text.lower()


def test_admin_login_success_sets_cookie_and_records_event(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        response = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert response.status_code == 200
    assert sqlite_settings.admin_session_cookie in response.cookies

    conn = get_connection(sqlite_settings)
    try:
        row = conn.execute(
            """
            SELECT admin_user, action
            FROM admin_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["admin_user"] == "admin"
    assert row["action"] == "login"


def test_admin_login_rejects_invalid_password(client: TestClient):
    with client:
        response = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "wrong-password"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin credentials."}


def test_admin_login_rejects_non_ascii_username_generically_and_audits_failure(
    tmp_path: Path,
):
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'unicode-admin.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="管理员",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
    )

    with TestClient(create_app(settings=settings)) as unicode_client:
        response = unicode_client.post(
            "/api/admin/login",
            json={"username": "不存在", "password": ADMIN_PASSWORD},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin credentials."}

    conn = get_connection(settings)
    try:
        row = conn.execute(
            """
            SELECT payload_json
            FROM admin_events
            WHERE action = 'login'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert json.loads(row["payload_json"])["result"] == "failure"


def test_admin_login_bounds_normalization_expanding_username_and_audits_failure(
    tmp_path: Path,
):
    from backend.app.main import create_app

    expanding_username = "ﬃ" * 128
    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'expanding-username.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
    )

    with TestClient(create_app(settings=settings)) as expanding_client:
        response = expanding_client.post(
            "/api/admin/login",
            json={"username": expanding_username, "password": ADMIN_PASSWORD},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin credentials."}
    assert admin_username_throttle_key(expanding_username) == (
        admin_username_throttle_key("ffi" * 128)
    )

    conn = get_connection(settings)
    try:
        attempt_row = conn.execute(
            "SELECT username_key, state FROM admin_login_attempts"
        ).fetchone()
        audit_row = conn.execute(
            """
            SELECT payload_json
            FROM admin_events
            WHERE action = 'login'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert attempt_row is not None
    assert len(attempt_row["username_key"]) == 64
    assert attempt_row["state"] == "failed"
    assert audit_row is not None
    assert json.loads(audit_row["payload_json"])["result"] == "failure"


def test_admin_login_rejects_oversized_username_before_authentication(
    client: TestClient,
):
    with client:
        response = client.post(
            "/api/admin/login",
            json={"username": "a" * 129, "password": "wrong-password"},
        )

    assert response.status_code == 422


def test_repository_uvicorn_launches_disable_proxy_header_processing():
    operational_suffixes = {".md", ".py", ".service", ".sh", ".toml", ".yaml", ".yml"}
    launch_sources = {
        Path("AGENTS.md"),
        Path("frontend/src/__checks__/manual-smoke.md"),
        *Path(".").glob("README*.md"),
        *(
            path
            for path in Path("scripts").rglob("*")
            if path.is_file() and path.suffix in operational_suffixes
        ),
        *(
            path
            for path in Path("deployment").rglob("*")
            if path.is_file() and path.suffix in operational_suffixes
        ),
    }

    discovered_launches: list[tuple[Path, str]] = []
    for source in launch_sources:
        for line in source.read_text(encoding="utf-8").splitlines():
            if "uvicorn backend.app.main:app" in line:
                discovered_launches.append((source, line))

    assert discovered_launches
    assert all(
        "--no-proxy-headers" in line
        for _source, line in discovered_launches
    ), discovered_launches


def test_admin_login_accepts_encoded_argon2id_password_hash(
    tmp_path: Path,
):
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'modern-admin.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_hash=hash_admin_password(password=ADMIN_PASSWORD),
        admin_password_salt=None,
    )

    with TestClient(create_app(settings=settings)) as modern_client:
        response = modern_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert settings.admin_password_hash.startswith("$argon2id$")
    assert response.status_code == 200


def test_successful_legacy_login_persists_one_time_argon2id_upgrade(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.main import create_app

    with client:
        first_response = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    conn = get_connection(sqlite_settings)
    try:
        credential_rows = conn.execute(
            "SELECT admin_user, password_hash FROM admin_credentials"
        ).fetchall()
    finally:
        conn.close()

    migrated_settings = Settings(
        **{
            **sqlite_settings.model_dump(),
            "admin_password_hash": None,
            "admin_password_salt": None,
        }
    )
    with TestClient(create_app(settings=migrated_settings)) as migrated_client:
        second_response = migrated_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert first_response.status_code == 200
    assert len(credential_rows) == 1
    assert credential_rows[0]["admin_user"] == "admin"
    assert credential_rows[0]["password_hash"].startswith("$argon2id$")
    assert second_response.status_code == 200


def test_admin_login_failures_are_generic_audited_and_credential_free(
    client: TestClient,
    sqlite_settings: Settings,
):
    bad_password = "do-not-persist-this-password"
    with client:
        wrong_password_response = client.post(
            "/api/admin/login",
            json={"username": "admin", "password": bad_password},
        )
        unknown_user_response = client.post(
            "/api/admin/login",
            json={"username": "missing-user", "password": bad_password},
        )

    assert wrong_password_response.status_code == 401
    assert unknown_user_response.status_code == 401
    assert wrong_password_response.json() == unknown_user_response.json() == {
        "detail": "Invalid admin credentials."
    }

    conn = get_connection(sqlite_settings)
    try:
        rows = conn.execute(
            """
            SELECT admin_user, action, payload_json
            FROM admin_events
            WHERE action = 'login'
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2
    serialized_events = json.dumps(
        [dict(row) for row in rows],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert all(json.loads(row["payload_json"])["result"] == "failure" for row in rows)
    for forbidden_value in (
        bad_password,
        sqlite_settings.admin_password_hash,
        sqlite_settings.admin_password_salt,
        sqlite_settings.admin_session_cookie,
        "password",
        "cookie",
        "token",
        "salt",
        "hash",
    ):
        assert forbidden_value not in serialized_events


def test_admin_login_throttles_normalized_username_across_client_addresses(
    tmp_path: Path,
):
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'username-throttle.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        admin_login_max_failures=2,
        admin_login_window_seconds=300,
    )
    app = create_app(settings=settings)

    with TestClient(app, client=("198.51.100.10", 4100)) as first_client:
        first_response = first_client.post(
            "/api/admin/login",
            json={"username": " ADMIN ", "password": "wrong"},
        )
    with TestClient(app, client=("198.51.100.11", 4101)) as second_client:
        second_response = second_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "wrong"},
        )
    with TestClient(app, client=("198.51.100.12", 4102)) as third_client:
        throttled_response = third_client.post(
            "/api/admin/login",
            json={"username": "Admin", "password": ADMIN_PASSWORD},
        )

    assert first_response.status_code == 401
    assert second_response.status_code == 401
    assert throttled_response.status_code == 401
    assert throttled_response.json() == first_response.json() == {
        "detail": "Invalid admin credentials."
    }

    conn = get_connection(settings)
    try:
        states = [
            row["state"]
            for row in conn.execute(
                "SELECT state FROM admin_login_attempts ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert states == ["failed", "failed"]


def test_admin_login_throttles_direct_client_address_and_ignores_forwarded_header(
    tmp_path: Path,
):
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'address-throttle.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        admin_login_max_failures=1,
        admin_login_window_seconds=300,
    )
    app = create_app(settings=settings)

    with TestClient(app, client=("198.51.100.20", 4200)) as address_client:
        first_response = address_client.post(
            "/api/admin/login",
            headers={"x-forwarded-for": "203.0.113.10"},
            json={"username": "missing-one", "password": "wrong"},
        )
        throttled_response = address_client.post(
            "/api/admin/login",
            headers={"x-forwarded-for": "203.0.113.11"},
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert first_response.status_code == 401
    assert throttled_response.status_code == 401
    assert throttled_response.json() == first_response.json()

    conn = get_connection(settings)
    try:
        attempts = conn.execute(
            """
            SELECT username_key, client_address, state
            FROM admin_login_attempts
            ORDER BY id
            """
        ).fetchall()
        audit_results = [
            json.loads(row["payload_json"])["result"]
            for row in conn.execute(
                """
                SELECT payload_json
                FROM admin_events
                WHERE action = 'login'
                ORDER BY id
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    assert [row["client_address"] for row in attempts] == ["198.51.100.20"]
    assert [row["state"] for row in attempts] == ["failed"]
    assert audit_results == ["failure", "throttled"]


def test_concurrent_login_reservations_bound_kdf_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'concurrent-throttle.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        admin_login_max_failures=2,
        admin_login_window_seconds=300,
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    entered_count = 0
    entered_lock = threading.Lock()
    capacity_reached = threading.Event()
    excess_kdf_started = threading.Event()
    release_kdf = threading.Event()

    def blocking_verifier(**_kwargs):
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == settings.admin_login_max_failures:
                capacity_reached.set()
            elif entered_count > settings.admin_login_max_failures:
                excess_kdf_started.set()
        assert release_kdf.wait(timeout=5)
        return False

    monkeypatch.setattr(app_main, "verify_admin_credentials", blocking_verifier)
    app = app_main.create_app(settings=settings)
    clients = [
        TestClient(app, client=(f"198.51.100.{index + 30}", 4300 + index))
        for index in range(6)
    ]

    def attempt_login(login_client: TestClient):
        return login_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": "wrong"},
        )

    with ThreadPoolExecutor(max_workers=len(clients)) as executor:
        futures = [executor.submit(attempt_login, login_client) for login_client in clients]
        try:
            assert capacity_reached.wait(timeout=5)
            assert not excess_kdf_started.wait(timeout=0.5)
        finally:
            release_kdf.set()
        responses = [future.result(timeout=5) for future in futures]

    assert entered_count == settings.admin_login_max_failures
    assert all(response.status_code == 401 for response in responses)

    conn = get_connection(settings)
    try:
        states = [
            row["state"]
            for row in conn.execute(
                "SELECT state FROM admin_login_attempts ORDER BY id"
            ).fetchall()
        ]
        audit_results = [
            json.loads(row["payload_json"])["result"]
            for row in conn.execute(
                "SELECT payload_json FROM admin_events ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert states == ["failed", "failed"]
    assert sorted(audit_results) == [
        "failure",
        "failure",
        "throttled",
        "throttled",
        "throttled",
        "throttled",
    ]


def test_login_reservation_heartbeat_blocks_extra_kdf_past_initial_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main
    from backend.app.admin import auth as admin_auth

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'reservation-heartbeat.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        admin_login_max_failures=1,
        admin_login_window_seconds=1,
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    monkeypatch.setattr(admin_auth, "LOGIN_RESERVATION_TTL_SECONDS", 1)
    monkeypatch.setattr(
        admin_auth,
        "LOGIN_RESERVATION_HEARTBEAT_SECONDS",
        0.2,
        raising=False,
    )
    entered_count = 0
    entered_lock = threading.Lock()
    first_kdf_entered = threading.Event()
    extra_kdf_entered = threading.Event()
    release_kdf = threading.Event()

    def blocking_verifier(**_kwargs):
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            entry_number = entered_count
            if entry_number == 1:
                first_kdf_entered.set()
            else:
                extra_kdf_entered.set()
        if entry_number == 1:
            assert release_kdf.wait(timeout=5)
        return False

    monkeypatch.setattr(app_main, "verify_admin_credentials", blocking_verifier)
    app = app_main.create_app(settings=settings)
    first_client = TestClient(app, client=("198.51.100.80", 4800))
    second_client = TestClient(app, client=("198.51.100.81", 4801))

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(
            first_client.post,
            "/api/admin/login",
            json={"username": "admin", "password": "wrong"},
        )
        try:
            assert first_kdf_entered.wait(timeout=5)
            time.sleep(1.4)
            second_response = second_client.post(
                "/api/admin/login",
                json={"username": "admin", "password": "wrong"},
            )
            assert not extra_kdf_entered.is_set()
        finally:
            release_kdf.set()
        first_response = first_future.result(timeout=5)

    assert entered_count == 1
    assert first_response.status_code == 401
    assert second_response.status_code == 401
    assert second_response.json() == first_response.json()


def test_admin_login_fails_closed_and_audits_when_reservation_ownership_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main
    from backend.app.admin.auth import verify_admin_credentials as real_verifier

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'lost-reservation.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_hash=hash_admin_password(password=ADMIN_PASSWORD),
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
    finally:
        conn.close()

    def verifier_that_loses_reservation(**kwargs):
        result = real_verifier(**kwargs)
        conn = get_connection(settings)
        try:
            conn.execute("DELETE FROM admin_login_attempts WHERE state = 'pending'")
        finally:
            conn.close()
        return result

    monkeypatch.setattr(
        app_main,
        "verify_admin_credentials",
        verifier_that_loses_reservation,
    )

    with TestClient(app_main.create_app(settings=settings)) as lost_client:
        response = lost_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid admin credentials."}
    assert settings.admin_session_cookie not in response.cookies

    conn = get_connection(settings)
    try:
        audit_rows = conn.execute(
            "SELECT payload_json FROM admin_events WHERE action = 'login'"
        ).fetchall()
    finally:
        conn.close()

    assert len(audit_rows) == 1
    assert json.loads(audit_rows[0]["payload_json"])["result"] == "failure"


def test_concurrent_legacy_logins_create_one_upgrade_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main
    from backend.app.admin.auth import verify_admin_credentials as real_verifier

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'concurrent-migration.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_salt=ADMIN_SALT,
        admin_password_hash=_password_hash(ADMIN_PASSWORD),
        admin_login_max_failures=5,
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
        conn.execute(
            """
            CREATE TRIGGER reject_admin_credential_update
            BEFORE UPDATE ON admin_credentials
            BEGIN
                SELECT RAISE(FAIL, 'admin credential must not be overwritten');
            END
            """
        )
    finally:
        conn.close()

    legacy_verification_barrier = threading.Barrier(2)
    modern_reverification_count = 0
    verification_lock = threading.Lock()

    def synchronized_verifier(**kwargs):
        nonlocal modern_reverification_count
        result = real_verifier(**kwargs)
        if kwargs["persisted_password_hash"] is None:
            legacy_verification_barrier.wait(timeout=5)
        else:
            with verification_lock:
                modern_reverification_count += 1
        return result

    monkeypatch.setattr(app_main, "verify_admin_credentials", synchronized_verifier)
    app = app_main.create_app(settings=settings)
    clients = [
        TestClient(app, client=(f"198.51.100.{index + 50}", 4500 + index))
        for index in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                login_client.post,
                "/api/admin/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            for login_client in clients
        ]
        responses = [future.result(timeout=10) for future in futures]

    assert [response.status_code for response in responses] == [200, 200]

    conn = get_connection(settings)
    try:
        credential_rows = conn.execute(
            "SELECT admin_user, password_hash FROM admin_credentials"
        ).fetchall()
        attempt_count = conn.execute(
            "SELECT COUNT(*) AS count FROM admin_login_attempts"
        ).fetchone()["count"]
        audit_results = [
            json.loads(row["payload_json"])["result"]
            for row in conn.execute(
                "SELECT payload_json FROM admin_events ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()

    assert len(credential_rows) == 1
    assert credential_rows[0]["admin_user"] == "admin"
    assert credential_rows[0]["password_hash"].startswith("$argon2id$")
    assert modern_reverification_count == 1
    assert attempt_count == 0
    assert audit_results == ["success", "success"]


def test_expired_pending_login_reservation_does_not_throttle_after_crash(
    tmp_path: Path,
):
    from backend.app.main import create_app

    settings = Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'expired-reservation.db'}",
        app_secret_key="SESSION_SIGNING_TEST_VALUE",
        admin_user="admin",
        admin_password_hash=hash_admin_password(password=ADMIN_PASSWORD),
        admin_login_max_failures=1,
    )
    conn = get_connection(settings)
    try:
        run_migrations(conn)
        conn.execute(
            """
            INSERT INTO admin_login_attempts (
                reservation_token,
                username_key,
                client_address,
                state,
                expires_at
            ) VALUES ('expired-token', ?, '198.51.100.70', 'pending', datetime('now', '-1 second'))
            """,
            ("a" * 64,),
        )
    finally:
        conn.close()

    with TestClient(
        create_app(settings=settings),
        client=("198.51.100.70", 4700),
    ) as expired_client:
        response = expired_client.post(
            "/api/admin/login",
            json={"username": "admin", "password": ADMIN_PASSWORD},
        )

    assert response.status_code == 200

    conn = get_connection(settings)
    try:
        attempt_count = conn.execute(
            "SELECT COUNT(*) AS count FROM admin_login_attempts"
        ).fetchone()["count"]
    finally:
        conn.close()

    assert attempt_count == 0


def test_export_job_endpoints_require_admin_auth(client: TestClient):
    response = client.get("/api/admin/export-jobs")
    assert response.status_code == 401
    assert response.json() == {"detail": "Admin login required."}

    response = client.delete("/api/admin/export-jobs/missing-job")
    assert response.status_code == 401
    assert response.json() == {"detail": "Admin login required."}

    response = client.post(
        "/api/admin/export-jobs",
        json={
            "export_type": "experiment_data",
            "filters": {},
            "include_test": False,
        },
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Admin login required."}


def test_admin_can_create_list_get_and_download_export_job(
    client: TestClient,
    sqlite_settings: Settings,
):
    with client:
        _admin_login(client)
        create_response = client.post(
            "/api/admin/export-jobs",
            json={
                "export_type": "experiment_data",
                "filters": {},
                "include_test": False,
            },
        )
        assert create_response.status_code == 200
        job_payload = create_response.json()
        job_uuid = job_payload["job_uuid"]

        list_response = client.get("/api/admin/export-jobs")
        get_response = client.get(f"/api/admin/export-jobs/{job_uuid}")
        download_response = client.get(f"/api/admin/export-jobs/{job_uuid}/download")

    assert list_response.status_code == 200
    assert get_response.status_code == 200
    assert download_response.status_code == 200
    assert job_payload["status"] == "queued"
    assert get_response.json()["status"] == "succeeded"
    assert get_response.json()["job_uuid"] == job_uuid
    assert any(item["job_uuid"] == job_uuid for item in list_response.json()["items"])
    assert download_response.headers["content-type"] == "application/zip"


def test_admin_can_delete_succeeded_export_job_and_archive_file(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.services.export_jobs import create_export_job

    with client:
        _admin_login(client)
        conn = get_connection(sqlite_settings)
        try:
            job = create_export_job(
                conn,
                export_type="experiment_data",
                filters={},
                include_test=False,
                created_by="admin",
            )
            job_uuid = str(job["job_uuid"])
            export_path = sqlite_settings.data_dir / "exports" / f"{job_uuid}.zip"
            export_path.parent.mkdir(parents=True, exist_ok=True)
            export_path.write_bytes(b"export archive")
            conn.execute(
                """
                UPDATE export_jobs
                SET status = 'succeeded',
                    output_path = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE job_uuid = ?
                """,
                (str(export_path), job_uuid),
            )
        finally:
            conn.close()

        delete_response = client.delete(f"/api/admin/export-jobs/{job_uuid}")
        get_response = client.get(f"/api/admin/export-jobs/{job_uuid}")

    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_file"] is True
    assert get_response.status_code == 404
    assert not export_path.exists()


def test_admin_delete_export_job_rejects_running_or_queued_jobs(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.services.export_jobs import create_export_job

    with client:
        _admin_login(client)
        conn = get_connection(sqlite_settings)
        try:
            job = create_export_job(
                conn,
                export_type="experiment_data",
                filters={},
                include_test=False,
                created_by="admin",
            )
            job_uuid = str(job["job_uuid"])
        finally:
            conn.close()

        delete_response = client.delete(f"/api/admin/export-jobs/{job_uuid}")
        get_response = client.get(f"/api/admin/export-jobs/{job_uuid}")

    assert delete_response.status_code == 409
    assert delete_response.json() == {
        "detail": "Export job is queued or running and cannot be deleted."
    }
    assert get_response.status_code == 200


def test_admin_export_job_rejects_reimbursement_include_test(client: TestClient):
    with client:
        _admin_login(client)
        response = client.post(
            "/api/admin/export-jobs",
            json={
                "export_type": "reimbursement",
                "filters": {},
                "include_test": True,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "reimbursement exports do not support include_test."
    }


def test_admin_download_export_job_rejects_not_ready_and_outside_exports_dir(
    client: TestClient,
    sqlite_settings: Settings,
):
    from backend.app.services.export_jobs import create_export_job

    with client:
        _admin_login(client)
        conn = get_connection(sqlite_settings)
        try:
            queued_job = create_export_job(
                conn,
                export_type="experiment_data",
                filters={},
                include_test=False,
                created_by="admin",
            )
            job_uuid = str(queued_job["job_uuid"])
        finally:
            conn.close()

        not_ready_response = client.get(f"/api/admin/export-jobs/{job_uuid}/download")
        assert not_ready_response.status_code == 409

        conn = get_connection(sqlite_settings)
        try:
            export_dir = sqlite_settings.data_dir / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            export_dir_output = export_dir / "directory-output"
            export_dir_output.mkdir(parents=True, exist_ok=True)
            conn.execute(
                """
                UPDATE export_jobs
                SET status = 'succeeded',
                    output_path = ?,
                    completed_at = CURRENT_TIMESTAMP
                WHERE job_uuid = ?
                """,
                (
                    str(export_dir_output),
                    job_uuid,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        file_response = client.get(f"/api/admin/export-jobs/{job_uuid}/download")

        conn = get_connection(sqlite_settings)
        try:
            conn.execute(
                """
                UPDATE export_jobs
                SET output_path = ?
                WHERE job_uuid = ?
                """,
                (
                    str(sqlite_settings.data_dir / "not-allowed.zip"),
                    job_uuid,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        outside_response = client.get(f"/api/admin/export-jobs/{job_uuid}/download")

    assert file_response.status_code == 404
    assert file_response.json() == {"detail": "Export file not found."}
    assert outside_response.status_code == 400
    assert outside_response.json() == {"detail": "Invalid export path."}
