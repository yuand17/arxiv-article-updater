import ipaddress
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_secret_key: str = "development-only-change-me"
    database_url: str = "sqlite:///./data/arxiv_updater.db"
    base_url: str = "http://127.0.0.1:8000"
    timezone: str = "Asia/Shanghai"
    local_dev_auto_login: bool = True

    serpapi_api_key: str = ""
    serpapi_monthly_query_budget: int = 240
    deepseek_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-flash"
    llm_monthly_token_budget: int = 5_000_000
    summary_user_weekly_limit: int = 50
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

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"

    def ensure_local_directories(self) -> None:
        if self.database_url.startswith("sqlite"):
            Path("data").mkdir(parents=True, exist_ok=True)
        Path(self.source_cache_dir).mkdir(parents=True, exist_ok=True)

    def allows_dev_auto_login_for(self, hostname: str | None) -> bool:
        if not (self.is_development and self.local_dev_auto_login and hostname):
            return False
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_local_directories()
    return settings
