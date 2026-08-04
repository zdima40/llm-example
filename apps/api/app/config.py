from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "Octa Oracle"
    public_base_url: str = "http://localhost:8088"
    mini_app_url: str = "http://localhost:8088"
    cors_origins: str = "http://localhost:8088,http://localhost:5173"

    dev_auth: bool = False
    dev_telegram_user_id: int = 1
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_init_data_max_age_seconds: int = 3600
    telegram_init_data_future_skew_seconds: int = 30

    database_url: str = "sqlite+aiosqlite:///./octa_oracle.db"
    db_auto_create: bool = False

    llm_mode: Literal["mock", "vllm"] = "mock"
    llm_system_prompt: str = (
        "Ты полезный ассистент. Отвечай на языке пользователя, точно и понятно."
    )
    llm_temperature: float = 0.6
    llm_max_output_tokens: int = 4096
    llm_context_char_budget: int = 8_000
    show_reasoning: bool = False

    vllm_base_url: str = "http://vllm:8000/v1"
    vllm_api_key: str = "local-vllm-key"
    vllm_model: str = "deepseek-r1-8b"
    vllm_connect_timeout_seconds: float = 10.0
    vllm_read_timeout_seconds: float = 600.0

    max_input_chars: int = 16_000
    max_concurrent_generations_per_user: int = 1
    rate_limit_requests_per_minute: int = 20
    sse_heartbeat_seconds: float = 10.0
    mock_chunk_delay_seconds: float = 0.025

    telegram_auto_setup: bool = False

    @field_validator("dev_auth")
    @classmethod
    def disable_dev_auth_in_production(cls, value: bool, info):
        app_env = info.data.get("app_env")
        if app_env == "production" and value:
            raise ValueError("DEV_AUTH cannot be enabled in production")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
