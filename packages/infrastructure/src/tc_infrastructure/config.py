"""Process configuration, loaded from the environment.

Every secret is a ``SecretStr`` so that an accidental ``repr``, log line, or
traceback prints ``**********`` rather than the value. See docs/DESIGN.md 12.2:
secrets must not appear in Git, logs, prompts, exports, or diagnostics.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tc_infrastructure.llm.reviewed_models import REVIEWED_MODELS


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
    # Safe mode (docs/adr/0006) restricts model_organize/model_query_plan to
    # tc_infrastructure.llm.reviewed_models.REVIEWED_MODELS. Custom mode lifts
    # that restriction for a host who accepts responsibility for whatever
    # model they pin. Defaults to safe: an operator who wants the wider
    # selection has to say so.
    model_selection_mode: Literal["safe", "custom"] = "safe"
    # Deliberately empty until a slug is pinned. An empty value selects the
    # deterministic offline adapter rather than silently calling a provider.
    model_organize: str = ""
    model_query_plan: str = ""
    model_supports_strict_schema: bool = False
    # Only consulted in custom mode - safe mode never allows provider
    # fallback, regardless of this value. See openrouter_allow_fallbacks.
    model_allow_fallback: bool = True
    monthly_budget_usd: float = Field(default=5.0, ge=0)

    # -- Attachments -------------------------------------------------------
    attachment_root: Path = Path("./attachments")
    attachment_max_bytes: int = Field(default=26_214_400, ge=1)

    # -- API ---------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8080, ge=1, le=65535)
    api_bearer_token: SecretStr = SecretStr("")

    @field_validator(
        "discord_owner_user_id",
        "discord_guild_id",
        "discord_channel_id",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """Treat an empty environment variable as absent.

        ``env.example`` ships the optional Discord IDs blank, and a blank line
        in a ``.env`` file arrives as the empty string rather than as a missing
        key. Without this, copying the template and filling in only what you
        need fails validation before the process reaches PostgreSQL.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

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

    @model_validator(mode="after")
    def _safe_mode_restricts_to_reviewed_models(self) -> Settings:
        """Safe mode is a startup guarantee, not a hint (docs/adr/0006).

        A pinned slug that has not been reviewed for retention/training policy
        and JSON-schema enforcement must not reach a live run silently - so
        this fails at process start, the same way an unknown timezone does,
        rather than at the first organize call. An empty slug is exempt: it
        selects the deterministic offline adapter, not a provider call.
        """
        if self.model_selection_mode != "safe":
            return self
        for field_name in ("model_organize", "model_query_plan"):
            slug = getattr(self, field_name)
            if slug and slug not in REVIEWED_MODELS:
                raise ValueError(
                    f"{field_name}={slug!r} is not in the reviewed-models allowlist "
                    "required by safe mode (docs/adr/0006). Either have it reviewed "
                    "and added to tc_infrastructure.llm.reviewed_models.REVIEWED_MODELS, "
                    "or set TC_MODEL_SELECTION_MODE=custom to select models freely."
                )
        return self

    @property
    def openrouter_allow_fallbacks(self) -> bool:
        """Whether OpenRouter may route to a different provider than the vetted one.

        Safe mode never allows it, unconditionally - a model that reaches
        REVIEWED_MODELS was checked for *that model's* retention/schema
        behavior, not whatever OpenRouter might substitute it with. Custom
        mode is the host's call, via ``model_allow_fallback``.
        """
        if self.model_selection_mode == "safe":
            return False
        return self.model_allow_fallback

    @property
    def openrouter_deny_data_collection(self) -> bool:
        """Whether to tell OpenRouter to route only through non-retaining providers.

        Safe mode always denies it: REVIEWED_MODELS entries were checked to
        not require a training/retention opt-in, and this is the request-side
        enforcement of that, not just trust in the registry. Custom mode
        leaves OpenRouter's own default in place, because forcing "deny" here
        would silently break the one documented custom-mode use case - a
        free, training-opted-in tier the host has deliberately chosen.
        """
        return self.model_selection_mode == "safe"

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
