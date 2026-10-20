"""Map JSON pointers to line and column in a JSON document, so violations can be reported as ``file:line:col``.

``json.loads`` throws positions away. This is a small scanner over *already syntactically valid* JSON that
records where every value starts and notices repeated object keys (which ``json.loads`` silently collapses).
"""

from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field

_MAX_DEPTH = 64
_WHITESPACE = " \t\r\n"


@dataclass
class Locations:
    """Positions (1-based line and column) of values by JSON pointer, plus repeated keys."""

    positions: dict[str, tuple[int, int]] = field(default_factory=dict)
    duplicate_keys: list[tuple[str, str, int, int]] = field(default_factory=list)  # (object pointer, key, line, col)

    def find(self, pointer: str) -> tuple[int, int] | None:
        """Position of ``pointer``, or of its nearest enclosing value when the pointer itself is absent."""
        while True:
            if (position := self.positions.get(pointer)) is not None:
                return position
            if not pointer:
                return None
            pointer = pointer.rsplit("/", 1)[0]


def escape_pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_for(loc: tuple[str | int, ...]) -> str:
    return "".join(f"/{escape_pointer_token(str(part))}" for part in loc)


class _Scanner:
    def __init__(self, text: str) -> None:
        self._text = text
        self._line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
        self._i = 0
        self.result = Locations()

    def _position(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self._line_starts, offset)
        return line, offset - self._line_starts[line - 1] + 1

    def _skip(self) -> None:
        while self._i < len(self._text) and self._text[self._i] in _WHITESPACE:
            self._i += 1

    def _string(self) -> str:
        start = self._i
        self._i += 1  # opening quote
        while self._text[self._i] != '"':
            self._i += 2 if self._text[self._i] == "\\" else 1
        self._i += 1
        decoded = json.loads(self._text[start : self._i])
        assert isinstance(decoded, str)
        return decoded

    def scan(self) -> Locations:
        self._skip()
        self._value("", 0)
        return self.result

    def _value(self, pointer: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise ValueError("document is nested too deeply")
        self._skip()
        self.result.positions[pointer] = self._position(self._i)
        ch = self._text[self._i]
        if ch == "{":
            self._object(pointer, depth)
        elif ch == "[":
            self._array(pointer, depth)
        elif ch == '"':
            self._string()
        else:  # number, true, false, null
            while self._i < len(self._text) and self._text[self._i] not in ",]} \t\r\n":
                self._i += 1

    def _object(self, pointer: str, depth: int) -> None:
        self._i += 1
        seen: set[str] = set()
        self._skip()
        if self._text[self._i] == "}":
            self._i += 1
            return
        while True:
            self._skip()
            key_position = self._position(self._i)
            key = self._string()
            if key in seen:
                self.result.duplicate_keys.append((pointer, key, *key_position))
            seen.add(key)
            self._skip()
            self._i += 1  # colon
            self._value(f"{pointer}/{escape_pointer_token(key)}", depth + 1)
            self._skip()
            if self._text[self._i] == ",":
                self._i += 1
                continue
            self._i += 1  # closing brace
            return

    def _array(self, pointer: str, depth: int) -> None:
        self._i += 1
        self._skip()
        if self._text[self._i] == "]":
            self._i += 1
            return
        index = 0
        while True:
            self._value(f"{pointer}/{index}", depth + 1)
            index += 1
            self._skip()
            if self._text[self._i] == ",":
                self._i += 1
                continue
            self._i += 1  # closing bracket
            return


def locate(text: str) -> Locations:
    """Positions of every value in ``text``, which must already be valid JSON."""
    try:
        return _Scanner(text).scan()
    except (ValueError, IndexError, RecursionError):
        return Locations()  # positions are a convenience: never let them break validation
