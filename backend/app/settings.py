from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "interface_v2"
    app_env: str = "development"
    app_base_url: str = ""
    app_secret_key: Optional[str] = None
    session_cookie_name: str = "aitrust_v2_sid"
    session_ttl_seconds: int = 21_600
    debug: bool = False
    api_prefix: str = "/api"
    data_dir: Path = ROOT_DIR / "data"
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'app.db').as_posix()}"
    log_level: str = "INFO"
    backup_max_member_bytes: int = Field(
        default=17_179_869_184,
        ge=1,
    )
    backup_max_total_uncompressed_bytes: int = Field(
        default=274_877_906_944,
        ge=1,
    )
    backup_max_compression_ratio: float = Field(default=200.0, ge=1.0)
    backup_max_central_directory_bytes: int = Field(
        default=67_108_864,
        ge=1,
    )
    backup_max_members: int = Field(default=100_000, ge=1)
    admin_user: str = "admin"
    admin_password_hash: Optional[str] = None
    admin_password_salt: Optional[str] = None
    admin_session_cookie: str = "aitrust_v2_admin_sid"
    admin_login_max_failures: int = Field(default=5, ge=1, le=100)
    admin_login_window_seconds: int = Field(default=300, ge=1, le=86_400)
    yizhan_base_url: str = "https://vip.yi-zhan.top/v1"
    yizhan_api_key: Optional[str] = None
    aabao_base_url: str = "https://api.aabao.ai/v1"
    aabao_api_key: Optional[str] = None
    packyapi_base_url: str = "https://www.packyapi.com/v1"
    packyapi_api_key: Optional[str] = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: Optional[str] = None
    deepseek_model: str = "deepseek-v4-pro"
    deepseek_timeout_seconds: float = Field(default=15.0, gt=0, le=60)
    main_model_primary: str = "gpt-5.1"
    main_model_fallback: str = "gpt-5"
    evaluator_model: str = "gemini-3.5-flash"
    error_semantic_max_attempts: int = Field(default=5, ge=1, le=5)
    error_semantic_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    provider_timeout_seconds: float = 30.0
    provider_cooldown_seconds: int = 300
    provider_unauthorized_cooldown_seconds: int = 1_800
    open_meteo_geocoding_url: str = (
        "https://geocoding-api.open-meteo.com/v1/search"
    )
    open_meteo_forecast_url: str = "https://api.open-meteo.com/v1/forecast"
    open_meteo_language: str = "zh"
    open_meteo_country_code: str = ""
    open_meteo_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    tencent_secret_id: Optional[str] = None
    tencent_secret_key: Optional[str] = None
    tencent_asr_endpoint: str = "asr.ap-hongkong.tencentcloudapi.com"
    asr_max_retry_per_turn: int = 3
    asr_max_upload_bytes: int = 8_388_608
    asr_max_request_bytes: int = 8_454_144
    asr_allowed_media_types: str = "audio/webm,audio/mp4,audio/ogg"
    asr_max_duration_seconds: int = 60
    asr_timeout_seconds: float = 60.0
    asr_poll_timeout_seconds: float = 30.0
    formal_desktop_only: bool = True
    formal_min_viewport_width: int = 1024
    formal_allow_text_input: bool = False
    test_channel_enabled: bool = True
    recruitment_test_override_open: bool = False

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"prod", "production"}

    def has_required_production_provider_settings(self) -> bool:
        if not self.is_production:
            return True
        required_values = (
            self.yizhan_api_key,
            self.aabao_api_key,
            self.packyapi_api_key,
            self.deepseek_api_key,
            self.tencent_secret_id,
            self.tencent_secret_key,
        )
        return all(value is not None and value.strip() for value in required_values)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
