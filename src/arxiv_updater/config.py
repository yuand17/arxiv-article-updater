from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the private, loopback-only desktop application."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/arxiv_updater.db"
    timezone: str = "Asia/Shanghai"

    serpapi_api_key: str = ""
    serpapi_monthly_query_budget: int = 240
    semantic_scholar_api_key: str = ""
    deepseek_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_thinking_enabled: bool = False
    llm_monthly_token_budget: int = 5_000_000
    source_cache_dir: str = "data/cache"

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

    @model_validator(mode="after")
    def require_local_sqlite(self) -> "Settings":
        if not self.database_url.startswith("sqlite"):
            raise ValueError("arXiv Updater 仅支持本地 SQLite 数据库")
        return self

    def ensure_local_directories(self) -> None:
        Path("data").mkdir(parents=True, exist_ok=True)
        Path(self.source_cache_dir).mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_local_directories()
    return settings
