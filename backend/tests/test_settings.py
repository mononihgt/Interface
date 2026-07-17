from backend.app.settings import Settings


def test_settings_loads_spec_env_names(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/app.db")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("YIZHAN_API_KEY", "yizhan-key-123")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key-123")
    monkeypatch.setenv("DEEPSEEK_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv(
        "OPEN_METEO_GEOCODING_URL",
        "https://geocoding.example.test/v1/search",
    )
    monkeypatch.setenv(
        "OPEN_METEO_FORECAST_URL",
        "https://forecast.example.test/v1/forecast",
    )
    monkeypatch.setenv("OPEN_METEO_LANGUAGE", "en")
    monkeypatch.setenv("OPEN_METEO_COUNTRY_CODE", "CN")
    monkeypatch.setenv("OPEN_METEO_TIMEOUT_SECONDS", "18.5")
    monkeypatch.setenv("ASR_MAX_UPLOAD_BYTES", "8388608")
    monkeypatch.setenv("ASR_MAX_REQUEST_BYTES", "8454144")
    monkeypatch.setenv("ASR_ALLOWED_MEDIA_TYPES", "audio/webm,audio/mp4")
    monkeypatch.setenv("ASR_MAX_DURATION_SECONDS", "45")
    monkeypatch.setenv("ADMIN_LOGIN_MAX_FAILURES", "7")
    monkeypatch.setenv("ADMIN_LOGIN_WINDOW_SECONDS", "900")
    monkeypatch.setenv("BACKUP_MAX_MEMBER_BYTES", "17179869184")
    monkeypatch.setenv("BACKUP_MAX_TOTAL_UNCOMPRESSED_BYTES", "274877906944")
    monkeypatch.setenv("BACKUP_MAX_COMPRESSION_RATIO", "200")
    monkeypatch.setenv("BACKUP_MAX_CENTRAL_DIRECTORY_BYTES", "67108864")
    monkeypatch.setenv("BACKUP_MAX_MEMBERS", "100000")

    settings = Settings()

    assert settings.app_env == "production"
    assert settings.database_url == "sqlite:///data/app.db"
    assert settings.data_dir == data_dir
    assert settings.yizhan_api_key == "yizhan-key-123"
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_api_key == "deepseek-key-123"
    assert settings.deepseek_model == "deepseek-v4-pro"
    assert settings.deepseek_timeout_seconds == 12.5
    assert settings.open_meteo_geocoding_url == "https://geocoding.example.test/v1/search"
    assert settings.open_meteo_forecast_url == "https://forecast.example.test/v1/forecast"
    assert settings.open_meteo_language == "en"
    assert settings.open_meteo_country_code == "CN"
    assert settings.open_meteo_timeout_seconds == 18.5
    assert not hasattr(settings, "test_channel_provider")
    assert settings.asr_max_upload_bytes == 8_388_608
    assert settings.asr_max_request_bytes == 8_454_144
    assert settings.asr_allowed_media_types == "audio/webm,audio/mp4"
    assert settings.asr_max_duration_seconds == 45
    assert settings.admin_login_max_failures == 7
    assert settings.admin_login_window_seconds == 900
    assert settings.backup_max_member_bytes == 17_179_869_184
    assert settings.backup_max_total_uncompressed_bytes == 274_877_906_944
    assert settings.backup_max_compression_ratio == 200
    assert settings.backup_max_central_directory_bytes == 67_108_864
    assert settings.backup_max_members == 100_000


def test_admin_login_security_settings_have_bounded_defaults():
    settings = Settings()

    assert settings.admin_login_max_failures == 5
    assert settings.admin_login_window_seconds == 300


def test_open_meteo_settings_have_public_keyless_defaults():
    settings = Settings()

    assert settings.open_meteo_geocoding_url == (
        "https://geocoding-api.open-meteo.com/v1/search"
    )
    assert settings.open_meteo_forecast_url == "https://api.open-meteo.com/v1/forecast"
    assert settings.open_meteo_language == "zh"
    assert settings.open_meteo_country_code == ""
    assert settings.open_meteo_timeout_seconds == 20.0
    assert not hasattr(settings, "open_meteo_api_key")
