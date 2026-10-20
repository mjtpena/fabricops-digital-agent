"""Release helpers (scripts/release_tools.py): changelog notes and the layout of the downloaded evidence."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64
IMAGE = f"crfabricopsshared.azurecr.io/orqentis-fabricops@sha256:{DIGEST}"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts" / "release_tools.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tools = _load()

CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Something not released yet.

## [1.2.3] - 2026-11-01

### Fixed

- A thing.

## [1.2.2] - 2026-10-01

- Older.

[Unreleased]: https://example.invalid/compare/v1.2.3...HEAD
[1.2.3]: https://example.invalid/compare/v1.2.2...v1.2.3
"""


class FakeGh:
    """Stands in for `gh run download`, reproducing the CLI's real layout rules.

    With a single --name the files are extracted straight into --dir. With several --name flags in one call each
    artifact gets its own sub-directory. The release workflow reads `release-bundle/image-digest.txt`, so only
    one-artifact-per-call produces a layout it can use.
    """

    CONTENT = {
        "image-digest": ("image-digest.txt", f"{IMAGE}\n"),
        "sbom": ("sbom.spdx.json", "{}"),
        "trivy-report": ("trivy-report.txt", "no findings"),
    }

    def __init__(self, missing: Sequence[str] = (), digest_text: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.missing = set(missing)
        self.digest_text = digest_text

    def __call__(self, arguments: Sequence[str]) -> None:
        arguments = list(arguments)
        self.calls.append(arguments)
        names = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--name"]
        target = Path(arguments[arguments.index("--dir") + 1])
        for name in names:
            if name in self.missing:
                continue
            filename, text = self.CONTENT[name]
            if name == "image-digest" and self.digest_text is not None:
                text = self.digest_text
            folder = target / name if len(names) > 1 else target
            folder.mkdir(parents=True, exist_ok=True)
            (folder / filename).write_text(text, encoding="utf-8")


def test_each_artifact_is_downloaded_with_its_own_call(tmp_path: Path) -> None:
    gh = FakeGh()
    image = tools.fetch_evidence("42", "o/r", tmp_path / "release-bundle", run=gh)
    assert image == IMAGE
    assert len(gh.calls) == 3
    assert all(call.count("--name") == 1 for call in gh.calls)
    bundle = tmp_path / "release-bundle"
    # The paths release.yml reads and attaches.
    assert (bundle / "image-digest.txt").read_text(encoding="utf-8").strip() == IMAGE
    assert (bundle / "sbom.spdx.json").is_file()
    assert (bundle / "trivy-report.txt").is_file()


def test_a_single_multi_name_download_would_not_produce_the_layout(tmp_path: Path) -> None:
    """Regression guard: this is the call the workflow used to make, and what the CLI does with it."""
    gh = FakeGh()
    gh(["run", "download", "42", "--name", "image-digest", "--name", "sbom", "--dir", str(tmp_path)])
    assert not (tmp_path / "image-digest.txt").exists()
    assert (tmp_path / "image-digest" / "image-digest.txt").is_file()


def test_missing_artifact_is_reported(tmp_path: Path) -> None:
    with pytest.raises(tools.ReleaseError, match=r"sbom\.spdx\.json"):
        tools.fetch_evidence("42", "o/r", tmp_path, run=FakeGh(missing=["sbom"]))


@pytest.mark.parametrize("text", ["not-a-digest\n", "registry/orqentis-fabricops:latest\n", "\n"])
def test_a_digest_file_without_a_digest_reference_is_rejected(tmp_path: Path, text: str) -> None:
    with pytest.raises(tools.ReleaseError, match="digest reference"):
        tools.fetch_evidence("42", "o/r", tmp_path, run=FakeGh(digest_text=text))


def test_changelog_notes_return_only_the_requested_section() -> None:
    notes = tools.changelog_notes(CHANGELOG, "1.2.3")
    assert notes.startswith("### Fixed")
    assert "A thing." in notes
    assert "Older." not in notes
    assert "Something not released yet." not in notes
    assert "example.invalid" not in notes, "the link-reference footer is not part of the notes"


def test_changelog_notes_stop_at_the_footer_for_the_last_section() -> None:
    notes = tools.changelog_notes(CHANGELOG.replace("## [1.2.2] - 2026-10-01\n\n- Older.\n\n", ""), "1.2.3")
    assert notes.endswith("- A thing.")


def test_changelog_without_the_version_is_rejected() -> None:
    with pytest.raises(tools.ReleaseError, match=r"no '## \[9.9.9\]' section"):
        tools.changelog_notes(CHANGELOG, "9.9.9")


def test_empty_changelog_section_is_rejected() -> None:
    with pytest.raises(tools.ReleaseError, match="empty"):
        tools.changelog_notes("## [1.0.0] - 2026-01-01\n\n## [0.9.0]\n- x\n", "1.0.0")


def test_version_is_matched_literally_not_as_a_pattern() -> None:
    with pytest.raises(tools.ReleaseError):
        tools.changelog_notes("## [1x2y3]\n- x\n", "1.2.3")


def test_command_line_prints_notes_and_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert tools.main(["notes", "1.2.3", "--changelog", str(changelog)]) == 0
    assert "A thing." in capsys.readouterr().out
    assert tools.main(["notes", "9.9.9", "--changelog", str(changelog)]) == 1
    assert "::error::" in capsys.readouterr().err
