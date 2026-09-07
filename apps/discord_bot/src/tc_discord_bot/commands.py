"""Owner-only slash commands: ``/organize``, ``/status``, ``/search``, ``/ask``.

Discord slash commands map to use cases directly, not HTTP loopback calls
(docs/DESIGN.md 5.1, 10) - ``organize`` here is the exact ``OrganizeWindow``
callable the worker's own scheduler drives, and ``search``/``ask`` are the
exact ``Search``/``AskQuestion`` use cases ``apps/api`` drives, not second
implementations.

Message capture is scoped by guild/channel (docs/DESIGN.md 4.1's
``CaptureAllowlist``, ``adapter.py``/``client.py``). These commands are a
different concern - operations that must work in a DM regardless of which
guild/channel is configured for capture, and that read or trigger processing
over the owner's personal memory content - so each command here checks the
caller's Discord user id directly against the configured owner, rather than
reusing the channel-scoped capture allowlist, and is explicitly registered as
usable from a DM (``allowed_contexts`` below) as well as a guild.

Registered as *global* commands (``build_admin_commands`` below), because a
DM-only deployment (docs/DESIGN.md 4.1: "leave both blank for DM-only
capture") has no guild to scope a faster guild-only registration to; see
``CaptureClient._sync_commands`` (client.py) for the guild-copy-for-instant-
availability-in-dev-plus-global-for-DMs sync strategy.

Scope note (docs/adr/0010, 2026-09-07): ``/search`` defaults to ``mode=exact``
(always available); ``semantic``/``hybrid`` return real results only after an
operator has run ``POST /v1/admin/khoj-sync`` at least once (no Discord
command for that yet - a deliberate scope cut, see docs/adr/0010). ``/ask``
answers only when the deployment has opted into ``TC_ASK_ENABLED`` *and* Khoj
itself has a chat model configured (docs/adr/0003 finding 4); otherwise it
says so explicitly rather than pretending no memory exists. ``/undo`` still
has no restore/revert use case built anywhere in the codebase - not part of
this slice either.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from tc_application.ask import AskQuestion
from tc_application.organize import OrganizeWindow
from tc_application.search import Search
from tc_domain.ask import AskAnswer
from tc_domain.capture import WorkspaceId
from tc_domain.search import SearchPage, SearchQuery
from tc_domain.windows import CaptureWindow, most_recent_cutoff, previous_cutoff_before
from tc_infrastructure.db.run_reader import PostgresRunReader, RunRecord
from tc_infrastructure.db.windows import PostgresCaptureWindows

logger = logging.getLogger(__name__)

# Discord hard-caps a single message (including an interaction followup) at
# 2000 characters. These commands send plain text, not the digest's
# section-boundary splitting (docs/DESIGN.md 4.2) - a deliberate scope cut
# for diagnostic/query replies, not an oversight (docs/adr/0010).
_MESSAGE_LIMIT = 2000
_SEARCH_RESULT_LIMIT = 5
_SEARCH_MODES = ("exact", "semantic", "hybrid")

# A DM-usable command must allow the DM context explicitly (discord.py 2.7's
# default leaves this to the application's Developer Portal installation
# settings, which this project does not control from code) and must not
# allow an arbitrary private-channel/group-DM context, which would let
# someone other than the owner's own DM with the bot reach it in principle.
_OWNER_ONLY_CONTEXTS = app_commands.AppCommandContext(
    guild=True, dm_channel=True, private_channel=False
)

FirstCaptureLookup = Callable[[WorkspaceId], Awaitable[dt.datetime | None]]

_NOT_AUTHORIZED = "Not authorized."


def _is_owner(interaction: discord.Interaction, owner_user_id: int) -> bool:
    return interaction.user.id == owner_user_id


async def _reject_if_not_owner(interaction: discord.Interaction, owner_user_id: int) -> bool:
    """Replies and returns True if the caller must be turned away."""
    if _is_owner(interaction, owner_user_id):
        return False
    # No detail about *why*: an administrative command existing at all is not
    # information a non-owner caller needs confirmed.
    command_name = interaction.command.name if interaction.command else "?"
    logger.info("admin_command.rejected", extra={"command": command_name})
    await interaction.response.send_message(_NOT_AUTHORIZED, ephemeral=True)
    return True


def build_admin_commands(
    *,
    organize: OrganizeWindow,
    windows: PostgresCaptureWindows,
    runs: PostgresRunReader,
    first_capture_at: FirstCaptureLookup,
    workspace_id: WorkspaceId,
    owner_user_id: int,
    digest_local_time: dt.time,
    timezone: str,
    search: Search,
    ask: AskQuestion,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> tuple[app_commands.Command[Any, ..., Any], ...]:
    """Builds the ``/organize``, ``/status``, ``/search``, and ``/ask`` commands,
    ready to register on a tree."""

    async def organize_callback(
        interaction: discord.Interaction,
        window_from: str | None = None,
        window_to: str | None = None,
    ) -> None:
        if await _reject_if_not_owner(interaction, owner_user_id):
            return

        try:
            window = await _resolve_window(
                window_from,
                window_to,
                first_capture_at=first_capture_at,
                workspace_id=workspace_id,
                digest_local_time=digest_local_time,
                timezone=timezone,
                clock=clock,
            )
        except _WindowInputError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if window is None:
            await interaction.response.send_message(
                "Nothing captured yet; there is no window to organize.", ephemeral=True
            )
            return

        # Organizing calls an LLM provider, which can take longer than
        # Discord's 3-second interaction-acknowledgement budget.
        await interaction.response.defer(ephemeral=True)

        # `/organize` is always an owner-triggered, out-of-band run - never the
        # scheduler's own - so it is persisted as `force_organize`, distinct
        # from the scheduler's `organize` rows (docs/DESIGN.md 4.2, 6.3).
        # `PostgresCaptureWindows.succeeded_run_for` only matches `kind ==
        # "organize"`, so this can never be mistaken for the scheduled run
        # that later supersedes it.
        # Captured via `on_run_started`, not the return value: `organize()`
        # re-raises on failure without returning anything, but it has already
        # created and (in its except block) failed the run by then - this is
        # the only way to learn that run's id in the failure case too.
        run_id: uuid.UUID | None = None

        def _record_run_id(started_run_id: uuid.UUID) -> None:
            nonlocal run_id
            run_id = started_run_id

        async with windows.locked(workspace_id, window) as acquired:
            if not acquired:
                await interaction.followup.send(
                    "This window is already being organized (by the scheduler or another "
                    "`/organize` call); try again shortly.",
                    ephemeral=True,
                )
                return

            try:
                await organize(
                    workspace_id, window, kind="force_organize", on_run_started=_record_run_id
                )
            except Exception as exc:
                # OrganizeWindow already wrote the sanitized failure to the run
                # row before re-raising (organize.py); nothing from `exc`
                # itself is safe to surface here (docs/DESIGN.md 14.2).
                logger.warning("organize_command.failed", extra={"error_class": type(exc).__name__})

        if run_id is None:
            # `run_ledger.start` itself never even completed, so no run
            # exists to look up - never fall back to `most_recent()` here, or
            # a concurrent run on another window could be shown instead.
            await interaction.followup.send(
                "Organize failed before a run could be recorded; check the bot's logs.",
                ephemeral=True,
            )
            return

        record = await runs.get(workspace_id, run_id)
        await interaction.followup.send(_format_organize_result(window, record), ephemeral=True)

    async def status_callback(interaction: discord.Interaction, run_id: str | None = None) -> None:
        if await _reject_if_not_owner(interaction, owner_user_id):
            return

        await interaction.response.defer(ephemeral=True)

        record: RunRecord | None
        if run_id is None:
            record = await runs.most_recent(workspace_id)
        else:
            try:
                parsed = uuid.UUID(run_id)
            except ValueError:
                await interaction.followup.send(
                    f"`{run_id}` is not a valid run id (expected a UUID).", ephemeral=True
                )
                return
            record = await runs.get(workspace_id, parsed)

        if record is None:
            await interaction.followup.send("No matching run found.", ephemeral=True)
            return
        await interaction.followup.send(_format_run(record), ephemeral=True)

    async def search_callback(
        interaction: discord.Interaction, query: str, mode: str = "exact"
    ) -> None:
        if await _reject_if_not_owner(interaction, owner_user_id):
            return
        if mode not in _SEARCH_MODES:
            await interaction.response.send_message(
                f"`mode` must be one of {', '.join(_SEARCH_MODES)}.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        page = await search(
            workspace_id,
            SearchQuery(q=query, limit=_SEARCH_RESULT_LIMIT),
            mode=mode,
        )
        await interaction.followup.send(_format_search_page(page, mode), ephemeral=True)

    async def ask_callback(interaction: discord.Interaction, question: str) -> None:
        if await _reject_if_not_owner(interaction, owner_user_id):
            return

        # Ask proxies to Khoj's chat model (docs/adr/0010), which can take
        # longer than the 3-second interaction-acknowledgement budget - same
        # reasoning as `/organize`'s defer.
        await interaction.response.defer(ephemeral=True)
        answer = await ask(workspace_id, question)
        await interaction.followup.send(_format_ask_answer(answer), ephemeral=True)

    organize_command: app_commands.Command[Any, ..., Any] = app_commands.Command(
        name="organize",
        description="Force-organize a capture window now (defaults to the current open window).",
        callback=organize_callback,
        allowed_contexts=_OWNER_ONLY_CONTEXTS,
    )
    app_commands.rename(window_from="from", window_to="to")(organize_command)
    app_commands.describe(
        window_from="ISO 8601 window start, e.g. 2026-09-01T20:00:00+07:00 (default: last organized cutoff)",
        window_to="ISO 8601 window end (default: now)",
    )(organize_command)

    status_command: app_commands.Command[Any, ..., Any] = app_commands.Command(
        name="status",
        description="Show an organize run's status (defaults to the most recent run).",
        callback=status_callback,
        allowed_contexts=_OWNER_ONLY_CONTEXTS,
    )
    app_commands.describe(run_id="A run id from a previous /organize or /status reply")(
        status_command
    )

    search_command: app_commands.Command[Any, ..., Any] = app_commands.Command(
        name="search",
        description="Search captured memory (exact by default; semantic/hybrid need a Khoj sync).",
        callback=search_callback,
        allowed_contexts=_OWNER_ONLY_CONTEXTS,
    )
    app_commands.describe(
        query="Free text to search for",
        mode="exact (default, always available), semantic, or hybrid",
    )(search_command)

    ask_command: app_commands.Command[Any, ..., Any] = app_commands.Command(
        name="ask",
        description="Ask a question over generated memory (requires Ask to be configured).",
        callback=ask_callback,
        allowed_contexts=_OWNER_ONLY_CONTEXTS,
    )
    app_commands.describe(question="The question to ask")(ask_command)

    return (organize_command, status_command, search_command, ask_command)


class _WindowInputError(ValueError):
    """A user-supplied ``from``/``to`` value could not be used."""


async def _resolve_window(
    window_from: str | None,
    window_to: str | None,
    *,
    first_capture_at: FirstCaptureLookup,
    workspace_id: WorkspaceId,
    digest_local_time: dt.time,
    timezone: str,
    clock: Callable[[], dt.datetime],
) -> CaptureWindow | None:
    """Builds the requested window, or the current open window if both are omitted.

    Deliberately does **not** consult ``PostgresCaptureWindows.organized_frontier``
    for an explicit ``from``/``to`` pair - an explicit window is exactly the
    escape hatch for re-running or backfilling a specific range, so it must
    not be silently clamped to what the frontier already considers organized.
    """
    if (window_from is None) != (window_to is None):
        raise _WindowInputError("Provide both `from` and `to`, or neither.")

    if window_from is not None and window_to is not None:
        try:
            start = dt.datetime.fromisoformat(window_from)
            end = dt.datetime.fromisoformat(window_to)
        except ValueError as exc:
            raise _WindowInputError(
                "`from`/`to` must be ISO 8601 timestamps with a UTC offset, "
                "e.g. 2026-09-01T20:00:00+07:00."
            ) from exc
        if start.tzinfo is None or end.tzinfo is None:
            raise _WindowInputError("`from`/`to` must include a UTC offset (e.g. +07:00 or Z).")
        try:
            return CaptureWindow(start=start, end=end)
        except ValueError as exc:
            raise _WindowInputError(str(exc)) from exc

    captured_at = await first_capture_at(workspace_id)
    if captured_at is None:
        return None
    now = clock()
    floor = previous_cutoff_before(captured_at, digest_local_time, timezone)
    # The open window's start is the last real cutoff at-or-before now - not
    # a full re-derivation of the organized frontier (that is the
    # scheduler's job, docs/DESIGN.md 4.2) - just "what would the next
    # scheduled cutoff organize if it fired right now".
    start = most_recent_cutoff(now, digest_local_time, timezone)
    if start < floor:
        start = floor
    if now <= start:
        return None
    return CaptureWindow(start=start, end=now)


def _format_organize_result(window: CaptureWindow, record: RunRecord | None) -> str:
    header = f"Window `{window.start.isoformat()}` -> `{window.end.isoformat()}`"
    if record is None:
        return f"{header}\nRun completed, but its status could not be re-read."
    return f"{header}\n{_format_run(record)}"


def _format_run(record: RunRecord) -> str:
    lines = [f"Run `{record.id}` - **{record.status}** ({record.kind})"]
    if record.window_start is not None and record.window_end is not None:
        lines.append(
            f"Window: `{record.window_start.isoformat()}` -> `{record.window_end.isoformat()}`"
        )
    if record.model_id is not None:
        lines.append(f"Model: `{record.model_id}` (provider: {record.model_provider or 'unknown'})")
    if record.input_tokens is not None or record.output_tokens is not None:
        lines.append(f"Tokens: {record.input_tokens or 0} in / {record.output_tokens or 0} out")
    if record.context_recall is not None:
        lines.append(f"Context recall: {record.context_recall}")
    if record.context_degraded:
        lines.append("⚠️ context assembly degraded (selector call failed or timed out)")
    if record.status == "failed":
        lines.append(f"Error: `{record.error_code}` - {record.error_detail}")
    return "\n".join(lines)


def _truncate(text: str) -> str:
    if len(text) <= _MESSAGE_LIMIT:
        return text
    marker = "\n… (truncated)"
    return text[: _MESSAGE_LIMIT - len(marker)] + marker


def _format_search_page(page: SearchPage, mode: str) -> str:
    header = f"**{mode}** search"
    if page.degraded:
        header += " — degraded: the semantic channel was unavailable"
    if not page.items:
        return f"{header}\nNo results."

    lines = [header]
    for result in page.items:
        channels = "+".join(result.channels) if result.channels else mode
        lines.append(f"`#{result.document_id}` **{result.title}** ({result.kind}, {channels})")
        lines.append(result.snippet)
    return _truncate("\n".join(lines))


def _format_ask_answer(answer: AskAnswer) -> str:
    if not answer.enabled:
        return (
            "Ask is not enabled on this deployment (`TC_ASK_ENABLED=false`, docs/adr/0010). "
            "Try `/search` instead."
        )
    if answer.degraded:
        return (
            "Ask is enabled, but Khoj could not answer right now - it may be unreachable, "
            "or have no chat model configured (docs/adr/0003 finding 4)."
        )

    lines = [answer.answer or "(no answer text)"]
    if answer.references:
        lines.append("")
        lines.append("**References:**")
        for reference in answer.references:
            lines.append(f"`#{reference.document_id}` {reference.title}")
    return _truncate("\n".join(lines))
