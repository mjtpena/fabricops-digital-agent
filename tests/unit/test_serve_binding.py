"""`orqentis serve` fails closed: self-asserted identities (dev headers) are not served on a public address."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from orqentis.cli import main
from orqentis.config.settings import ApiAuthMode
from tests.conftest import make_settings

pytestmark = pytest.mark.security


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "LOCALHOST", "127.5.4.3"])
def test_loopback_hosts_may_serve_dev_headers(tmp_path: Path, host: str) -> None:
    make_settings(tmp_path).check_bind_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.4", "example.internal", ""])  # noqa: S104
def test_public_hosts_may_not_serve_dev_headers(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError, match="dev_headers"):
        make_settings(tmp_path).check_bind_host(host)


def test_the_explicit_opt_in_and_easy_auth_allow_a_public_bind(tmp_path: Path) -> None:
    make_settings(tmp_path, dev_headers_allow_remote=True).check_bind_host("0.0.0.0")  # noqa: S104
    make_settings(tmp_path, api_auth_mode=ApiAuthMode.EASY_AUTH).check_bind_host("0.0.0.0")  # noqa: S104


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict[str, Any]]:
    """Run `orqentis serve` without a real server; records what would have been served."""
    monkeypatch.chdir(tmp_path)  # no stray .env, and the data directory stays out of the repository
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    return calls


def test_serve_refuses_dev_headers_on_a_public_address_by_default(
    served: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: with no environment set, `serve --host 0.0.0.0` let any caller choose its own roles."""
    assert main(["serve", "--host", "0.0.0.0"]) == 2  # noqa: S104
    assert served == []
    assert "dev_headers" in capsys.readouterr().err


def test_serve_keeps_working_on_loopback(served: list[dict[str, Any]]) -> None:
    assert main(["serve"]) == 0
    assert main(["serve", "--host", "127.0.0.1", "--port", "9000"]) == 0
    assert [c["host"] for c in served] == ["127.0.0.1", "127.0.0.1"]


def test_serve_on_a_public_address_needs_the_explicit_opt_in_or_easy_auth(
    served: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ORQENTIS_DEV_HEADERS_ALLOW_REMOTE", "true")
    assert main(["serve", "--host", "0.0.0.0"]) == 0  # noqa: S104
    monkeypatch.delenv("ORQENTIS_DEV_HEADERS_ALLOW_REMOTE")
    monkeypatch.setenv("ORQENTIS_API_AUTH_MODE", "easy_auth")
    assert main(["serve", "--host", "0.0.0.0"]) == 0  # noqa: S104
    assert len(served) == 2
