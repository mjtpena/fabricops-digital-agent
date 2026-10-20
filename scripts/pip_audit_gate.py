#!/usr/bin/env python3
"""Dependency vulnerability gate: run pip-audit and fail on anything not on the reviewed allowlist.

    python scripts/pip_audit_gate.py --path <site-packages of the environment to audit>

pip-audit has no expiry for ignored vulnerabilities, so the acceptance policy lives here. Every accepted
vulnerability is an entry in ``.pip-audit-allowlist.toml`` with a justification, an owner and an expiry date
(at most ``MAX_EXPIRY_DAYS`` ahead). An expired or incomplete entry does not count: the vulnerability is
reported as unaccepted and the gate fails. An entry that matches nothing is reported as stale.

Standard library only, so it runs on a bare CI runner. ``--report`` evaluates a saved ``pip-audit --format json``
file instead of running pip-audit (used by the tests).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_EXPIRY_DAYS = 90
REQUIRED_FIELDS = ("id", "package", "justification", "owner", "expires")
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent.parent / ".pip-audit-allowlist.toml"


@dataclass(frozen=True)
class Finding:
    package: str
    version: str
    vuln_id: str
    aliases: tuple[str, ...]
    fix_versions: tuple[str, ...]

    @property
    def ids(self) -> frozenset[str]:
        return frozenset({self.vuln_id, *self.aliases})


@dataclass(frozen=True)
class AllowEntry:
    vuln_id: str
    package: str
    justification: str
    owner: str
    expires: dt.date


@dataclass
class Evaluation:
    unaccepted: list[Finding] = field(default_factory=list)
    accepted: list[tuple[Finding, AllowEntry]] = field(default_factory=list)
    stale: list[AllowEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.unaccepted and not self.errors


def normalise(name: str) -> str:
    """PEP 503 name normalisation, so ``Foo_Bar`` and ``foo-bar`` compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def load_allowlist(path: Path, today: dt.date) -> tuple[list[AllowEntry], list[str]]:
    """Parse and validate the allowlist. Invalid entries are errors; expired entries are errors and not applied."""
    if not path.exists():
        return [], []
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    unknown_tables = sorted(set(data) - {"ignore"})
    errors = [
        f"{path.name}: unknown top-level key {key!r} (only [[ignore]] entries are allowed)" for key in unknown_tables
    ]
    entries: list[AllowEntry] = []
    for index, raw in enumerate(data.get("ignore", []), start=1):
        label = f"{path.name} entry {index} ({raw.get('id', '?')})"
        missing = [name for name in REQUIRED_FIELDS if not str(raw.get(name, "")).strip()]
        if missing:
            errors.append(f"{label}: missing {', '.join(missing)}")
            continue
        extra = sorted(set(raw) - set(REQUIRED_FIELDS))
        if extra:
            errors.append(f"{label}: unknown field(s) {', '.join(extra)}")
            continue
        expires = raw["expires"]
        if not isinstance(expires, dt.date) or isinstance(expires, dt.datetime):
            errors.append(f"{label}: expires must be a TOML date such as 2026-12-31")
            continue
        if expires < today:
            errors.append(f"{label}: expired on {expires}; renew it with a fresh justification or remove it")
            continue
        if expires > today + dt.timedelta(days=MAX_EXPIRY_DAYS):
            errors.append(f"{label}: expires {expires}, more than {MAX_EXPIRY_DAYS} days ahead")
            continue
        entries.append(
            AllowEntry(
                vuln_id=str(raw["id"]).strip(),
                package=normalise(str(raw["package"])),
                justification=str(raw["justification"]).strip(),
                owner=str(raw["owner"]).strip(),
                expires=expires,
            )
        )
    return entries, errors


def parse_report(report: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    for dependency in report.get("dependencies", []):
        for vuln in dependency.get("vulns", []):
            findings.append(
                Finding(
                    package=normalise(dependency["name"]),
                    version=str(dependency.get("version", "?")),
                    vuln_id=vuln["id"],
                    aliases=tuple(vuln.get("aliases", [])),
                    fix_versions=tuple(vuln.get("fix_versions", [])),
                )
            )
    return findings


def evaluate(report: dict[str, Any], allowlist: Path, today: dt.date) -> Evaluation:
    entries, errors = load_allowlist(allowlist, today)
    result = Evaluation(errors=errors)
    used: set[AllowEntry] = set()
    for finding in parse_report(report):
        match = next((e for e in entries if e.package == finding.package and e.vuln_id in finding.ids), None)
        if match is None:
            result.unaccepted.append(finding)
        else:
            used.add(match)
            result.accepted.append((finding, match))
    result.stale = [entry for entry in entries if entry not in used]
    return result


def run_pip_audit(extra_args: list[str]) -> dict[str, Any]:
    command = [sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off", *extra_args]
    # pip-audit exits 1 when it finds vulnerabilities; the gate decides from the JSON, so only a real failure
    # (no parseable report) is an error here.
    completed = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    try:
        report: dict[str, Any] = json.loads(completed.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"pip-audit did not produce a JSON report (exit code {completed.returncode})") from None
    return report


def render(result: Evaluation, allowlist: Path) -> str:
    lines: list[str] = []
    if result.unaccepted:
        lines.append("Unaccepted vulnerabilities:")
        for f in result.unaccepted:
            fix = ", ".join(f.fix_versions) or "no fix released"
            lines.append(
                f"  - {f.package} {f.version}: {f.vuln_id} (fix: {fix}) [{', '.join(f.aliases) or 'no aliases'}]"
            )
    if result.accepted:
        lines.append("Accepted (reviewed allowlist):")
        for f, e in result.accepted:
            lines.append(
                f"  - {f.package} {f.version}: {f.vuln_id} until {e.expires}, owner {e.owner}: {e.justification}"
            )
    for entry in result.stale:
        lines.append(f"Stale allowlist entry (matches no current vulnerability): {entry.vuln_id} for {entry.package}")
    for error in result.errors:
        lines.append(f"Allowlist error: {error}")
    if not lines:
        lines.append("No known vulnerabilities.")
    lines.append(f"Allowlist: {allowlist}")
    lines.append("Result: " + ("PASS" if result.passed else "FAIL"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument(
        "--report", type=Path, help="evaluate this saved pip-audit JSON report instead of running pip-audit"
    )
    parser.add_argument("--today", type=dt.date.fromisoformat, default=None, help="override today's date (YYYY-MM-DD)")
    args, pip_audit_args = parser.parse_known_args(argv)
    today = args.today or dt.datetime.now(dt.UTC).date()

    if args.report:
        report: dict[str, Any] = json.loads(args.report.read_text(encoding="utf-8"))
    else:
        report = run_pip_audit(pip_audit_args)

    result = evaluate(report, args.allowlist, today)
    text = render(result, args.allowlist)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write("### pip-audit\n\n```text\n" + text + "\n```\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
