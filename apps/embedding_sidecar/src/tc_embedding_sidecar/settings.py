"""Sidecar configuration.

Deliberately not `tc_infrastructure.config.Settings`: this process does not
share a Python version with the main workspace (docs/adr/0010 §5), so it
cannot import first-party code from it either. A separate, minimal settings
object is the isolation boundary, not an oversight.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_EMBEDDING_")

    # docs/adr/0010 §5's recommended model: the same one Khoj already used,
    # with direct operational evidence (CPU-only, ~30s cold start, 384 dims).
    model_id: str = "thenlper/gte-small"
    host: str = "0.0.0.0"
    port: int = 8081


@lru_cache
def get_settings() -> Settings:
    return Settings()
