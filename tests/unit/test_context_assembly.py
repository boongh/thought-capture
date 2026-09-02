"""Assembling context: deterministic signals plus the non-fatal `select` call."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from tc_application.context_assembly import assemble_context
from tc_domain.context import ContextAssemblyConfig, Tier1Row
from tc_domain.llm import LLMRequest, LLMResponse
from tc_infrastructure.llm.offline import OfflineLLMProvider

NOW = dt.datetime(2026, 8, 31, 20, 0, tzinfo=dt.UTC)


async def no_journal(request: LLMRequest, response: LLMResponse, attempt: int) -> None:
    """Journaling is required at the call site; these tests just don't check it."""


def make_index() -> tuple[Tier1Row, ...]:
    return (
        Tier1Row(
            stable_key="project:aurora",
            entity_type="project",
            canonical_name="Aurora",
            aliases=(),
            summary="",
            last_mentioned_at=None,
            open_thread_count=0,
        ),
        Tier1Row(
            stable_key="person:jane-doe",
            entity_type="person",
            canonical_name="Jane Doe",
            aliases=("Jane",),
            summary="",
            last_mentioned_at=None,
            open_thread_count=0,
        ),
    )


class FailingProvider:
    """Always raises, to exercise the non-fatal selector-failure path."""

    model_id = "failing/model"
    supports_strict_schema = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        from tc_domain.llm import LLMError

        raise LLMError("synthetic provider outage")


async def test_alias_signal_alone_selects_a_named_entity() -> None:
    provider = OfflineLLMProvider(responder=lambda _: json.dumps({"stable_keys": []}))
    result = await assemble_context(
        index=make_index(),
        window_text="shipping the Aurora launch today",
        provider=provider,
        journal=no_journal,
        now=NOW,
    )
    assert "project:aurora" in result.full_stable_keys
    assert result.degraded is False


async def test_the_selector_can_add_a_pronoun_only_reference() -> None:
    provider = OfflineLLMProvider(
        responder=lambda _: json.dumps({"stable_keys": ["person:jane-doe"]})
    )
    result = await assemble_context(
        index=make_index(),
        window_text="grabbed lunch with her again",
        provider=provider,
        journal=no_journal,
        now=NOW,
    )
    assert "person:jane-doe" in result.full_stable_keys
    assert result.degraded is False


async def test_a_hallucinated_key_is_dropped_not_trusted() -> None:
    provider = OfflineLLMProvider(
        responder=lambda _: json.dumps({"stable_keys": ["project:does-not-exist"]})
    )
    result = await assemble_context(
        index=make_index(),
        window_text="nothing relevant here",
        provider=provider,
        journal=no_journal,
        now=NOW,
    )
    assert result.full_stable_keys == frozenset()
    assert result.degraded is False


async def test_a_provider_failure_degrades_rather_than_raises() -> None:
    result = await assemble_context(
        index=make_index(),
        window_text="shipping the Aurora launch today",
        provider=FailingProvider(),
        journal=no_journal,
        now=NOW,
    )
    # The deterministic alias signal still ran.
    assert "project:aurora" in result.full_stable_keys
    assert result.degraded is True


async def test_an_empty_index_skips_the_selector_call_entirely() -> None:
    calls: list[LLMRequest] = []

    class RecordingProvider(OfflineLLMProvider):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            calls.append(request)
            return await super().complete(request)

    provider = RecordingProvider(responder=lambda _: json.dumps({"stable_keys": []}))
    result = await assemble_context(
        index=(), window_text="anything", provider=provider, journal=no_journal, now=NOW
    )
    assert calls == []
    assert result.selections == ()
    assert result.degraded is False


async def test_config_caps_the_number_selected_full(monkeypatch: pytest.MonkeyPatch) -> None:
    index = tuple(
        Tier1Row(
            stable_key=f"topic:t{i}",
            entity_type="topic",
            canonical_name=f"Topic {i}",
            aliases=(),
            summary="",
            last_mentioned_at=NOW,
            open_thread_count=0,
        )
        for i in range(5)
    )
    provider = OfflineLLMProvider(responder=lambda _: json.dumps({"stable_keys": []}))
    result = await assemble_context(
        index=index,
        window_text="",
        provider=provider,
        journal=no_journal,
        now=NOW,
        config=ContextAssemblyConfig(max_selected_documents=2, recency_days=3.0),
    )
    assert len(result.full_stable_keys) == 2
