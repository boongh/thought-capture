"""Bounds on `Settings`.

Every field here exists to cap a resource (settings.py's own comments say
which). A bound that is only documented is not a bound: `0`, a negative
value, or an empty string used to be accepted silently, and the resulting
process still reported a *green* `/health` while being unable to serve -
`TC_EMBEDDING_MAX_CONCURRENT_ENCODES=0` loads the model, answers `/health`
with `status: ok`, and blocks every non-empty `/embed` forever on a
semaphore that can never be acquired. These tests pin the failure to
process start instead.

`Settings()` is constructed directly rather than through the `lru_cache`d
`get_settings()`, so no test here can leave a poisoned cache behind for the
next one; the one test that does go through `get_settings()` clears the
cache around itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from tc_embedding_sidecar.settings import Settings, get_settings

REPO_ENV_PREFIX = "TC_EMBEDDING_"
# Generous enough for a cold `import uvicorn` on a slow machine, short enough
# that a regression (a process that starts and serves) fails the suite quickly.
STARTUP_TIMEOUT_SECONDS = 60


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `TC_EMBEDDING_*` value exported in the developer's own shell would
    otherwise leak into every construction below and make these assertions
    depend on the machine."""
    for key in list(os.environ):
        if key.startswith(REPO_ENV_PREFIX):
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_valid() -> None:
    settings = Settings()

    assert settings.max_concurrent_encodes >= 1
    assert settings.limit_concurrency >= (
        settings.max_concurrent_encodes + settings.max_queued_encodes
    )


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        # Strictly positive: zero or negative disables the thing the bound
        # was sized to protect, or wedges the service outright.
        ("MAX_BATCH_SIZE", "0"),
        ("MAX_BATCH_SIZE", "-1"),
        ("MAX_TEXT_BYTES", "0"),
        ("MAX_TEXT_BYTES", "-1"),
        ("MAX_CONCURRENT_ENCODES", "0"),
        ("MAX_CONCURRENT_ENCODES", "-1"),
        ("MAX_QUEUED_ENCODES", "-1"),
        ("MAX_REQUEST_BYTES", "0"),
        ("MAX_REQUEST_BYTES", "-1"),
        ("MAX_BODY_READ_SECONDS", "0"),
        ("MAX_BODY_READ_SECONDS", "-1.5"),
        ("LIMIT_CONCURRENCY", "0"),
        ("LIMIT_CONCURRENCY", "-1"),
        ("H11_MAX_INCOMPLETE_EVENT_SIZE", "0"),
        ("H11_MAX_INCOMPLETE_EVENT_SIZE", "-1"),
        # Positive but far too small to hold a request line and a Host header:
        # h11 would abandon every connection mid-header.
        ("H11_MAX_INCOMPLETE_EVENT_SIZE", "1"),
        ("H11_MAX_INCOMPLETE_EVENT_SIZE", "512"),
        # A port outside the range the OS can actually bind.
        ("PORT", "0"),
        ("PORT", "65536"),
        ("PORT", "-1"),
        # Empty is not a model, a revision, or a bind address.
        ("MODEL_ID", ""),
        ("MODEL_REVISION", ""),
        ("HOST", ""),
    ],
)
def test_invalid_value_is_rejected_naming_the_field(
    monkeypatch: pytest.MonkeyPatch, env_name: str, value: str
) -> None:
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}{env_name}", value)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    field = env_name.lower()
    assert field in str(excinfo.value).lower(), (
        f"{env_name}={value!r} was rejected, but the error does not name {field!r}: {excinfo.value}"
    )


def test_zero_queued_encodes_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 is a meaningful setting here, not an invalid one: admit only what can
    run right now and 429 everything else. Guards against over-tightening this
    field to `gt=0` along with its strictly-positive neighbours."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_QUEUED_ENCODES", "0")

    assert Settings().max_queued_encodes == 0


@pytest.mark.parametrize("limit", ["9", "10", "11"])
def test_limit_concurrency_below_admission_total_plus_overhead_is_rejected(
    monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    """Admission total is 10 here, so 10 and 11 are the interesting cases: at
    10 uvicorn 503s the last queued request (its test is `>=`, and the current
    connection is already counted), and at 11 the container healthcheck is the
    connection turned away once the queue is full."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_CONCURRENT_ENCODES", "2")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_QUEUED_ENCODES", "8")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}LIMIT_CONCURRENCY", limit)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "TC_EMBEDDING_LIMIT_CONCURRENCY" in message
    assert "TC_EMBEDDING_MAX_QUEUED_ENCODES" in message


def test_limit_concurrency_at_admission_total_plus_overhead_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is exactly `total + 2`, not "comfortably more" - an operator
    who sizes uvicorn to it is correct, if tight. Guards against the bound
    drifting upward into an arbitrary number."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_CONCURRENT_ENCODES", "2")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_QUEUED_ENCODES", "8")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}LIMIT_CONCURRENCY", "12")

    assert Settings().limit_concurrency == 12


def test_request_byte_cap_below_the_advertised_batch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TC_EMBEDDING_MAX_REQUEST_BYTES=1` used to start happily: `/health` is a
    bodyless GET and still answered "ok", while every `/embed` 413'd - the same
    green-health-check-over-a-dead-service shape as
    `MAX_CONCURRENT_ENCODES=0`."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_REQUEST_BYTES", "1")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert "TC_EMBEDDING_MAX_REQUEST_BYTES" in str(excinfo.value)


def test_raising_the_batch_limit_without_the_byte_cap_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cross-field case: neither field is individually out of range, but
    together they advertise a batch the byte cap would reject."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_BATCH_SIZE", "256")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "TC_EMBEDDING_MAX_REQUEST_BYTES" in message
    assert "TC_EMBEDDING_MAX_BATCH_SIZE" in message


def test_byte_cap_exactly_at_the_worst_case_advertised_batch_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The floor is the *worst-case* JSON-serialized size, not the raw byte
    total: `max_batch_size=2, max_text_bytes=1000` requires
    `2*1000*6 + 2*3 + 12 = 12_018` bytes to admit a batch of two
    all-control-character texts at exactly the advertised size - the true
    minimum a maximally escaped, contract-legal request could serialize to.
    An earlier version of this test pinned the bug this floor fixes: it
    accepted `max_request_bytes=2000` for the same batch shape, which the
    real middleware would 413 for any content that needs JSON escaping."""
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_BATCH_SIZE", "2")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_TEXT_BYTES", "1000")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_REQUEST_BYTES", "12018")

    assert Settings().max_request_bytes == 12018


def test_byte_cap_one_below_the_worst_case_advertised_batch_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_BATCH_SIZE", "2")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_TEXT_BYTES", "1000")
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_REQUEST_BYTES", "12017")

    with pytest.raises(ValidationError):
        Settings()


def test_default_byte_cap_admits_the_default_worst_case_batch() -> None:
    """Pins the exact number, not just an inequality, so a future change to
    either default that breaks the relationship fails loudly rather than
    silently staying under a loose bound."""
    settings = Settings()
    worst_case_default_batch = (
        settings.max_batch_size * settings.max_text_bytes * 6 + settings.max_batch_size * 3 + 12
    )

    assert worst_case_default_batch == 3_145_932
    assert worst_case_default_batch <= settings.max_request_bytes


def test_request_byte_cap_error_names_all_three_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_BATCH_SIZE", "256")

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    message = str(excinfo.value)
    assert "TC_EMBEDDING_MAX_REQUEST_BYTES" in message
    assert "TC_EMBEDDING_MAX_BATCH_SIZE" in message
    assert "TC_EMBEDDING_MAX_TEXT_BYTES" in message


def test_get_settings_propagates_the_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`get_settings()` is `lru_cache`d; the cache must not turn a rejected
    configuration into a cached success (or vice versa) for the caller in
    `__main__.main()`."""
    get_settings.cache_clear()
    monkeypatch.setenv(f"{REPO_ENV_PREFIX}MAX_CONCURRENT_ENCODES", "0")
    try:
        with pytest.raises(ValidationError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_invalid_env_exits_before_the_port_is_bound() -> None:
    """The whole point of the finding: the failure must be loud and *early*.

    What this pins is the observable property - `python -m tc_embedding_sidecar`
    with an invalid value exits non-zero and never logs "Uvicorn running on",
    so the container never reaches a listening state or a green healthcheck.

    It deliberately does NOT claim to pin *where* the error is raised.
    `__main__.main()` calls `get_settings()` before `uvicorn.run`, which is the
    right placement, but an error raised inside the `factory=True` app factory
    would also exit before a socket exists: uvicorn calls `config.load()` (and
    therefore the factory) in `Server._serve` before `await self.startup(...)`
    binds anything. An earlier version of this docstring claimed otherwise.
    """
    project_root = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "tc_embedding_sidecar"],
            cwd=project_root,
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=STARTUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:  # pragma: no cover - only on regression
        # Still running after the timeout means it did not reject the
        # configuration at all: it bound the port and is serving. That is the
        # exact regression this test exists to catch, so report it as such
        # rather than as an infrastructure flake.
        pytest.fail(
            "`python -m tc_embedding_sidecar` was still running after "
            f"{STARTUP_TIMEOUT_SECONDS}s with TC_EMBEDDING_MAX_CONCURRENT_ENCODES=0 - "
            "an invalid configuration must exit non-zero before uvicorn binds a port"
        )

    assert completed.returncode != 0, (
        "the process started successfully with an invalid configuration; "
        f"stdout={completed.stdout!r}"
    )
    combined = f"{completed.stdout}{completed.stderr}".lower()
    assert "max_concurrent_encodes" in combined
    assert "uvicorn running on" not in combined


def _child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(REPO_ENV_PREFIX)}
    env[f"{REPO_ENV_PREFIX}MAX_CONCURRENT_ENCODES"] = "0"
    # Prepend rather than replace: clobbering an inherited PYTHONPATH would
    # make the child's import environment differ from the developer's, and the
    # resulting ImportError would surface as a confusing failure on the
    # "names the field" assertion rather than at its real cause.
    src = str(Path(__file__).resolve().parents[1] / "src")
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{inherited}" if inherited else src
    return env
