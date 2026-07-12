"""Application settings.

All fields are defaulted so ``Settings()`` builds with an empty environment
and no ``.env`` file — the credential-free path still works. When a repo-root
``.env`` is present its values override the defaults (real Supabase / Claude).
Precedence: real environment variables > ``.env`` file > defaults.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root is api/../ ; the .env lives there (gitignored). Resolve absolutely
# so it loads regardless of the current working directory (host or container).
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STUDYPAL_",
        env_file=_ENV_FILE,
        extra="ignore",
    )

    app_name: str = "StudyPal API"
    app_version: str = "0.1.0"
    environment: str = "dev"
    cors_origins: list[str] = ["http://localhost:5173"]

    # Database — defaults to the compose-default local Postgres (no real secrets).
    # PR-2 will swap this for the real Supabase DSN via env.
    db_dsn: str = "postgresql://studypal:studypal@localhost:5432/studypal"

    # Auth stub — in PR-1 a request header injects the user_id; PR-2 replaces
    # this with real JWKS JWT verification.  deny_when_no_identity = True means
    # the stub returns 401 when no X-User-Id header is present (deny-by-default).
    stub_auth_header: str = "x-user-id"
    deny_when_no_identity: bool = True

    # Generation caps — guard against cost-DoS (invariant 7).
    max_scope_chars: int = 8_000
    max_questions: int = 50

    # Claude (PR-2). Empty until live generation is wired; never committed.
    anthropic_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
