"""Builds the per-step ``LLMProvider``s from configuration.

``organize`` and ``select`` (docs/DESIGN.md 7.3.2, 7.4) are independently
pinned models - a reviewed model for one says nothing about the other, and
today they typically differ (``upstage/solar-pro4`` is reviewed for select;
organize has no reviewed candidate yet). An empty slug for either selects the
deterministic offline adapter (``Settings.uses_offline_model_adapter`` /
``uses_offline_select_adapter``) rather than silently calling a provider.
"""

from __future__ import annotations

from openai import AsyncOpenAI

from tc_domain.llm import LLMProvider
from tc_infrastructure.config import Settings
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tc_infrastructure.llm.openrouter import OpenRouterProvider


def _build_provider(
    settings: Settings,
    *,
    model_id: str,
    supports_strict_schema: bool,
    client: AsyncOpenAI | None = None,
) -> LLMProvider:
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=settings.openrouter_base_url,
        model_id=model_id,
        supports_strict_schema=supports_strict_schema,
        client=client,
        allow_fallbacks=settings.openrouter_allow_fallbacks,
        deny_data_collection=settings.openrouter_deny_data_collection,
        only_providers=settings.openrouter_only_providers(model_id),
        require_zdr=settings.openrouter_require_zdr,
    )


def build_organize_provider(
    settings: Settings, *, client: AsyncOpenAI | None = None
) -> LLMProvider:
    if settings.uses_offline_model_adapter:
        return OfflineLLMProvider()
    return _build_provider(
        settings,
        model_id=settings.model_organize,
        # docs/adr/0006: safe mode derives strict-schema enforcement from the
        # reviewed model's own recorded capability, not from the mutable
        # `model_supports_strict_schema` flag - that flag only applies in
        # custom mode, where an operator override is legitimate.
        supports_strict_schema=settings.strict_schema_supported(
            settings.model_organize, custom_flag=settings.model_supports_strict_schema
        ),
        client=client,
    )


def build_select_provider(settings: Settings, *, client: AsyncOpenAI | None = None) -> LLMProvider:
    if settings.uses_offline_select_adapter:
        return OfflineLLMProvider()
    return _build_provider(
        settings,
        model_id=settings.model_select,
        # Same safe/custom split as build_organize_provider above - see
        # Settings.strict_schema_supported. This is the fix for the PR #16
        # review finding: TC_MODEL_SELECT_SUPPORTS_STRICT_SCHEMA defaulting to
        # False must not silently drop strict response_format/
        # require_parameters for a reviewed select model in safe mode.
        supports_strict_schema=settings.strict_schema_supported(
            settings.model_select, custom_flag=settings.model_select_supports_strict_schema
        ),
        client=client,
    )
