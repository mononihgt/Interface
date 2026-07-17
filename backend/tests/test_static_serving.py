from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.settings import Settings


@pytest.fixture
def sqlite_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "static-serving.db"
    return Settings(
        app_env="test",
        data_dir=tmp_path,
        database_url=f"sqlite:///{db_path}",
        app_secret_key="STATIC_SERVING_SESSION_SECRET",
        admin_user="admin",
        admin_password_salt="admin-salt",
        admin_password_hash="admin-hash",
    )


def test_spa_routes_and_assets_are_served_from_frontend_dist(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from backend.app import main as app_main

    dist_dir = tmp_path / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        '<!doctype html><title>task14</title><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("console.log('task14 asset');", encoding="utf-8")

    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: dist_dir)

    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        root_response = client.get("/")
        welcome_response = client.get("/welcome")
        asset_response = client.get("/assets/app.js")

    assert root_response.status_code == 200
    assert welcome_response.status_code == 200
    assert asset_response.status_code == 200
    assert "task14" in root_response.text
    assert "task14" in welcome_response.text
    assert asset_response.text == "console.log('task14 asset');"


def test_api_routes_are_not_shadowed_and_admin_serves_react_dashboard(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from backend.app import main as app_main

    dist_dir = tmp_path / "frontend" / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<!doctype html><title>spa only</title>", encoding="utf-8")

    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: dist_dir)

    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        health_response = client.get("/api/health")
        admin_response = client.get("/admin")
        admin_console_response = client.get("/admin/console", follow_redirects=False)
        admin_console_slash_response = client.get(
            "/admin/console/",
            follow_redirects=False,
        )

    assert health_response.status_code == 200
    assert health_response.json()["app"] == "interface_v2"
    assert admin_response.status_code == 200
    assert admin_response.headers["content-type"].startswith("text/html")
    assert "spa only" in admin_response.text
    assert "/api/admin/login" not in admin_response.text
    assert admin_console_response.status_code == 303
    assert admin_console_response.headers["location"] == "/admin"
    assert admin_console_slash_response.status_code == 303
    assert admin_console_slash_response.headers["location"] == "/admin"


def test_manifest_json_is_served_for_admin_console_browser_requests(
    sqlite_settings: Settings,
):
    from backend.app.main import create_app

    client = TestClient(create_app(settings=sqlite_settings))

    with client:
        response = client.get("/manifest.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/manifest+json")
    assert response.json()["name"] == "interface_v2"


def test_missing_frontend_build_keeps_api_working_and_unknown_routes_controlled(
    sqlite_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from backend.app import main as app_main

    missing_dist_dir = tmp_path / "frontend" / "dist"
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: missing_dist_dir)

    client = TestClient(app_main.create_app(settings=sqlite_settings))

    with client:
        health_response = client.get("/api/health")
        readiness_response = client.get("/api/readiness")
        unknown_response = client.get("/welcome")

    assert health_response.status_code == 200
    assert readiness_response.status_code == 503
    assert readiness_response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_entrypoint_missing",
    }
    assert unknown_response.status_code == 404
