"""Embedding sidecar configuration fields (docs/adr/0010 §4-5).

Mirrors ``tests/unit/test_settings.py``'s ``build`` helper: ``_env_file=None``
keeps a developer's real ``.env`` out of the test.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tc_infrastructure.config import Settings

REQUIRED_ENV = {
    "TC_WORKSPACE_TIMEZONE": "Asia/Bangkok",
}


def build(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for key, value in {**REQUIRED_ENV, **env}.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def test_embedding_settings_default_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = build(monkeypatch)

    assert settings.embedding_sidecar_base_url == "http://embedding-sidecar:8081"
    assert settings.embedding_sidecar_timeout_seconds == 30.0
    assert settings.embedding_sync_batch_size == 20


def test_embedding_settings_are_overridable_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = build(
        monkeypatch,
        TC_EMBEDDING_SIDECAR_BASE_URL="http://127.0.0.1:8081",
        TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS="5.5",
        TC_EMBEDDING_SYNC_BATCH_SIZE="7",
    )

    assert settings.embedding_sidecar_base_url == "http://127.0.0.1:8081"
    assert settings.embedding_sidecar_timeout_seconds == 5.5
    assert settings.embedding_sync_batch_size == 7


def test_embedding_sidecar_timeout_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        build(monkeypatch, TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS="0")


def test_embedding_sync_batch_size_must_be_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        build(monkeypatch, TC_EMBEDDING_SYNC_BATCH_SIZE="0")
