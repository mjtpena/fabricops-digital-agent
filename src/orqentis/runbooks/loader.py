"""Parse Markdown runbooks with YAML front matter into validated :class:`Runbook` objects."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from orqentis.domain.runbooks import REQUIRED_RUNBOOK_SECTIONS, Runbook

_FRONT_MATTER = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
_SECTION = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


class RunbookValidationError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("Invalid runbooks:\n  - " + "\n  - ".join(problems))
        self.problems = problems


def parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(_SECTION.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group("title")] = body[match.end() : end].strip()
    return sections


def parse_runbook(text: str, *, relative_path: str) -> Runbook:
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise ValueError(f"{relative_path}: missing YAML front matter")
    front: Any = yaml.safe_load(match.group("yaml"))
    if not isinstance(front, dict):
        raise ValueError(f"{relative_path}: front matter must be a mapping")
    sections = parse_sections(match.group("body"))
    missing = [s for s in REQUIRED_RUNBOOK_SECTIONS if not sections.get(s)]
    if missing:
        raise ValueError(f"{relative_path}: missing or empty sections: {', '.join(missing)}")
    for signal in front.get("signals", []):
        try:
            re.compile(str(signal))
        except re.error as exc:
            raise ValueError(f"{relative_path}: invalid signal pattern {signal!r}: {exc}") from exc
    try:
        return Runbook.model_validate(
            {
                **front,
                "version": str(front.get("version", "")),
                "sections": sections,
                "path": relative_path,
                "content_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        )
    except ValidationError as exc:
        raise ValueError(f"{relative_path}: {exc}") from exc


def load_runbooks(root: Path) -> list[Runbook]:
    problems: list[str] = []
    runbooks: list[Runbook] = []
    for path in sorted(root.rglob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        relative = path.relative_to(root).as_posix()
        try:
            runbooks.append(parse_runbook(path.read_text(encoding="utf-8"), relative_path=relative))
        except ValueError as exc:
            problems.append(str(exc))
    ids = [r.id for r in runbooks]
    problems.extend(f"duplicate runbook id {rid}" for rid in sorted({i for i in ids if ids.count(i) > 1}))
    if problems:
        raise RunbookValidationError(problems)
    return runbooks
