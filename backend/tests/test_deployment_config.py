from pathlib import Path

from backend.app.settings import Settings


ROOT_DIR = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT_DIR / "deployment" / "interface-v2.env.example"
SERVICE_FILE = ROOT_DIR / "deployment" / "interface-v2.service"

EXPECTED_ENV_KEYS = {
    "APP_BASE_URL",
    "APP_SECRET_KEY",
    "DATA_DIR",
    "DATABASE_URL",
    "ADMIN_USER",
    "ADMIN_PASSWORD_HASH",
    "YIZHAN_BASE_URL",
    "YIZHAN_API_KEY",
    "AABAO_BASE_URL",
    "AABAO_API_KEY",
    "PACKYAPI_BASE_URL",
    "PACKYAPI_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "MAIN_MODEL_PRIMARY",
    "MAIN_MODEL_FALLBACK",
    "TENCENT_SECRET_ID",
    "TENCENT_SECRET_KEY",
    "TENCENT_ASR_ENDPOINT",
}


def _active_assignments(path: Path) -> dict[str, str]:
    assignments = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, value = line.split("=", maxsplit=1)
        assignments[name] = value
    return assignments


def test_environment_example_contains_only_operator_managed_settings():
    assignments = _active_assignments(ENV_EXAMPLE)

    assert set(assignments) == EXPECTED_ENV_KEYS
    assert {name.lower() for name in assignments} <= set(Settings.model_fields)
    assert assignments["DATA_DIR"] == "/var/lib/interface-v2"
    assert assignments["DATABASE_URL"] == "sqlite:////var/lib/interface-v2/app.db"
    assert (
        assignments["TENCENT_ASR_ENDPOINT"]
        == "asr.ap-hongkong.tencentcloudapi.com"
    )


def test_service_fixes_production_python_runtime_settings():
    service_text = SERVICE_FILE.read_text(encoding="utf-8")

    assert "Environment=APP_ENV=production" in service_text
    assert "Environment=PYTHONDONTWRITEBYTECODE=1" in service_text
    assert "Environment=PYTHONUNBUFFERED=1" in service_text
    assert "EnvironmentFile=/etc/interface-v2/interface-v2.env" in service_text
