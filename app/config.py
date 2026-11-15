"""Configuration, read from the environment (and a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./relais.db"

    # auto: real when ANTHROPIC_API_KEY is set, otherwise the labelled demo script.
    # real: refuses to start without a key rather than silently faking answers.
    ai_mode: str = "auto"
    anthropic_api_key: str | None = None
    ai_agent_model: str = "claude-opus-5-5"
    ai_classify_model: str = "claude-opus-5-5"
    ai_effort: str = "medium"
    ai_refusal_fallback: bool = True
    agent_max_steps: int = 8

    erp_base_url: str = "http://127.0.0.1:8200/mock-erp"
    erp_token: str = "dev-erp-token"

    inbound_api_key: str | None = None
    dashboard_user: str = "admin"
    dashboard_password: str | None = None

    worker_inline: bool = True
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 3

    seed_demo_data: bool = True
    seed_demo_tickets: bool = True

    @property
    def resolved_ai_mode(self) -> str:
        if self.ai_mode == "auto":
            return "real" if self.anthropic_api_key else "demo"
        return self.ai_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()
