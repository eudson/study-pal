"""Application settings.

All fields are defaulted so ``Settings()`` builds with an empty environment
and no ``.env`` file — this is a credential-free bootstrap milestone.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="STUDYPAL_")

    app_name: str = "StudyPal API"
    app_version: str = "0.1.0"
    environment: str = "dev"
    cors_origins: list[str] = ["http://localhost:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
