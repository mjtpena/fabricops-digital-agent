"""Parsing and diffing the schema history of a Delta table (pure functions; no I/O).

A table's ``_delta_log`` is a sequence of JSON commit files. A commit that changes the schema carries a ``metaData``
action whose ``schemaString`` is the full schema as JSON. Diffing consecutive schemas gives the columns that were added,
removed, renamed (only when Delta column mapping proves it) or whose type changed.

**Everything read here is untrusted**: table and column names, ``commitInfo`` fields and ``userMetadata`` are written by
whoever can write to the table. Names are stripped of control characters and envelope-like tags and clipped to a fixed
length; sizes (columns, lines, bytes) are bounded; a malformed line is skipped, never raised. The values are carried as
data and are later scanned for instruction-like content by the evidence recorder like every other Fabric string.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from orqentis.domain.delta import ColumnSpec, DeltaCommitFile, Rename, SchemaChange, TypeChange
from orqentis.tools.untrusted import sanitize_text

MAX_COLUMNS = 2000
MAX_NAME = 256
MAX_SCHEMA_STRING = 2 * 1024 * 1024
MAX_LINE = MAX_SCHEMA_STRING + 64 * 1024
MAX_TYPE_DEPTH = 8

_PHYSICAL_NAME = "delta.columnMapping.physicalName"
_COLUMN_ID = "delta.columnMapping.id"


def clip(text: object, limit: int) -> str:
    """Untrusted text made safe to store and show: control and envelope characters removed, at most ``limit`` long."""
    cleaned = sanitize_text(str(text), max_length=limit * 4).strip()
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def render_type(raw: Any, *, _depth: int = 0) -> str:
    """A Delta schema type as one compact string: ``string``, ``decimal(10,2)``, ``array<int>``, ``struct<a:int>``."""
    if _depth > MAX_TYPE_DEPTH:
        return "…"
    if isinstance(raw, str):
        return clip(raw, 64)
    if not isinstance(raw, dict):
        return "unknown"
    kind = raw.get("type")
    if kind == "array":
        return clip(f"array<{render_type(raw.get('elementType'), _depth=_depth + 1)}>", MAX_NAME)
    if kind == "map":
        key, value = raw.get("keyType"), raw.get("valueType")
        return clip(f"map<{render_type(key, _depth=_depth + 1)},{render_type(value, _depth=_depth + 1)}>", MAX_NAME)
    if kind == "struct":
        fields = raw.get("fields")
        inner = (
            ",".join(
                f"{clip(f.get('name', '?'), 64)}:{render_type(f.get('type'), _depth=_depth + 1)}"
                for f in (fields if isinstance(fields, list) else [])[:50]
                if isinstance(f, dict)
            )
            if isinstance(fields, list)
            else ""
        )
        return clip(f"struct<{inner}>", MAX_NAME)
    return clip(kind if isinstance(kind, str) else "unknown", 64)


@dataclass(frozen=True)
class SchemaSnapshot:
    """The top-level columns of a table at one version."""

    columns: tuple[ColumnSpec, ...]
    # logical name -> physical name, only for columns whose field metadata carries Delta column mapping
    physical: dict[str, str] = field(default_factory=dict)
    truncated: bool = False

    def by_name(self) -> dict[str, ColumnSpec]:
        return {c.name: c for c in self.columns}

    def same_as(self, other: SchemaSnapshot) -> bool:
        return self.columns == other.columns and self.physical == other.physical

    def to_payload(self) -> dict[str, Any]:
        """The snapshot as event payload fields (already bounded and sanitised by the parser)."""
        return {
            "columns": [c.model_dump() for c in self.columns],
            "physical": dict(self.physical),
            "columns_truncated": self.truncated,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> SchemaSnapshot | None:
        """The snapshot stored by :meth:`to_payload`, or ``None`` when the payload carries none (or cannot be read)."""
        columns = payload.get("columns")
        physical = payload.get("physical")
        if not isinstance(columns, list) or not isinstance(physical, dict):
            return None
        try:
            return cls(
                tuple(ColumnSpec.model_validate(c) for c in columns),
                {str(k): str(v) for k, v in physical.items()},
                truncated=payload.get("columns_truncated") is True,
            )
        except ValueError:  # pydantic's ValidationError is a ValueError
            return None


@dataclass(frozen=True)
class ParsedCommit:
    version: int
    committed_at: datetime | None = None
    operation: str | None = None
    engine_info: str | None = None
    user_metadata: str | None = None
    schema: SchemaSnapshot | None = None  # None: the commit does not carry a metaData action
    truncated: bool = False


def _loads(text: str) -> Any:
    """``json.loads`` that a writer of the table cannot make raise: bad JSON, or nesting beyond the parser's limit."""
    try:
        return json.loads(text)
    except (ValueError, RecursionError):  # a 200 KB line of "[[[[..." is well under every size cap
        return None


def parse_schema_string(raw: object) -> SchemaSnapshot | None:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_SCHEMA_STRING:
        return None
    document = _loads(raw)
    fields = document.get("fields") if isinstance(document, dict) else None
    if not isinstance(fields, list):
        return None
    columns: list[ColumnSpec] = []
    physical: dict[str, str] = {}
    for entry in fields[:MAX_COLUMNS]:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = clip(entry["name"], MAX_NAME)
        if not name or name in physical or any(c.name == name for c in columns):
            continue  # an empty or duplicate (after cleaning) name is not a column we can reason about
        columns.append(
            ColumnSpec(name=name, data_type=render_type(entry.get("type")), nullable=entry.get("nullable") is not False)
        )
        metadata = entry.get("metadata")
        if isinstance(metadata, dict) and isinstance(metadata.get(_PHYSICAL_NAME), str):
            physical[name] = clip(metadata[_PHYSICAL_NAME], MAX_NAME)
    return SchemaSnapshot(tuple(columns), physical, truncated=len(fields) > MAX_COLUMNS)


def _action(line: str) -> tuple[str, dict[str, Any]] | None:
    if len(line) > MAX_LINE:
        return None
    document = _loads(line)
    if not isinstance(document, dict) or len(document) != 1:
        return None
    ((kind, body),) = document.items()
    return (kind, body) if isinstance(body, dict) else None


def _millis(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_commit(commit: DeltaCommitFile) -> ParsedCommit:
    """The schema and provenance a commit file carries. Malformed or unknown lines are ignored."""
    committed_at: datetime | None = None
    operation = engine_info = user_metadata = None
    schema: SchemaSnapshot | None = None
    for line in commit.lines:
        parsed = _action(line)
        if parsed is None:
            continue
        kind, body = parsed
        if kind == "commitInfo":
            committed_at = _millis(body.get("timestamp")) or committed_at
            operation = clip(body["operation"], 120) if isinstance(body.get("operation"), str) else operation
            engine_info = clip(body["engineInfo"], 120) if isinstance(body.get("engineInfo"), str) else engine_info
            if isinstance(body.get("userMetadata"), str):
                user_metadata = clip(body["userMetadata"], 300)
        elif kind == "metaData":
            schema = parse_schema_string(body.get("schemaString")) or schema
            committed_at = committed_at or _millis(body.get("createdTime"))
    return ParsedCommit(commit.version, committed_at, operation, engine_info, user_metadata, schema, commit.truncated)


def diff_schemas(
    old: SchemaSnapshot, new: SchemaSnapshot
) -> tuple[list[ColumnSpec], list[ColumnSpec], list[TypeChange], list[Rename]]:
    """``(added, removed, type_changes, renames)`` going from ``old`` to ``new``."""
    old_by, new_by = old.by_name(), new.by_name()
    renames: list[Rename] = []
    # Column mapping: the same physical column under a different logical name is a rename, not a drop plus an add.
    old_by_physical = {phys: name for name, phys in old.physical.items()}
    renamed_before: set[str] = set()
    renamed_after: set[str] = set()
    for name, phys in new.physical.items():
        before = old_by_physical.get(phys)
        if before is not None and before != name and before in old_by and name in new_by:
            renames.append(Rename(before=before, after=name))
            renamed_before.add(before)
            renamed_after.add(name)
    added = [c for n, c in new_by.items() if n not in old_by and n not in renamed_after]
    removed = [c for n, c in old_by.items() if n not in new_by and n not in renamed_before]
    type_changes = [
        TypeChange(column=n, before=old_by[n].data_type, after=c.data_type)
        for n, c in new_by.items()
        if n in old_by and old_by[n].data_type != c.data_type
    ]
    return added, removed, type_changes, renames


def schema_change(
    table: str,
    old: tuple[int, SchemaSnapshot],
    new: tuple[int, SchemaSnapshot],
    commit: ParsedCommit,
) -> SchemaChange | None:
    """The change between two known schemas, or ``None`` when the columns and their types are the same."""
    added, removed, type_changes, renames = diff_schemas(old[1], new[1])
    if not (added or removed or type_changes or renames):
        return None
    return SchemaChange(
        table=clip(table, MAX_NAME),
        from_version=old[0],
        to_version=new[0],
        committed_at=commit.committed_at,
        operation=commit.operation,
        added=added,
        removed=removed,
        type_changes=type_changes,
        renames=renames,
    )
