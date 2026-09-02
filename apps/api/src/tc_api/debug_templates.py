"""Hand-built HTML for the operator debug pages (docs/adr/0009).

Pure string building, no templating engine: three fixed layouts don't need
one. Every interpolated value that could contain user content goes through
``html.escape`` - thought bodies and digest markdown are free text and must
never be trusted as HTML.
"""

from __future__ import annotations

import datetime as dt
import uuid
from html import escape as esc

from tc_infrastructure.db.document_reader import DocumentSummary
from tc_infrastructure.db.entity_reader import EntityRecord
from tc_infrastructure.db.llm_call_reader import LlmCallRecord
from tc_infrastructure.db.thought_reader import ThoughtRecord

_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #1a1a1a; background: #fff; }
nav a { margin-right: 1rem; font-weight: 600; text-decoration: none; color: #0b5fff; }
nav a.active { color: #1a1a1a; text-decoration: underline; }
table { border-collapse: collapse; width: 100%; margin-top: 1rem; }
th, td { border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }
th { background: #f4f4f4; }
td.body { max-width: 40ch; white-space: pre-wrap; word-break: break-word; }
pre { background: #f7f7f7; padding: 1rem; overflow-x: auto; white-space: pre-wrap; }
.card { border: 1px solid #ddd; border-radius: 6px; padding: 1rem; margin-top: 1rem; }
.meta { color: #555; font-size: 0.9em; }
"""


def _page(title: str, active: str, body: str) -> str:
    def link(path: str, label: str) -> str:
        cls = ' class="active"' if active == path else ""
        return f'<a href="/debug/{path}"{cls}>{label}</a>'

    nav = (
        f"<nav>{link('thoughts', 'Thoughts')}"
        f"{link('entities', 'Entities')}"
        f"{link('digests', 'Digests')}"
        f"{link('runs', 'Runs')}</nav>"
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title><style>{_STYLE}</style></head>"
        f"<body><h1>{esc(title)}</h1>{nav}{body}</body></html>"
    )


def _fmt(value: dt.datetime | dt.date | dt.time | None) -> str:
    return esc(value.isoformat()) if value is not None else ""


def thoughts_page(records: list[ThoughtRecord], *, next_cursor: str | None) -> str:
    rows = "".join(
        f"<tr><td>{r.id}</td><td>{_fmt(r.client_local_date)} {_fmt(r.client_local_time)}</td>"
        f"<td>{esc(r.source)}</td><td class='body'>{esc(r.body)}</td>"
        f"<td>{len(r.attachments)}</td></tr>"
        for r in records
    )
    table = (
        "<table><thead><tr><th>ID</th><th>Local time</th><th>Source</th>"
        f"<th>Body</th><th>Attachments</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    if next_cursor:
        href = f"/debug/thoughts?cursor={esc(next_cursor)}"
        table += f'<p><a href="{href}">Next page &rarr;</a></p>'
    return _page("Raw thoughts", "thoughts", table)


def entities_page(records: list[EntityRecord]) -> str:
    rows = "".join(
        f"<tr><td>{esc(r.canonical_name)}</td><td>{esc(r.entity_type)}</td>"
        f"<td>{esc(', '.join(r.aliases))}</td><td>{r.mention_count}</td>"
        f"<td>{_fmt(r.last_mentioned_at)}</td><td>{_fmt(r.created_at)}</td></tr>"
        for r in records
    )
    table = (
        "<table><thead><tr><th>Canonical name</th><th>Type</th><th>Aliases</th>"
        f"<th>Mentions</th><th>Last mentioned</th><th>Created</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )
    return _page("Entities", "entities", table)


def digests_page(records: list[DocumentSummary], bodies: dict[uuid.UUID, str]) -> str:
    cards = "".join(
        f"<div class='card'><h2>{esc(r.title)}</h2>"
        f"<p class='meta'>{esc(r.stable_key)} &middot; revision {r.revision_number} "
        f"&middot; updated {_fmt(r.updated_at)}</p>"
        f"<p>{esc(r.change_summary)}</p>"
        f"<pre>{esc(bodies.get(r.id, ''))}</pre></div>"
        for r in records
    )
    if not records:
        cards = "<p>No digests yet.</p>"
    return _page("Daily digests", "digests", cards)


def runs_page(records: list[LlmCallRecord], *, next_cursor: str | None) -> str:
    """Recent journaled LLM calls (docs/adr/0008), read-only.

    Shows request-shape metadata used to evaluate a candidate model
    (docs/model-evaluation-organize-select.md) - which model was asked, the
    reasoning-effort control sent (if any), cost and latency - never
    ``request_messages``/``response_raw`` (raw prompt/completion content;
    see ``LlmCallRecord``'s own docstring for why those are excluded).
    """
    rows = "".join(
        f"<tr><td>{_fmt(r.created_at)}</td><td>{esc(r.step)}</td>"
        f"<td>{esc(r.model_requested)}</td><td>{esc(r.model_served or '')}</td>"
        f"<td>{esc(r.provider or '')}</td>"
        f"<td>{esc(str(r.request_params.get('reasoning_effort') or ''))}</td>"
        f"<td>{r.input_tokens if r.input_tokens is not None else ''}</td>"
        f"<td>{r.output_tokens if r.output_tokens is not None else ''}</td>"
        f"<td>{esc(str(r.estimated_cost_usd)) if r.estimated_cost_usd is not None else ''}</td>"
        f"<td>{r.latency_ms if r.latency_ms is not None else ''}</td>"
        f"<td>{esc(r.error_code or '')}</td></tr>"
        for r in records
    )
    table = (
        "<table><thead><tr><th>Created</th><th>Step</th><th>Model requested</th>"
        "<th>Model served</th><th>Provider</th><th>Reasoning effort</th>"
        "<th>Input tok</th><th>Output tok</th><th>Cost (USD)</th>"
        f"<th>Latency (ms)</th><th>Error</th></tr></thead><tbody>{rows}</tbody></table>"
    )
    if not records:
        table = "<p>No LLM calls journaled yet.</p>"
    elif next_cursor:
        href = f"/debug/runs?cursor={esc(next_cursor)}"
        table += f'<p><a href="{href}">Next page &rarr;</a></p>'
    return _page("LLM calls", "runs", table)
