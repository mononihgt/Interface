from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.settings import Settings


def test_health_returns_expected_metadata_without_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "super-secret-value")

    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["app"] == "interface_v2"
    assert payload["env"] == "test"
    assert "database" in payload
    assert payload["database"]["reachable"] is None
    assert payload["database"]["status"] == "not_checked"
    assert "date" in payload
    assert "secret" not in payload
    assert "openai_api_key" not in payload
    assert "super-secret-value" not in response.text


@pytest.fixture
def readiness_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    for directory_name in ("audio", "exports", "logs"):
        (data_dir / directory_name).mkdir(parents=True, exist_ok=True)
    return Settings(
        app_env="test",
        data_dir=data_dir,
        database_url=f"sqlite:///{data_dir / 'app.db'}",
    )


@pytest.fixture
def frontend_dist(tmp_path: Path) -> Path:
    dist_dir = tmp_path / "frontend" / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "assets" / "app.js").write_text(
        "console.log('ready');",
        encoding="utf-8",
    )
    (dist_dir / "index.html").write_text(
        '<!doctype html><script type="module" src="/assets/app.js"></script>',
        encoding="utf-8",
    )
    return dist_dir


def test_readiness_proves_database_migrations_storage_and_frontend(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "components": {
            "database": {"status": "ready", "reason": None},
            "migrations": {"status": "ready", "reason": None},
            "storage": {"status": "ready", "reason": None},
            "frontend": {"status": "ready", "reason": None},
            "providers": {"status": "ready", "reason": None},
        },
    }
    conn = sqlite3.connect(readiness_settings.data_dir / "app.db")
    try:
        probe_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE name = 'readiness_write_probe'"
        ).fetchone()
    finally:
        conn.close()
    assert probe_table is None


def test_readiness_reports_unwritable_storage_without_paths(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)
    audio_dir = readiness_settings.data_dir / "audio"
    audio_dir.chmod(0o500)
    try:
        with TestClient(app_main.create_app(settings=readiness_settings)) as client:
            response = client.get("/api/readiness")
    finally:
        audio_dir.chmod(0o700)

    assert response.status_code == 503
    assert response.json()["components"]["storage"] == {
        "status": "not_ready",
        "reason": "storage_not_writable",
    }
    assert str(readiness_settings.data_dir) not in response.text


def test_readiness_reports_missing_frontend_assets(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    (frontend_dist / "assets" / "app.js").unlink()
    (frontend_dist / "assets").rmdir()
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_assets_missing",
    }


def test_readiness_rejects_empty_assets_directory(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    (frontend_dist / "assets" / "app.js").unlink()
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_assets_empty",
    }


def test_readiness_rejects_missing_referenced_frontend_bundle(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    (frontend_dist / "index.html").write_text(
        '<!doctype html><script src="/assets/missing.js"></script>',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_asset_missing",
    }


def test_readiness_rejects_unreadable_referenced_frontend_bundle(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    asset_path = frontend_dist / "assets" / "app.js"
    asset_path.chmod(0)
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)
    try:
        with TestClient(app_main.create_app(settings=readiness_settings)) as client:
            response = client.get("/api/readiness")
    finally:
        asset_path.chmod(0o600)

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_asset_unreadable",
    }


def test_readiness_rejects_empty_referenced_frontend_bundle(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    (frontend_dist / "assets" / "app.js").write_bytes(b"")
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_asset_empty",
    }


def test_readiness_rejects_placeholder_index_without_local_bundle_reference(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    (frontend_dist / "index.html").write_text(
        "<!doctype html><title>placeholder</title>",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_bundle_reference_missing",
    }


def test_readiness_stays_non_ready_when_build_appears_after_app_construction(
    readiness_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    dist_dir = tmp_path / "late-frontend" / "dist"
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: dist_dir)
    app = app_main.create_app(settings=readiness_settings)

    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (assets_dir / "app.js").write_text("console.log('late');", encoding="utf-8")
    (dist_dir / "index.html").write_text(
        '<!doctype html><script src="/assets/app.js"></script>',
        encoding="utf-8",
    )

    with TestClient(app) as client:
        readiness_response = client.get("/api/readiness")
        asset_response = client.get("/assets/app.js")
        spa_response = client.get("/welcome")

    assert readiness_response.status_code == 503
    assert readiness_response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_static_routes_unavailable",
    }
    assert asset_response.status_code == 404
    assert spa_response.status_code == 404


def test_readiness_rejects_assets_root_swap_during_reference_open(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    from backend.app import main as app_main

    assets_dir = frontend_dist / "assets"
    owned_assets = frontend_dist / "assets-owned"
    outside_assets = tmp_path / "outside-assets"
    outside_assets.mkdir()
    (outside_assets / "app.js").write_text("outside", encoding="utf-8")
    original_os_open = app_main.os.open
    swapped = False

    def swap_assets_before_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if (
            not swapped
            and (Path(path) == assets_dir / "app.js" or path == "app.js")
        ):
            swapped = True
            assets_dir.rename(owned_assets)
            assets_dir.symlink_to(outside_assets, target_is_directory=True)
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(app_main.os, "open", swap_assets_before_open)
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert swapped is True
    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_assets_missing",
    }


def test_readiness_rejects_frontend_reference_outside_assets_without_path_disclosure(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    outside_asset = frontend_dist.parent / "outside.js"
    outside_asset.write_text("outside", encoding="utf-8")
    (frontend_dist / "index.html").write_text(
        '<!doctype html><link rel="modulepreload" href="../outside.js">',
        encoding="utf-8",
    )
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": "frontend_asset_reference_outside_assets",
    }
    assert str(frontend_dist) not in response.text
    assert str(outside_asset) not in response.text


@pytest.mark.parametrize(
    ("index_state", "expected_reason"),
    [
        ("empty", "frontend_entrypoint_empty"),
        ("unreadable", "frontend_entrypoint_unreadable"),
    ],
)
def test_readiness_requires_readable_nonempty_frontend_entrypoint(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_state: str,
    expected_reason: str,
):
    from backend.app import main as app_main

    index_path = frontend_dist / "index.html"
    if index_state == "empty":
        index_path.write_bytes(b"")
    else:
        index_path.chmod(0)
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)
    try:
        with TestClient(app_main.create_app(settings=readiness_settings)) as client:
            response = client.get("/api/readiness")
    finally:
        index_path.chmod(0o600)

    assert response.status_code == 503
    assert response.json()["components"]["frontend"] == {
        "status": "not_ready",
        "reason": expected_reason,
    }


def test_readiness_reports_migration_drift(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)
    with TestClient(app_main.create_app(settings=readiness_settings)) as client:
        conn = sqlite3.connect(readiness_settings.data_dir / "app.db")
        try:
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ('999_unexpected')"
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get("/api/readiness")

    assert response.status_code == 503
    assert response.json()["components"]["migrations"] == {
        "status": "not_ready",
        "reason": "migration_state_mismatch",
    }
    assert "999_unexpected" not in response.text


def test_production_readiness_requires_provider_and_asr_settings_without_disclosure(
    readiness_settings: Settings,
    frontend_dist: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from backend.app import main as app_main

    production_settings = readiness_settings.model_copy(
        update={
            "app_env": "production",
            "yizhan_api_key": "PROVIDER_SECRET_SENTINEL",
            "aabao_api_key": "PROVIDER_SECRET_SENTINEL",
            "packyapi_api_key": "PROVIDER_SECRET_SENTINEL",
            "deepseek_api_key": None,
            "tencent_secret_id": "PROVIDER_SECRET_SENTINEL",
            "tencent_secret_key": "PROVIDER_SECRET_SENTINEL",
        }
    )
    monkeypatch.setattr(app_main, "get_frontend_dist_dir", lambda: frontend_dist)

    with TestClient(app_main.create_app(settings=production_settings)) as client:
        readiness_response = client.get("/api/readiness")
        liveness_response = client.get("/api/health")

    assert readiness_response.status_code == 503
    assert readiness_response.json()["components"]["providers"] == {
        "status": "not_ready",
        "reason": "required_provider_settings_missing",
    }
    assert "PROVIDER_SECRET_SENTINEL" not in readiness_response.text
    assert "api_key" not in readiness_response.text
    assert "secret" not in readiness_response.text
    assert liveness_response.status_code == 200
