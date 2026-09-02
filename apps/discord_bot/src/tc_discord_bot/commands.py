"""Owner-only slash commands: ``/organize`` and ``/status``.

Discord slash commands map to use cases directly, not HTTP loopback calls
(docs/DESIGN.md 5.1, 10) - ``organize`` here is the exact ``OrganizeWindow``
callable the worker's own scheduler drives, not a second implementation.

Message capture is scoped by guild/channel (docs/DESIGN.md 4.1's
``CaptureAllowlist``, ``adapter.py``/``client.py``). These commands are a
different concern - administrative operations that must work in a DM
regardless of which guild/channel is configured for capture - so each command
here checks the caller's Discord user id directly against the configured
owner, rather than reusing the channel-scoped capture allowlist, and is
explicitly registered as usable from a DM (``allowed_contexts`` below) as
well as a guild.

Registered as *global* commands (``build_admin_commands`` below), because a
DM-only deployment (docs/DESIGN.md 4.1: "leave both blank for DM-only
capture") has no guild to scope a faster guild-only registration to; see
``CaptureClient._sync_commands`` (client.py) for the guild-copy-for-instant-
availability-in-dev-plus-global-for-DMs sync strategy.

Scope note (2026-09-02): only ``/organize`` and ``/status`` are implemented.
``/search`` depends on the exact-search slice (still an open PR at the time
of writing), ``/ask`` is blocked on the Khoj-chat-credential privacy decision
ADR-0003 explicitly defers to the owner, and ``/undo`` has no restore/revert
use case built yet anywhere in the codebase. Building any of those is new,
separately-scoped work, not part of this slice.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import discord
from discord import app_commands

from tc_application.organize import OrganizeWindow
from tc_domain.capture import WorkspaceId
from tc_domain.windows import CaptureWindow, most_recent_cutoff, previous_cutoff_before
from tc_infrastructure.db.run_reader import PostgresRunReader, RunRecord
from tc_infrastructure.db.windows import PostgresCaptureWindows

logger = logging.getLogger(__name__)

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
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> tuple[app_commands.Command[Any, ..., Any], ...]:
    """Builds the ``/organize`` and ``/status`` commands, ready to register on a tree."""

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

        async with windows.locked(workspace_id, window) as acquired:
            if not acquired:
                await interaction.followup.send(
                    "This window is already being organized (by the scheduler or another "
                    "`/organize` call); try again shortly.",
                    ephemeral=True,
                )
                return

            try:
                await organize(workspace_id, window)
            except Exception as exc:
                # OrganizeWindow already wrote the sanitized failure to the run
                # row before re-raising (organize.py); nothing from `exc`
                # itself is safe to surface here (docs/DESIGN.md 14.2).
                logger.warning("organize_command.failed", extra={"error_class": type(exc).__name__})

        record = await runs.most_recent(workspace_id)
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

    return (organize_command, status_command)


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
