"""Static regression guard for review finding F12
(docs/plans/embedding-sync-review-round-2.md): the `worker` service silently
never saw an operator's `TC_EMBEDDING_SIDECAR_BASE_URL` override, because
`deploy/compose/docker-compose.yml` forwarded
`TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS`/`TC_EMBEDDING_SYNC_BATCH_SIZE` into the
container's environment but not the base URL. `env.example`'s
`TC_EMBEDDING_SIDECAR_BASE_URL` doubled as a host-loopback value for the
contract-test overlay, so the naive fix - forwarding it with a loopback
default - would have injected an unreachable `127.0.0.1` URL into every
operator's `worker` container instead. The fix splits the variable:
`TC_EMBEDDING_SIDECAR_BASE_URL` (container) and
`TC_EMBEDDING_SIDECAR_HOST_BASE_URL` (host, contract tests only).

These tests assert, by static inspection (no Docker needed), that every
`TC_EMBEDDING_*` setting the worker's code path actually reads is forwarded
into the `worker` service's `environment`, and that the forwarded default for
the base URL is the in-container service name, never a loopback address -
the same style of cheap regression guard
`tests/unit/test_check_backup_restore_isolation.py::TestDockerComposeForwardsBackupTimeout::test_docker_compose_yml_forwards_the_variable_to_the_backup_service`
already uses for `TC_BACKUP_COPY_TIMEOUT_SECONDS`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCKER_COMPOSE_YML = REPO_ROOT / "deploy" / "compose" / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / "env.example"
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"
CHECK_PS1 = REPO_ROOT / "scripts" / "check.ps1"
CONTRACT_CONFTEST = REPO_ROOT / "tests" / "contract" / "embedding_sidecar" / "conftest.py"
SIDECAR_DOCKERFILE = REPO_ROOT / "apps" / "embedding_sidecar" / "Dockerfile"
SIDECAR_SETTINGS = (
    REPO_ROOT / "apps" / "embedding_sidecar" / "src" / "tc_embedding_sidecar" / "settings.py"
)

# The two build-time model pins (review finding F16): a `.env` override alone
# never reaches the sidecar's bake step, so Compose's build.args AND
# environment blocks, the Dockerfile's ARG defaults, and settings.py's field
# defaults must all agree - see the sidecar service's own anchor comment in
# deploy/compose/docker-compose.yml for the full reasoning.
MODEL_VARS = ("TC_EMBEDDING_MODEL_ID", "TC_EMBEDDING_MODEL_REVISION")


def _embedding_sidecar_service() -> dict[str, Any]:
    data = cast(dict[str, Any], yaml.safe_load(DOCKER_COMPOSE_YML.read_text(encoding="utf-8")))
    return cast(dict[str, Any], data["services"]["embedding-sidecar"])


def _compose_default(raw: str, var: str) -> str:
    """Pull the `${VAR:-default}` fallback out of a raw Compose YAML string
    value, without ever hard-coding the expected default in this test."""
    match = re.search(r"\$\{" + re.escape(var) + r":-([^}]*)\}", raw)
    assert match is not None, f"{var} is not written as ${{{var}:-default}} - got: {raw!r}"
    return match.group(1)


def _dockerfile_arg_default(var: str) -> str:
    text = SIDECAR_DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(rf"^ARG {var}=(.+)$", text, re.MULTILINE)
    assert match is not None, (
        f"apps/embedding_sidecar/Dockerfile must declare `ARG {var}=<default>`"
    )
    return match.group(1).strip()


def _settings_field_default(field: str) -> str:
    text = SIDECAR_SETTINGS.read_text(encoding="utf-8")
    match = re.search(rf'^\s*{field}: str = Field\(default="([^"]+)"', text, re.MULTILINE)
    assert match is not None, f'settings.py must declare `{field}: str = Field(default="...")`'
    return match.group(1)


def test_embedding_sidecar_service_forwards_model_vars_at_runtime() -> None:
    environment = _embedding_sidecar_service()["environment"]
    for var in MODEL_VARS:
        assert var in environment, (
            f"deploy/compose/docker-compose.yml's `embedding-sidecar` service must "
            f"forward {var} into the container's runtime environment - without this, "
            f"an operator's .env override is silently ignored (review finding F16)"
        )


def test_embedding_sidecar_service_passes_model_vars_as_build_args() -> None:
    build_args = _embedding_sidecar_service()["build"]["args"]
    for var in MODEL_VARS:
        assert var in build_args, (
            f"deploy/compose/docker-compose.yml's `embedding-sidecar` service must pass "
            f"{var} as a build.arg - without this, the model baked into the image at "
            f"build time never reflects an operator's .env override, and forwarding it "
            f"only at runtime (above) would name a model whose weights were never baked, "
            f"crashing the container at start with HF_HUB_OFFLINE=1 set (review finding F16)"
        )


def test_model_var_defaults_agree_across_compose_dockerfile_and_settings() -> None:
    service = _embedding_sidecar_service()
    field_by_var = {
        "TC_EMBEDDING_MODEL_ID": "model_id",
        "TC_EMBEDDING_MODEL_REVISION": "model_revision",
    }
    for var, field in field_by_var.items():
        compose_env_default = _compose_default(str(service["environment"][var]), var)
        compose_build_arg_default = _compose_default(str(service["build"]["args"][var]), var)
        dockerfile_default = _dockerfile_arg_default(var)
        settings_default = _settings_field_default(field)

        all_defaults = {
            "compose environment": compose_env_default,
            "compose build.args": compose_build_arg_default,
            "Dockerfile ARG": dockerfile_default,
            "settings.py field": settings_default,
        }
        distinct = set(all_defaults.values())
        assert len(distinct) == 1, (
            f"{var} defaults have drifted between build-time and run-time sources "
            f"(review finding F16 exists specifically to prevent this): {all_defaults!r}"
        )


def test_env_example_documents_the_model_vars() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for var in MODEL_VARS:
        assert re.search(rf"^{var}=.+$", text, re.MULTILINE), (
            f"env.example must document {var} alongside the other TC_EMBEDDING_* keys "
            f"(review finding F16) - operators need a visible place to notice this "
            f"variable exists and that it is a build-time pin"
        )


# Every TC_EMBEDDING_* setting apps/worker/src/tc_worker/__main__.py's
# HttpEmbeddingClient/DeliverEmbeddingSync construction actually reads
# (config.py's Settings fields for the sidecar).
WORKER_EMBEDDING_ENV_VARS = (
    "TC_EMBEDDING_SIDECAR_BASE_URL",
    "TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS",
    "TC_EMBEDDING_SYNC_BATCH_SIZE",
)


def _worker_environment() -> dict[str, Any]:
    data = cast(dict[str, Any], yaml.safe_load(DOCKER_COMPOSE_YML.read_text(encoding="utf-8")))
    return cast(dict[str, Any], data["services"]["worker"]["environment"])


def test_worker_service_forwards_every_embedding_setting_it_reads() -> None:
    environment = _worker_environment()
    for var in WORKER_EMBEDDING_ENV_VARS:
        assert var in environment, (
            f"deploy/compose/docker-compose.yml's `worker` service must forward "
            f"{var} into the container - without this, an operator's .env "
            f"override is silently ignored and worker always falls back to "
            f"the Settings default (review finding F12)"
        )


def test_worker_base_url_default_is_the_in_container_service_name() -> None:
    environment = _worker_environment()
    forwarded_value = environment["TC_EMBEDDING_SIDECAR_BASE_URL"]
    assert forwarded_value == "${TC_EMBEDDING_SIDECAR_BASE_URL:-http://embedding-sidecar:8081}", (
        f"the worker container's default for TC_EMBEDDING_SIDECAR_BASE_URL must "
        f"be the in-container service name (matching config.py Settings' own "
        f"default), never a host-loopback address - a loopback default here "
        f"would silently break embedding sync for every operator who copied "
        f"env.example unchanged (review finding F12) - got: {forwarded_value!r}"
    )
    assert "127.0.0.1" not in forwarded_value


def _api_environment() -> dict[str, Any]:
    data = cast(dict[str, Any], yaml.safe_load(DOCKER_COMPOSE_YML.read_text(encoding="utf-8")))
    return cast(dict[str, Any], data["services"]["api"]["environment"])


# Since review round 4 (`6ac8c6b`), apps/api/src/tc_api/app.py constructs a
# real `HttpEmbeddingClient` and injects it into `StartReembedRun` for
# `POST /v1/admin/reembed` - api embeds now, not just enqueues. Only these two
# variables (matching WORKER_EMBEDDING_ENV_VARS minus the batch size, which
# has no sync loop to read it in `api`) need forwarding.
API_EMBEDDING_ENV_VARS = (
    "TC_EMBEDDING_SIDECAR_BASE_URL",
    "TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS",
)


def test_api_service_forwards_every_embedding_setting_it_reads() -> None:
    environment = _api_environment()
    for var in API_EMBEDDING_ENV_VARS:
        assert var in environment, (
            f"deploy/compose/docker-compose.yml's `api` service must forward "
            f"{var} into the container - since review round 4 (6ac8c6b), "
            f"apps/api/src/tc_api/app.py builds a real HttpEmbeddingClient for "
            f"POST /v1/admin/reembed, so an operator's .env override being "
            f"silently ignored here means reembed either 503s or silently "
            f"talks to a different sidecar than sync does (review finding F20)"
        )


def test_api_base_url_default_matches_worker_default() -> None:
    api_environment = _api_environment()
    worker_environment = _worker_environment()
    assert (
        api_environment["TC_EMBEDDING_SIDECAR_BASE_URL"]
        == worker_environment["TC_EMBEDDING_SIDECAR_BASE_URL"]
    ), (
        "api and worker must forward TC_EMBEDDING_SIDECAR_BASE_URL with the "
        "byte-identical default (the in-container service name, never a "
        "loopback address - review finding F12) - otherwise reembed and sync "
        "could silently talk to two different sidecars"
    )
    assert "127.0.0.1" not in str(api_environment["TC_EMBEDDING_SIDECAR_BASE_URL"])


def test_api_timeout_default_matches_worker_default() -> None:
    api_environment = _api_environment()
    worker_environment = _worker_environment()
    assert (
        api_environment["TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS"]
        == worker_environment["TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS"]
    ), (
        "api and worker must forward TC_EMBEDDING_SIDECAR_TIMEOUT_SECONDS with "
        "the byte-identical default (review finding F20)"
    )


def test_api_service_does_not_forward_the_sync_batch_size() -> None:
    api_environment = _api_environment()
    assert "TC_EMBEDDING_SYNC_BATCH_SIZE" not in api_environment, (
        "apps/api has no sync loop (only apps/worker's EmbeddingSyncLoop reads "
        "TC_EMBEDDING_SYNC_BATCH_SIZE) - forwarding it into `api` would be "
        "dead configuration (review finding F20, point 2)"
    )


def test_env_example_documents_the_container_url_as_the_service_name() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^TC_EMBEDDING_SIDECAR_BASE_URL=(.+)$", text, re.MULTILINE)
    assert match is not None, "env.example must document TC_EMBEDDING_SIDECAR_BASE_URL"
    assert match.group(1) == "http://embedding-sidecar:8081", (
        "env.example's TC_EMBEDDING_SIDECAR_BASE_URL must model the "
        "container-side default (the in-container service name), matching "
        "what deploy/compose/docker-compose.yml actually forwards - "
        f"got: {match.group(1)!r}"
    )


def test_env_example_documents_a_separate_host_url_for_contract_tests() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^TC_EMBEDDING_SIDECAR_HOST_BASE_URL=(.+)$", text, re.MULTILINE)
    assert match is not None, (
        "env.example must document a SEPARATE TC_EMBEDDING_SIDECAR_HOST_BASE_URL "
        "for the host-loopback value the contract-test overlay publishes - "
        "collapsing this into TC_EMBEDDING_SIDECAR_BASE_URL was the exact "
        "overloaded-name bug review finding F12 fixed"
    )
    assert match.group(1) == "http://127.0.0.1:8081"


def test_check_scripts_probe_the_host_variable_not_the_container_one() -> None:
    sh_text = CHECK_SH.read_text(encoding="utf-8")
    ps1_text = CHECK_PS1.read_text(encoding="utf-8")
    assert "TC_EMBEDDING_SIDECAR_HOST_BASE_URL" in sh_text, (
        "scripts/check.sh must probe TC_EMBEDDING_SIDECAR_HOST_BASE_URL (the "
        "host-loopback variable), not TC_EMBEDDING_SIDECAR_BASE_URL (the "
        "container variable) - the host running check.sh is never the "
        "worker container"
    )
    assert "TC_EMBEDDING_SIDECAR_HOST_BASE_URL" in ps1_text, (
        "scripts/check.ps1 must probe TC_EMBEDDING_SIDECAR_HOST_BASE_URL for "
        "the same reason as check.sh"
    )


def test_contract_conftest_reads_the_host_variable() -> None:
    text = CONTRACT_CONFTEST.read_text(encoding="utf-8")
    assert 'os.environ.get(\n    "TC_EMBEDDING_SIDECAR_HOST_BASE_URL"' in text or (
        "TC_EMBEDDING_SIDECAR_HOST_BASE_URL" in text and "os.environ.get" in text
    ), (
        "tests/contract/embedding_sidecar/conftest.py must read "
        "TC_EMBEDDING_SIDECAR_HOST_BASE_URL, not TC_EMBEDDING_SIDECAR_BASE_URL - "
        "these contract tests run on the host, against the port the "
        "contract-test overlay publishes to loopback"
    )
