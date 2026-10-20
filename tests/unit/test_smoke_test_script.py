"""scripts/smoke-test.sh against a local stand-in for the Container Apps front door and a fake `az`.

The script is bash and curl; these tests run it as a subprocess (Git Bash on Windows, any Linux bash in CI).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import stat
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "smoke-test.sh"
TOKEN = "good-token"  # a test value the stub accepts
EXPECTED_IMAGE = "crfabricopsshared.azurecr.io/orqentis-fabricops@sha256:" + "b" * 64

# A fake `az`: answers the three calls the script makes from FAKE_* variables.
FAKE_AZ = r"""#!/usr/bin/env bash
case "$*" in
  *"get-access-token"*) echo "good-token" ;;
  *"--ids"*) printf '%s\n%s\n%s\n' "$FAKE_IMAGE" "$FAKE_TRAFFIC" "$FAKE_STATE" ;;
  *"resource show"*) printf '%s\n%s\n%s\n' "$FAKE_APP_ID" "$FAKE_LATEST" "$FAKE_READY" ;;
  *) echo "unexpected az call: $*" >&2; exit 1 ;;
esac
"""


def _handler(mode: str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, code: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            authed = self.headers.get("Authorization") == f"Bearer {TOKEN}"
            if self.path == "/health":
                self._send(401 if mode == "health_needs_auth" and not authed else 200, {"status": "ok"})
            elif self.path == "/incidents":
                if mode == "anonymous_open":
                    self._send(200, [])
                elif not authed:
                    self._send(401, {})
                elif mode == "forbidden":
                    self._send(403, {})
                elif mode == "not_a_list":
                    self._send(200, {"items": []})
                else:
                    self._send(200, [{"id": "INC-1"}])
            else:
                self._send(404, {})

    return Handler


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[HTTPServer] = []

    def start(mode: str) -> str:
        server = HTTPServer(("127.0.0.1", 0), _handler(mode))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_az(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    az = directory / "az"
    az.write_text(FAKE_AZ, encoding="utf-8", newline="\n")
    az.chmod(az.stat().st_mode | stat.S_IEXEC)
    return directory


def _run(base_url: str, fake_az: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash, "bash is required to run the delivery scripts (Git Bash on Windows)"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith(("SMOKE_", "FAKE_"))},
        "PATH": f"{fake_az}{os.pathsep}{os.environ['PATH']}",
        "SMOKE_BASE_URL": base_url,
        "SMOKE_TOKEN": TOKEN,
        "SMOKE_RETRIES": "2",
        "SMOKE_RETRY_DELAY": "0",
        **overrides,
    }
    return subprocess.run([bash, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603


APP_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.App/containerApps/ca"
GOOD_REVISION = {
    "FAKE_APP_ID": APP_ID,
    "FAKE_LATEST": "ca--0000002",
    "FAKE_READY": "ca--0000002",
    "FAKE_IMAGE": EXPECTED_IMAGE,
    "FAKE_TRAFFIC": "100",
    "FAKE_STATE": "Running",
}
REVISION_CHECK = {
    "SMOKE_CONTAINER_APP": "ca",
    "SMOKE_RESOURCE_GROUP": "rg",
    "SMOKE_EXPECTED_IMAGE": EXPECTED_IMAGE,
}


def test_a_healthy_deployment_passes_every_check(serve: Any, fake_az: Path) -> None:
    result = _run(serve("ok"), fake_az, **REVISION_CHECK, **GOOD_REVISION)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS") == 4
    assert "runs the deployed image" in result.stdout
    assert TOKEN not in result.stdout + result.stderr


def test_without_a_revision_config_the_revision_check_is_skipped(serve: Any, fake_az: Path) -> None:
    result = _run(serve("ok"), fake_az)
    assert result.returncode == 0, result.stdout
    assert "revision check skipped" in result.stdout
    assert result.stdout.count("PASS") == 3


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"FAKE_READY": "ca--0000001"}, "the new revision is not ready: the previous one still answers"),
        ({"FAKE_IMAGE": EXPECTED_IMAGE.replace("b" * 64, "c" * 64)}, "the latest revision runs a different image"),
        ({"FAKE_TRAFFIC": "0"}, "the latest revision takes no traffic"),
        ({"FAKE_STATE": "Failed"}, "the latest revision is not running"),
        ({"FAKE_LATEST": "", "FAKE_READY": ""}, "no revision at all"),
    ],
)
def test_a_bad_revision_fails_even_though_the_http_checks_pass(
    serve: Any, fake_az: Path, override: dict[str, str], why: str
) -> None:
    result = _run(serve("ok"), fake_az, **REVISION_CHECK, **{**GOOD_REVISION, **override})
    assert result.returncode == 1, f"{why}\n{result.stdout}"
    assert "FAIL  revision" in result.stdout
    assert "PASS  GET /health -> 200" in result.stdout, "the HTTP checks still ran and passed"


def test_partial_revision_config_is_a_usage_error(serve: Any, fake_az: Path) -> None:
    result = _run(serve("ok"), fake_az, SMOKE_CONTAINER_APP="ca")
    assert result.returncode == 2
    assert "together" in result.stderr


def test_unauthenticated_access_to_incidents_fails_the_run(serve: Any, fake_az: Path) -> None:
    result = _run(serve("anonymous_open"), fake_az)
    assert result.returncode == 1
    assert "expected 401" in result.stdout


def test_a_rejected_token_fails_the_run(serve: Any, fake_az: Path) -> None:
    result = _run(serve("forbidden"), fake_az)
    assert result.returncode == 1
    assert "with the Automation token -> 403" in result.stdout


def test_a_non_list_body_fails_the_run(serve: Any, fake_az: Path) -> None:
    result = _run(serve("not_a_list"), fake_az)
    assert result.returncode == 1
    assert "not a JSON array" in result.stdout


def test_health_is_anonymous_by_default_and_authenticated_on_request(serve: Any, fake_az: Path) -> None:
    url = serve("health_needs_auth")
    default = _run(url, fake_az)
    assert default.returncode == 1
    assert "SMOKE_HEALTH_AUTHENTICATED=true" in default.stdout + default.stderr
    assert _run(url, fake_az, SMOKE_HEALTH_AUTHENTICATED="true").returncode == 0


def test_plain_http_is_refused_except_for_localhost(fake_az: Path) -> None:
    result = _run("http://example.com", fake_az)
    assert result.returncode == 2
    assert "https://" in result.stderr


def test_the_token_comes_from_az_when_none_is_supplied(serve: Any, fake_az: Path) -> None:
    result = _run(
        serve("ok"), fake_az, SMOKE_TOKEN="", SMOKE_TOKEN_RESOURCE="api://00000000-0000-0000-0000-000000000000"
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_token_never_reaches_the_output_on_a_ci_runner(serve: Any, fake_az: Path) -> None:
    """On GitHub Actions GITHUB_ACTIONS=true. The script once printed `::add-mask::<token>`: the token itself."""
    for token_source in ({"SMOKE_TOKEN": TOKEN}, {"SMOKE_TOKEN": "", "SMOKE_TOKEN_RESOURCE": "api://x"}):
        result = _run(
            serve("ok"),
            fake_az,
            GITHUB_ACTIONS="true",
            GITHUB_STEP_SUMMARY="",
            **REVISION_CHECK,
            **GOOD_REVISION,
            **token_source,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert TOKEN not in result.stdout + result.stderr
        assert "::add-mask::" not in result.stdout + result.stderr


def test_a_rejected_token_is_described_by_its_claims_but_never_printed(serve: Any, fake_az: Path) -> None:
    claims = (
        base64.urlsafe_b64encode(json.dumps({"aud": "api://x", "roles": ["FabricOps.Reader"]}).encode())
        .decode()
        .rstrip("=")
    )
    jwt = f"e30.{claims}.signature-part-that-must-not-leak"
    result = _run(serve("ok"), fake_az, GITHUB_ACTIONS="true", SMOKE_TOKEN=jwt)
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "roles=['FabricOps.Reader']" in output
    assert jwt not in output
    assert claims not in output, "the payload segment of the token is not printed either"
    assert "signature-part-that-must-not-leak" not in output


def test_the_script_cannot_trace_or_echo_the_token() -> None:
    """Static guard: no shell tracing, no verbose curl, no runner mask command, no token on a command line."""
    code = "\n".join(
        line for line in SCRIPT.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in (
        "set -x",
        "xtrace",
        "set -v",
        "curl -v",
        "--verbose",
        "--trace",
        "add-mask",
        'echo "$token"',
        "echo $token",
    ):
        assert forbidden not in code, f"smoke-test.sh must not contain {forbidden!r}"
    assert "Authorization: Bearer" in code
    assert "--config -" in code, "the bearer header is passed to curl on stdin, not as an argument"
