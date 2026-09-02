"""Builds the per-step ``LLMProvider``s from configuration.

``organize`` and ``select`` (docs/DESIGN.md 7.3.2, 7.4) are independently
pinned models - a reviewed model for one says nothing about the other, and
today they typically differ (``upstage/solar-pro4`` is reviewed for select;
organize has no reviewed candidate yet). An empty slug for either selects the
deterministic offline adapter (``Settings.uses_offline_model_adapter`` /
``uses_offline_select_adapter``) rather than silently calling a provider.
"""

from __future__ import annotations

from tc_domain.llm import LLMProvider
from tc_infrastructure.config import Settings
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tc_infrastructure.llm.openrouter import OpenRouterProvider


def _build_provider(
    settings: Settings, *, model_id: str, supports_strict_schema: bool
) -> LLMProvider:
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=settings.openrouter_base_url,
        model_id=model_id,
        supports_strict_schema=supports_strict_schema,
        allow_fallbacks=settings.openrouter_allow_fallbacks,
        deny_data_collection=settings.openrouter_deny_data_collection,
        only_providers=settings.openrouter_only_providers(model_id),
        require_zdr=settings.openrouter_require_zdr,
    )


def build_organize_provider(settings: Settings) -> LLMProvider:
    if settings.uses_offline_model_adapter:
        return OfflineLLMProvider()
    return _build_provider(
        settings,
        model_id=settings.model_organize,
        supports_strict_schema=settings.model_supports_strict_schema,
    )


def build_select_provider(settings: Settings) -> LLMProvider:
    if settings.uses_offline_select_adapter:
        return OfflineLLMProvider()
    return _build_provider(
        settings,
        model_id=settings.model_select,
        supports_strict_schema=settings.model_select_supports_strict_schema,
    )
