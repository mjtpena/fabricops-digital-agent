"""With ``ORQENTIS_REQUIRE_SERVICES=1`` (CI) a missing PostgreSQL or Azurite fails the tests; it never skips them."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import REPO


def run_inner(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # No docker on PATH and no running server named: the service fixtures have nothing to start or use.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ORQENTIS_", "PYTEST_"))}
    env["PATH"] = str(Path(sys.executable).parent)
    env.update(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/contract/test_event_store_contract.py",
            "-k",
            "postgres",
            "-x",
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
            "-rs",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_a_missing_service_fails_the_contract_tests_when_services_are_required() -> None:
    required = run_inner({"ORQENTIS_REQUIRE_SERVICES": "1"})
    assert required.returncode == 1, required.stdout + required.stderr
    assert "ORQENTIS_REQUIRE_SERVICES=1" in required.stdout
    assert "skipped" not in required.stdout.lower().splitlines()[-1]


def test_a_missing_service_skips_them_locally_without_the_flag() -> None:
    optional = run_inner({})
    assert optional.returncode == 0, optional.stdout + optional.stderr
    assert "skipped" in optional.stdout.lower()
