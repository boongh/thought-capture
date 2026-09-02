"""Builds the ``organize``-step ``LLMProvider`` from configuration.

An empty ``model_organize`` selects the deterministic offline adapter
(``Settings.uses_offline_model_adapter``) - today that is every deployment,
since safe mode's ``REVIEWED_MODELS`` is still empty
(``tc_infrastructure.llm.reviewed_models``), so no organize call can reach a
real provider until the owner reviews and adds one.
"""

from __future__ import annotations

from tc_domain.llm import LLMProvider
from tc_infrastructure.config import Settings
from tc_infrastructure.llm.offline import OfflineLLMProvider
from tc_infrastructure.llm.openrouter import OpenRouterProvider


def build_organize_provider(settings: Settings) -> LLMProvider:
    if settings.uses_offline_model_adapter:
        return OfflineLLMProvider()

    model_id = settings.model_organize
    return OpenRouterProvider(
        api_key=settings.openrouter_api_key.get_secret_value(),
        base_url=settings.openrouter_base_url,
        model_id=model_id,
        supports_strict_schema=settings.model_supports_strict_schema,
        allow_fallbacks=settings.openrouter_allow_fallbacks,
        deny_data_collection=settings.openrouter_deny_data_collection,
        only_providers=settings.openrouter_only_providers(model_id),
        require_zdr=settings.openrouter_require_zdr,
    )
