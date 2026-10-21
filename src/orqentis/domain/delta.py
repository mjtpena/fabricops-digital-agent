"""Delta Lake table metadata as read from a table's ``_delta_log`` (schema history only).

Everything here comes from a file a data engineer controls: table and column names, ``commitInfo`` fields. It is
untrusted data. These models carry it only after the parser (``telemetry/delta.py``) bounded and sanitised it.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from orqentis.domain.common import DomainModel


class ColumnSpec(DomainModel):
    """One top-level column of a Delta table schema."""

    name: str = Field(max_length=256)
    data_type: str = Field(max_length=256)
    nullable: bool = True


class DeltaCommitRef(DomainModel):
    """A JSON commit file of a table's log, as listed (the content is read separately)."""

    version: int = Field(ge=0)
    modified_at: AwareDatetime | None = None


class DeltaCommitFile(DomainModel):
    """What the adapter returns for one JSON commit: only the action lines the collector needs.

    ``lines`` holds the raw ``metaData``, ``commitInfo`` and ``protocol`` actions (one JSON document per line); the many
    ``add`` / ``remove`` lines are skipped while streaming. ``truncated`` says the byte cap was reached before the end.
    """

    version: int = Field(ge=0)
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False
    size_bytes: int | None = Field(default=None, ge=0)


class DeltaTableRef(DomainModel):
    """A table declared in the contracts, resolved to the lakehouse that holds it."""

    id: str
    item_id: str
    workspace_id: str
    # The same shape the contracts allow: a name can never carry a path separator or a dot segment.
    table: str = Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
    schema_name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

    @property
    def path(self) -> str:
        """Path of the table below the lakehouse root: ``Tables/<table>`` or ``Tables/<schema>/<table>``."""
        return f"Tables/{self.schema_name}/{self.table}" if self.schema_name else f"Tables/{self.table}"


class ChangeKind(StrEnum):
    COLUMN_ADDED = "COLUMN_ADDED"
    COLUMN_REMOVED = "COLUMN_REMOVED"
    COLUMN_RENAMED = "COLUMN_RENAMED"
    TYPE_CHANGED = "TYPE_CHANGED"


class TypeChange(DomainModel):
    column: str
    before: str
    after: str


class Rename(DomainModel):
    before: str
    after: str


class SchemaChange(DomainModel):
    """The difference between two consecutive schemas of one table (versions ``from_version`` -> ``to_version``)."""

    table: str
    from_version: int
    to_version: int
    committed_at: AwareDatetime | None = None
    operation: str | None = Field(default=None, max_length=120)
    added: list[ColumnSpec] = Field(default_factory=list)
    removed: list[ColumnSpec] = Field(default_factory=list)
    type_changes: list[TypeChange] = Field(default_factory=list)
    # Only when Delta column mapping proves it (same physical column, new logical name). A removed and an added column
    # of the same type is *consistent with* a rename but not proof of one, so the reasoner words it that way.
    renames: list[Rename] = Field(default_factory=list)

    @property
    def is_breaking(self) -> bool:
        """Whether a reader that selects columns by name can fail: a column went away or changed type."""
        return bool(self.removed or self.type_changes or self.renames)
