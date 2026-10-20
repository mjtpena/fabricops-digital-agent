"""The dependency vulnerability gate (scripts/pip_audit_gate.py): allowlist rules, expiry and matching."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
TODAY = dt.date(2026, 10, 20)


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pip_audit_gate", ROOT / "scripts" / "pip_audit_gate.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


def _report(*vulns: tuple[str, str, str, list[str]]) -> dict[str, Any]:
    """(package, version, vuln id, aliases) -> a pip-audit JSON report."""
    return {
        "dependencies": [
            {
                "name": name,
                "version": version,
                "vulns": [{"id": vuln_id, "aliases": aliases, "fix_versions": ["9.9.9"], "description": "x"}],
            }
            for name, version, vuln_id, aliases in vulns
        ]
        + [{"name": "clean-package", "version": "1.0", "vulns": []}]
    }


def _allowlist(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "allowlist.toml"
    path.write_text(body, encoding="utf-8")
    return path


ENTRY = """
[[ignore]]
id = "GHSA-aaaa-bbbb-cccc"
package = "Some_Package"
justification = "The vulnerable parser is not reachable: FabricOps never parses untrusted archives."
owner = "mjtpena"
expires = {expires}
"""


def test_no_vulnerabilities_passes(tmp_path: Path) -> None:
    result = gate.evaluate({"dependencies": []}, tmp_path / "missing.toml", TODAY)
    assert result.passed
    assert not result.unaccepted


def test_unlisted_vulnerability_fails(tmp_path: Path) -> None:
    report = _report(("some-package", "1.0", "GHSA-aaaa-bbbb-cccc", ["CVE-2026-0001"]))
    result = gate.evaluate(report, tmp_path / "missing.toml", TODAY)
    assert not result.passed
    assert [f.vuln_id for f in result.unaccepted] == ["GHSA-aaaa-bbbb-cccc"]


def test_allowlisted_vulnerability_passes_and_is_reported(tmp_path: Path) -> None:
    allowlist = _allowlist(tmp_path, ENTRY.format(expires="2026-12-01"))
    report = _report(("some-package", "1.0", "GHSA-aaaa-bbbb-cccc", []))
    result = gate.evaluate(report, allowlist, TODAY)
    assert result.passed
    assert len(result.accepted) == 1
    assert "until 2026-12-01" in gate.render(result, allowlist)


def test_allowlist_matches_by_alias_and_normalised_package_name(tmp_path: Path) -> None:
    body = ENTRY.format(expires="2026-12-01").replace("GHSA-aaaa-bbbb-cccc", "CVE-2026-0001")
    report = _report(("some-package", "1.0", "GHSA-aaaa-bbbb-cccc", ["CVE-2026-0001"]))
    assert gate.evaluate(report, _allowlist(tmp_path, body), TODAY).passed


def test_allowlist_is_scoped_to_the_named_package(tmp_path: Path) -> None:
    allowlist = _allowlist(tmp_path, ENTRY.format(expires="2026-12-01"))
    report = _report(("other-package", "1.0", "GHSA-aaaa-bbbb-cccc", []))
    result = gate.evaluate(report, allowlist, TODAY)
    assert not result.passed
    assert len(result.stale) == 1


def test_expired_entry_is_an_error_and_does_not_accept_the_vulnerability(tmp_path: Path) -> None:
    allowlist = _allowlist(tmp_path, ENTRY.format(expires="2026-10-19"))
    report = _report(("some-package", "1.0", "GHSA-aaaa-bbbb-cccc", []))
    result = gate.evaluate(report, allowlist, TODAY)
    assert not result.passed
    assert result.unaccepted
    assert any("expired" in error for error in result.errors)


def test_expiry_beyond_the_review_horizon_is_rejected(tmp_path: Path) -> None:
    too_far = (TODAY + dt.timedelta(days=gate.MAX_EXPIRY_DAYS + 1)).isoformat()
    result = gate.evaluate({"dependencies": []}, _allowlist(tmp_path, ENTRY.format(expires=too_far)), TODAY)
    assert not result.passed
    assert any("days ahead" in error for error in result.errors)


@pytest.mark.parametrize("missing", ["justification", "owner", "expires", "package", "id"])
def test_every_field_is_required(tmp_path: Path, missing: str) -> None:
    body = "\n".join(
        line for line in ENTRY.format(expires="2026-12-01").splitlines() if not line.startswith(f"{missing} =")
    )
    result = gate.evaluate({"dependencies": []}, _allowlist(tmp_path, body), TODAY)
    assert not result.passed
    assert any(f"missing {missing}" in error for error in result.errors)


def test_blank_justification_is_rejected(tmp_path: Path) -> None:
    body = ENTRY.format(expires="2026-12-01").replace(
        "The vulnerable parser is not reachable: FabricOps never parses untrusted archives.", "  "
    )
    result = gate.evaluate({"dependencies": []}, _allowlist(tmp_path, body), TODAY)
    assert any("missing justification" in error for error in result.errors)


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    body = ENTRY.format(expires="2026-12-01") + 'ticket = "OPS-1"\n'
    result = gate.evaluate({"dependencies": []}, _allowlist(tmp_path, body), TODAY)
    assert any("unknown field" in error for error in result.errors)


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report_file = tmp_path / "report.json"
    report_file.write_text(json.dumps(_report(("some-package", "1.0", "GHSA-aaaa-bbbb-cccc", []))), encoding="utf-8")
    missing = str(tmp_path / "missing.toml")
    assert gate.main(["--report", str(report_file), "--allowlist", missing, "--today", "2026-10-20"]) == 1
    assert "Result: FAIL" in capsys.readouterr().out

    allowlist = _allowlist(tmp_path, ENTRY.format(expires="2026-12-01"))
    assert gate.main(["--report", str(report_file), "--allowlist", str(allowlist), "--today", "2026-10-20"]) == 0
    assert "Result: PASS" in capsys.readouterr().out


def test_repository_allowlist_is_valid() -> None:
    """The committed allowlist parses and holds no expired or malformed entry."""
    result = gate.evaluate({"dependencies": []}, gate.DEFAULT_ALLOWLIST, dt.datetime.now(dt.UTC).date())
    assert result.errors == []
