"""PostgreSQL and Azurite for the storage tests: an existing server (CI) or a throwaway Docker container (local).

* ``ORQENTIS_TEST_POSTGRES_HOST`` / ``_PORT`` / ``_USER`` name a server that is already running (the CI service
  container). It must accept the user without a password (``POSTGRES_HOST_AUTH_METHOD=trust``) and let it create
  schemas; no credential is involved anywhere.
* ``ORQENTIS_TEST_AZURITE_URL`` / ``_ACCOUNT`` / ``_KEY`` name a running Azurite. Without them a container is
  started with an account whose key is **generated for this run** (never a committed or well-known key).
* Otherwise Docker is used. With neither a server nor Docker the dependent tests are *skipped*, unless
  ``ORQENTIS_REQUIRE_SERVICES=1`` (set by CI): then they fail, so a broken service can never turn a CI job green.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

POSTGRES_IMAGE = "postgres:17"
AZURITE_IMAGE = "mcr.microsoft.com/azure-storage/azurite"


@dataclass(frozen=True)
class PostgresServer:
    host: str
    port: int
    user: str = "postgres"
    database: str = "postgres"


@dataclass(frozen=True)
class AzuriteServer:
    url: str  # http://127.0.0.1:<port>/<account>
    account: str
    key: str


def services_required() -> bool:
    return os.environ.get("ORQENTIS_REQUIRE_SERVICES") == "1"


def skip_or_fail(reason: str) -> None:
    if services_required():
        pytest.fail(f"{reason} (ORQENTIS_REQUIRE_SERVICES=1)")
    pytest.skip(reason)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker(*args: str, timeout: float = 120) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _published_port(container: str, private: int) -> int:
    # "127.0.0.1:50906" (one line per address family)
    return int(_docker("port", container, f"{private}/tcp").splitlines()[0].rsplit(":", 1)[1])


def start_postgres() -> Iterator[PostgresServer]:
    """Yield a ready server; remove the container afterwards."""
    if host := os.environ.get("ORQENTIS_TEST_POSTGRES_HOST"):
        yield PostgresServer(
            host,
            int(os.environ.get("ORQENTIS_TEST_POSTGRES_PORT", "5432")),
            os.environ.get("ORQENTIS_TEST_POSTGRES_USER", "postgres"),
        )
        return
    if not docker_available():
        skip_or_fail("PostgreSQL contract tests need Docker or ORQENTIS_TEST_POSTGRES_HOST")
    name = f"orqentis-test-pg-{secrets.token_hex(4)}"
    _docker(
        "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", POSTGRES_IMAGE,
        "-c", "fsync=off", "-c", "max_connections=300",
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 90
        while True:
            ready = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "postgres", "-h", "127.0.0.1"],
                capture_output=True,
                check=False,
            )
            if ready.returncode == 0:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("PostgreSQL did not become ready")
            time.sleep(0.5)
        time.sleep(1.0)  # the entrypoint restarts the server once after initialisation
        yield PostgresServer("127.0.0.1", _published_port(name, 5432))
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def start_azurite() -> Iterator[AzuriteServer]:
    if url := os.environ.get("ORQENTIS_TEST_AZURITE_URL"):
        yield AzuriteServer(url, os.environ["ORQENTIS_TEST_AZURITE_ACCOUNT"], os.environ["ORQENTIS_TEST_AZURITE_KEY"])
        return
    if not docker_available():
        skip_or_fail("Blob contract tests need Docker or ORQENTIS_TEST_AZURITE_URL")
    account = "devstore"
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    name = f"orqentis-test-azurite-{secrets.token_hex(4)}"
    _docker(
        "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::10000",
        "-e", f"AZURITE_ACCOUNTS={account}:{key}", AZURITE_IMAGE,
        "azurite-blob", "--blobHost", "0.0.0.0", "--skipApiVersionCheck", "--loose", "--silent",
    )  # fmt: skip
    try:
        port = _published_port(name, 10000)
        deadline = time.monotonic() + 60
        while True:
            try:
                import socket

                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Azurite did not become ready") from None
                time.sleep(0.3)
        time.sleep(0.5)
        yield AzuriteServer(f"http://127.0.0.1:{port}/{account}", account, key)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
