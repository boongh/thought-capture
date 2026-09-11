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


def test_api_service_does_not_forward_the_sidecar_base_url() -> None:
    data = cast(dict[str, Any], yaml.safe_load(DOCKER_COMPOSE_YML.read_text(encoding="utf-8")))
    api_environment = cast(dict[str, Any], data["services"]["api"]["environment"])
    assert "TC_EMBEDDING_SIDECAR_BASE_URL" not in api_environment, (
        "apps/api/src/tc_api/app.py only enqueues via PostgresEmbeddingForceSync "
        "and never constructs HttpEmbeddingClient - forwarding this into `api` "
        "would be dead configuration"
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
