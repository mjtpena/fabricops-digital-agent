"""Nothing a contracts command prints can drive the terminal: control characters are stripped on the way out."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from orqentis.cli import main
from orqentis.contracts.commands import _say

ESC, BEL, CSI = "\x1b", "\x07", "\x9b"


def render(text: str) -> str:
    stream = io.StringIO()
    _say(Console(file=stream, width=200, force_terminal=False, highlight=False), text)
    return stream.getvalue()


def test_say_strips_escape_bell_and_c1_control_characters_but_keeps_layout() -> None:
    out = render(f"ok{ESC}[31m red{ESC}]0;title{BEL} bell{CSI}2J end\ttab\nnext")
    # Rich expands the tab; what matters is that the printable text survives and the controls do not.
    assert out.split() == ["ok[31m", "red]0;title", "bell2J", "end", "tab", "next"]
    assert out.endswith("\n") and "\n" in out.rstrip("\n")  # the line break is kept
    assert not any(c in out for c in (ESC, BEL, CSI))


def test_say_prints_markup_like_text_literally() -> None:
    assert render("[bold red]not markup[/]") == "[bold red]not markup[/]\n"


def test_validate_output_never_carries_terminal_escapes_from_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    edge = {"tenant_id": "t", "source_item_id": "a", "target_item_id": "b", "dependency_type": "TRIGGERS"}
    hostile = f"d{ESC}[31m1{BEL}"
    document = {"dependencies": [{"id": hostile, **edge}, {"id": hostile, **edge, "target_item_id": "c"}]}
    path = tmp_path / "hostile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert main(["contracts", "validate", str(path)]) == 1
    out = capsys.readouterr().out
    assert "duplicate id" in out  # the message was printed, with the id echoed, ...
    assert ESC not in out and BEL not in out  # ... minus the control characters
