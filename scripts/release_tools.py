#!/usr/bin/env python3
"""Release helpers for .github/workflows/release.yml, kept in a script so that they can be tested.

    python scripts/release_tools.py notes 1.2.3                       # CHANGELOG.md section -> stdout
    python scripts/release_tools.py fetch 123456789 --repo o/r --dest release-bundle

``notes`` prints the body of the ``## [1.2.3]`` section of CHANGELOG.md and fails if it is missing or empty.
``fetch`` downloads the release evidence of a CD run (image digest, SBOM, scan report) into ``--dest`` and checks it.

Each artifact is downloaded with its own ``gh run download --name <artifact>`` call. With several ``--name`` flags in
one call the GitHub CLI puts every artifact in a sub-directory named after it (``dest/sbom/sbom.spdx.json``); with a
single name it extracts the files straight into ``--dest``. The workflow depends on the second layout, hence one call
each.
Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

# artifact name -> the file it holds
EVIDENCE = {
    "image-digest": "image-digest.txt",
    "sbom": "sbom.spdx.json",
    "trivy-report": "trivy-report.txt",
}
IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9.\-/_:]+@sha256:[0-9a-f]{64}$")

Runner = Callable[[Sequence[str]], None]


class ReleaseError(Exception):
    """A release precondition failed; the message is shown to the person who pushed the tag."""


def changelog_notes(changelog: str, version: str) -> str:
    """The body of the ``## [version]`` section: up to the next section or the link-reference footer."""
    lines = changelog.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## [") or re.match(r"^\[[^\]]+\]:\s", line):
            break
        body.append(line)
    notes = "\n".join(body).strip()
    if not notes:
        raise ReleaseError(f"The '## [{version}]' section of CHANGELOG.md is empty")
    return notes


def _gh(arguments: Sequence[str]) -> None:
    subprocess.run(["gh", *arguments], check=True)  # noqa: S603, S607 - fixed arguments, no shell


def fetch_evidence(run_id: str, repo: str, dest: Path, run: Runner = _gh) -> str:
    """Download the evidence artifacts of a CD run into ``dest`` (flat) and return the image reference."""
    dest.mkdir(parents=True, exist_ok=True)
    for name in EVIDENCE:
        run(["run", "download", run_id, "--repo", repo, "--name", name, "--dir", str(dest)])
    missing = [filename for filename in EVIDENCE.values() if not (dest / filename).is_file()]
    if missing:
        raise ReleaseError(f"CD run {run_id} did not produce: {', '.join(missing)}")
    image = (dest / EVIDENCE["image-digest"]).read_text(encoding="utf-8").strip()
    if not IMAGE_REFERENCE.match(image):
        raise ReleaseError(f"{EVIDENCE['image-digest']} does not hold an image digest reference: {image!r}")
    return image


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    notes = commands.add_parser("notes", help="print the CHANGELOG.md section for a version")
    notes.add_argument("version")
    notes.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    fetch = commands.add_parser("fetch", help="download the evidence artifacts of a CD run")
    fetch.add_argument("run_id")
    fetch.add_argument("--repo", required=True)
    fetch.add_argument("--dest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "notes":
            print(changelog_notes(args.changelog.read_text(encoding="utf-8"), args.version))
        else:
            print(fetch_evidence(args.run_id, args.repo, args.dest))
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
