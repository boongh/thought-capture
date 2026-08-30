# One image, several entrypoints. The workspace shares a single lockfile
# (docs/DESIGN.md 17), so building per-service images would only duplicate the
# same dependency closure. Compose selects the process with `command`.

FROM python:3.14.7-slim-trixie AS base

# uv is pinned: an unpinned build tool makes the image non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependencies, cached separately from source so that editing a Python file
# does not re-resolve the environment.
# ---------------------------------------------------------------------------
COPY pyproject.toml uv.lock .python-version ./
COPY packages/domain/pyproject.toml packages/domain/
COPY packages/application/pyproject.toml packages/application/
COPY packages/infrastructure/pyproject.toml packages/infrastructure/
COPY apps/api/pyproject.toml apps/api/
COPY apps/discord_bot/pyproject.toml apps/discord_bot/
COPY apps/worker/pyproject.toml apps/worker/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-workspace --no-dev

# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------
COPY packages/ packages/
COPY apps/ apps/
COPY migrations/ migrations/
COPY alembic.ini ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Non-root, and owning only what it needs to write (docs/DESIGN.md 12.4).
RUN useradd --create-home --uid 10001 tc \
    && mkdir -p /data/attachments \
    && chown -R tc:tc /data
USER tc

# Attachments are canonical binary state and live on a volume, never in the
# image layer.
VOLUME ["/data/attachments"]

CMD ["python", "-m", "tc_discord_bot"]
