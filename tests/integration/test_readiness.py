"""Liveness vs readiness: the process stays up and answers /health while the container is pending or failing."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import httpx
import pytest

from orqentis import bootstrap
from orqentis.api.app import create_app
from orqentis.bootstrap import Container, build_container
from orqentis.config.settings import Settings
from orqentis.runtime.supervisor import describe_failure
from orqentis.storage.sqlite import SqliteDatabase
from tests.conftest import make_settings
from tests.support.polling import wait_until

VIEWER = {"X-Orqentis-User": "victor@contoso.com"}


def fast_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "startup_grace_seconds": 0,  # return from start-up at once, as a slow real build would force us to
        "build_retry_initial_seconds": 0.01,
        "build_retry_max_seconds": 0.05,
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def client_for(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")  # type: ignore[arg-type]


async def test_a_failing_build_keeps_the_process_alive_reports_a_redacted_reason_and_is_retried(
    tmp_path: Path,
) -> None:
    settings = fast_settings(tmp_path)
    attempts = 0

    async def flaky_factory(s: Settings) -> Container:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ValueError(f"contracts file {s.data_dir}/estate.json is invalid: client_secret=hunter2-SECRET")
        return await build_container(s, reset=True)

    app = create_app(settings, container_factory=flaky_factory)
    async with app.router.lifespan_context(app), client_for(app) as http:
        await wait_until(lambda: attempts >= 1)
        # Liveness is unaffected by the failing build...
        health = await http.get("/health")
        assert health.status_code == 200 and health.json()["status"] == "ok"
        # ...readiness says why not, without leaking the secret that was in the exception message...
        not_ready = await http.get("/health/ready")
        assert not_ready.status_code == 503
        body = not_ready.json()
        assert body["status"] == "not_ready" and body["reason"].startswith("ValueError: contracts file")
        assert "hunter2" not in not_ready.text and body["attempts"] >= 1
        # ...and requests that need the container get a clear, retryable 503 instead of a 500.
        blocked = await http.get("/incidents", headers=VIEWER)
        assert blocked.status_code == 503 and blocked.json()["error"] == "NOT_READY"
        assert blocked.headers["retry-after"] == "5"

        # The build is retried with backoff until it works; the process never restarted.
        await wait_until(lambda: app.state.runtime.ready)
        assert attempts == 3
        ready = await http.get("/health/ready")
        assert ready.status_code == 200 and ready.json() == {"status": "ready", "scheduler": "disabled"}
        assert (await http.get("/incidents", headers=VIEWER)).status_code == 200
        assert (await http.get("/health")).status_code == 200


async def test_readiness_is_503_while_the_build_is_still_pending_and_200_after(tmp_path: Path) -> None:
    gate = threading.Event()  # the build "enumerates the estate" until the test lets it finish

    async def slow_factory(s: Settings) -> Container:
        await wait_until(gate.is_set)
        return await build_container(s, reset=True)

    app = create_app(fast_settings(tmp_path), container_factory=slow_factory)
    async with app.router.lifespan_context(app), client_for(app) as http:
        assert (await http.get("/health")).status_code == 200
        pending = await http.get("/health/ready")
        assert pending.status_code == 503 and pending.json()["reason"] == "starting"
        gate.set()
        await wait_until(lambda: app.state.runtime.ready)
        assert (await http.get("/health/ready")).status_code == 200


async def test_a_fast_build_is_ready_before_the_first_request(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)  # default grace period
    app = create_app(settings)
    async with app.router.lifespan_context(app), client_for(app) as http:
        assert (await http.get("/health/ready")).status_code == 200
        assert (await http.get("/health")).json()["reasoner"] == "rule_based"


async def test_health_needs_no_container(tmp_path: Path) -> None:
    async def never(s: Settings) -> Container:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    app = create_app(fast_settings(tmp_path), container_factory=never)
    async with app.router.lifespan_context(app), client_for(app) as http:
        health = (await http.get("/health")).json()
        assert health["status"] == "ok" and health["fabric_mode"] == "mock" and health["reasoner"] == "rule_based"


async def test_a_real_build_failure_in_the_composition_root_does_not_crash_or_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing policy file fails the real build after the databases were opened: they must be closed again."""
    opened: list[SqliteDatabase] = []

    class TrackedDatabase(SqliteDatabase):
        def __init__(self, path: Path | str) -> None:
            super().__init__(path)
            opened.append(self)

    monkeypatch.setattr(bootstrap, "SqliteDatabase", TrackedDatabase)
    app = create_app(fast_settings(tmp_path, policy_path=tmp_path / "no-such-policy.yaml"))
    async with app.router.lifespan_context(app), client_for(app) as http:
        await wait_until(lambda: app.state.runtime.status.attempts >= 3)
        assert (await http.get("/health")).status_code == 200
        ready = await http.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["reason"].startswith("FileNotFoundError")
    assert len(opened) >= 6  # state + events database for each attempt
    # Every database opened by a failed attempt was closed again (a closed connection refuses to run anything).
    for db in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            db._conn.execute("SELECT 1")


def test_failure_descriptions_are_one_redacted_line() -> None:
    text = describe_failure(RuntimeError("first line token=abc123\nsecond line with detail\nthird"))
    assert text == "RuntimeError: first line token=[REDACTED]"
    assert describe_failure(KeyError()) == "KeyError"
    assert len(describe_failure(ValueError("x" * 5000))) < 260
