# Own image, own pinned base - deliberately not layered onto the main
# workspace image (Dockerfile at the repository root): this process needs a
# different Python (3.12, not 3.14) and a different dependency closure
# entirely (docs/adr/0010 §5).
#
# Base pinned by digest, per this repo's convention (see the root Dockerfile
# and deploy/compose/docker-compose.yml for the same pattern applied
# elsewhere):
#   docker pull python:3.12.14-slim-trixie
#   docker inspect --format='{{index .RepoDigests 0}}' python:3.12.14-slim-trixie
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

# uv is pinned: an unpinned build tool makes the image non-reproducible
# (same rationale as the root Dockerfile).
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # sentence-transformers/huggingface_hub cache directories - pointed at a
    # location the non-root user below actually owns, and baked into the
    # image at build time (below) so a fresh container never pays the
    # ~30s cold-start download on every restart, only on every rebuild.
    HF_HOME=/home/tc/.cache/huggingface

WORKDIR /app

RUN useradd --create-home --uid 10001 tc

COPY pyproject.toml uv.lock .python-version ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p "$HF_HOME" && chown -R tc:tc /app "$HF_HOME"
USER tc

# Bakes the pinned model's weights into the image layer, so a container
# restart never depends on network access to Hugging Face's hub at runtime
# (docs/DESIGN.md 9's "local by default"; docs/adr/0010 §5's "no external
# calls"). Uses the same settings module the app itself reads - including
# `model_revision` (settings.py's own docstring has the full reasoning for
# why the bare model id alone is not enough) - so the model id and pinned
# revision can never drift between build time and run time.
RUN python -c "from tc_embedding_sidecar.settings import get_settings; \
    from sentence_transformers import SentenceTransformer; \
    settings = get_settings(); \
    SentenceTransformer(settings.model_id, revision=settings.model_revision)"

# Only takes effect after the bake step above, which needs the opposite -
# forcing this on earlier would make that download fail. Guarantees the
# running process never falls back to a network round-trip against Hugging
# Face's hub even if it is transiently reachable, matching "no external
# calls" as an enforced property, not an incidental one.
ENV HF_HUB_OFFLINE=1

EXPOSE 8081

CMD ["python", "-m", "tc_embedding_sidecar"]
