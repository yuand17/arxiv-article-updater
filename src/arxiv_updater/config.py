from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .external_services import ExternalServiceState, load_external_service


class Settings(BaseSettings):
    """Configuration for the private, loopback-only desktop application."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/arxiv_updater.db"
    timezone: str = "Asia/Shanghai"

    serpapi_api_key: str = ""
    deepseek_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_thinking_enabled: bool = False
    llm_monthly_token_budget: int = 50_000_000
    source_cache_dir: str = "data/cache"
    scirate_browser_profile_dir: str = "data/chrome/scirate"
    scirate_browser_timeout_seconds: int = Field(default=300, ge=30, le=900)

    arxiv_categories: list[str] = Field(
        default_factory=lambda: [
            "quant-ph",
            "cond-mat.dis-nn",
            "cond-mat.mes-hall",
            "cond-mat.mtrl-sci",
            "cond-mat.other",
            "cond-mat.quant-gas",
            "cond-mat.soft",
            "cond-mat.stat-mech",
            "cond-mat.str-el",
            "cond-mat.supr-con",
        ]
    )

    def __init__(self, **values: Any) -> None:
        # API keys are accepted only as explicit runtime/test values.  Supplying
        # empty init values keeps OS environment variables and .env out of the
        # credential path; production keys are injected from Credential Manager.
        values.setdefault("serpapi_api_key", "")
        values.setdefault("deepseek_api_key", "")
        super().__init__(**values)

    @model_validator(mode="after")
    def require_local_sqlite(self) -> "Settings":
        if not self.database_url.startswith("sqlite"):
            raise ValueError("arXiv Updater 仅支持本地 SQLite 数据库")
        return self

    def ensure_local_directories(self) -> None:
        Path("data").mkdir(parents=True, exist_ok=True)
        Path(self.source_cache_dir).mkdir(parents=True, exist_ok=True)
        Path(self.scirate_browser_profile_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    services = get_external_service_states()
    settings.serpapi_api_key = services["serpapi"].effective_api_key
    settings.deepseek_api_key = services["deepseek"].effective_api_key
    settings.ensure_local_directories()
    return settings


def get_external_service_states() -> dict[str, ExternalServiceState]:
    """Resolve secure UI/runtime service state without exposing keys to templates."""

    return {
        "serpapi": load_external_service("serpapi"),
        "deepseek": load_external_service("deepseek"),
    }
