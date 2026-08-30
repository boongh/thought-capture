"""Process configuration, loaded from the environment.

Every secret is a ``SecretStr`` so that an accidental ``repr``, log line, or
traceback prints ``**********`` rather than the value. See docs/DESIGN.md 12.2:
secrets must not appear in Git, logs, prompts, exports, or diagnostics.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration.

    Values come from the process environment, falling back to a local ``.env``.
    ``.env`` is gitignored; see ``env.example`` for the template.
    """

    model_config = SettingsConfigDict(
        env_prefix="TC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Workspace ---------------------------------------------------------
    workspace_name: str = "personal"
    workspace_timezone: str = "Asia/Bangkok"
    digest_local_time: dt.time = dt.time(20, 0)

    # -- Database ----------------------------------------------------------
    # The application role is denied UPDATE/DELETE on `thoughts`. The migration
    # role owns the schema and is used only by the one-shot migrate step.
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://tc_app:tc_app@127.0.0.1:5432/thought_capture"
    )
    migration_database_url: SecretStr = SecretStr(
        "postgresql+psycopg://tc_migrator:tc_migrator@127.0.0.1:5432/thought_capture"
    )
    database_pool_size: int = Field(default=5, ge=1, le=50)
    app_db_password: SecretStr = SecretStr("tc_app")

    # -- Discord -----------------------------------------------------------
    discord_bot_token: SecretStr = SecretStr("")
    discord_owner_user_id: int | None = None
    discord_guild_id: int | None = None
    discord_channel_id: int | None = None

    # -- OpenRouter --------------------------------------------------------
    openrouter_api_key: SecretStr = SecretStr("")
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Deliberately empty until a slug is pinned. An empty value selects the
    # deterministic offline adapter rather than silently calling a provider.
    model_organize: str = ""
    model_query_plan: str = ""
    model_supports_strict_schema: bool = False
    monthly_budget_usd: float = Field(default=5.0, ge=0)

    # -- Attachments -------------------------------------------------------
    attachment_root: Path = Path("./attachments")
    attachment_max_bytes: int = Field(default=26_214_400, ge=1)

    # -- API ---------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_bearer_token: SecretStr = SecretStr("")

    @field_validator("workspace_timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        """Reject an unknown IANA name at startup rather than at 20:00."""
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"{value!r} is not a known IANA timezone (for example 'Asia/Bangkok')"
            ) from exc
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.workspace_timezone)

    @property
    def uses_offline_model_adapter(self) -> bool:
        """True when no provider slug is pinned, so organization runs offline."""
        return not self.model_organize


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, resolved once."""
    return Settings()
