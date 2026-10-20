"""A log call whose ``extra`` names a LogRecord attribute raises ``KeyError`` as soon as that level is enabled.

Reproduced: the PostgreSQL migration runner logged ``extra={"name": ...}``. Nothing noticed while the logger was at
WARNING (the call is skipped), and the first migration of a deployed environment, which logs at INFO, would have crashed
the start-up. This scans every ``extra={...}`` literal in the source.
"""

from __future__ import annotations

import ast
import logging

from tests.conftest import REPO

RESERVED = set(logging.LogRecord("n", logging.INFO, "f", 1, "m", (), None).__dict__) | {"message", "asctime"}


def test_no_log_call_uses_a_reserved_logrecord_attribute_as_an_extra_key() -> None:
    offenders: list[str] = []
    for path in sorted((REPO / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    offenders += [
                        f"{path.relative_to(REPO)}:{node.lineno}: {key.value!r}"
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and key.value in RESERVED
                    ]
    assert offenders == []
