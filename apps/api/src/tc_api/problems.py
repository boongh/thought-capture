"""RFC 9457 problem details.

Every error response has the same shape and carries the request id, so a report
of "it returned 500" can be traced to a specific request in the logs
(docs/DESIGN.md 10).
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

PROBLEM_CONTENT_TYPE = "application/problem+json"

# A stable, greppable vocabulary. Clients branch on `type`, never on prose.
TYPE_PREFIX = "https://thought-capture.local/problems/"


class ProblemError(Exception):
    """An error that should surface as a problem document."""

    def __init__(
        self,
        *,
        status: int,
        title: str,
        code: str,
        detail: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.code = code
        self.detail = detail
        self.extra = extra or {}
        # e.g. `WWW-Authenticate`, so a 401 can trigger a browser's native
        # credential prompt rather than only being machine-readable.
        self.headers = headers


def problem_response(request: Request, error: ProblemError) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{TYPE_PREFIX}{error.code}",
        "title": error.title,
        "status": error.status,
        "request_id": getattr(request.state, "request_id", None),
    }
    if error.detail:
        body["detail"] = error.detail
    body.update(error.extra)

    return JSONResponse(
        status_code=error.status,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=error.headers,
    )


def unauthorized(detail: str) -> ProblemError:
    return ProblemError(status=401, title="Unauthorized", code="unauthorized", detail=detail)


def not_found(detail: str) -> ProblemError:
    return ProblemError(status=404, title="Not Found", code="not-found", detail=detail)


def bad_request(code: str, detail: str) -> ProblemError:
    return ProblemError(status=400, title="Bad Request", code=code, detail=detail)


def unprocessable(code: str, detail: str) -> ProblemError:
    return ProblemError(status=422, title="Unprocessable Content", code=code, detail=detail)


def service_unavailable(detail: str) -> ProblemError:
    return ProblemError(
        status=503, title="Service Unavailable", code="dependency-unavailable", detail=detail
    )


def not_implemented(code: str, detail: str) -> ProblemError:
    return ProblemError(status=501, title="Not Implemented", code=code, detail=detail)
