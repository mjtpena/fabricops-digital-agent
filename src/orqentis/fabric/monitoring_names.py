"""Which KQL database of a workspace is its monitoring database (decision D9, docs/spikes/workspace-monitoring.md).

Nothing in the Fabric API marks a KQL database as the one workspace monitoring created: the listing has an id, a display
name and ``properties`` (``parentEventhouseItemId``, ``queryServiceUri``, ``ingestionServiceUri``, ``databaseType`` =
``ReadWrite`` or ``Shortcut``), none of which says "monitoring". So the display name is all there is, and it differs:

* the legacy monitoring database is called ``Monitoring KQL database`` in every workspace;
* the monitoring item that replaced it names its database ``<workspace name> <monitoring item name>_Database``
  (documentation: workspace name + item name + ``_Database``, illegal characters replaced by ``_``), so it differs per
  workspace and no single configured value can match them all.

**Neither form has been observed on a real monitoring database yet** (the owner has not enabled monitoring); both are
the documented forms and are re-checked then. How sure a match is matters to the caller, so it is reported:

* ``NAMED``: the configured name (``ORQENTIS_WORKSPACE_MONITORING_DATABASE`` replaces the rules and matches only that
  exact name, case-insensitively), or the legacy name. The owner or the documentation says this is the database.
* ``INFERRED``: the name has the monitoring item's documented shape. Any KQL database an engineer names that way matches
  too, so a caller must not read "the table is missing" from such a database as "no activity yet".

The monitoring item's shape is matched literally and case-sensitively, as Fabric writes it: the workspace's name (each
character as itself, or as ``_`` where Fabric replaces it), a separator, an item name with at least one letter or digit,
and the literal suffix ``_Database``. ``Sales Database`` and ``Sales-Archive-Database`` are not that shape.

Two databases matching in one workspace is never resolved by guessing: the callers treat it as an error.
"""

from __future__ import annotations

from enum import StrEnum

LEGACY_DATABASE_NAME = "Monitoring KQL database"
_SUFFIX = "_Database"


class Identification(StrEnum):
    NAMED = "named"
    INFERRED = "inferred"


def _item_database(name: str, workspace_name: str) -> bool:
    """Whether ``name`` is ``<workspace><separator><item name>_Database`` (illegal characters replaced by ``_``)."""
    if not workspace_name or not name.endswith(_SUFFIX):
        return False
    stem = name[: -len(_SUFFIX)]
    if len(stem) <= len(workspace_name):
        return False  # no room for a separator and an item name
    prefix, rest = stem[: len(workspace_name)], stem[len(workspace_name) :]
    if not all(p == w or (p == "_" and not w.isalnum()) for p, w in zip(prefix, workspace_name, strict=True)):
        return False
    return not rest[0].isalnum() and any(c.isalnum() for c in rest)


def identify_monitoring_database(
    display_name: str, *, workspace_name: str | None, override: str = ""
) -> Identification | None:
    """How ``display_name`` identifies the monitoring database of the workspace ``workspace_name``, or ``None``.

    Without ``workspace_name`` only the legacy name (or the override) can match.
    """
    name = display_name.strip()
    configured = override.strip()
    if configured:
        return Identification.NAMED if name.casefold() == configured.casefold() else None
    if name.casefold() == LEGACY_DATABASE_NAME.casefold():
        return Identification.NAMED
    if workspace_name and _item_database(name, workspace_name.strip()):
        return Identification.INFERRED
    return None


def is_monitoring_database(display_name: str, *, workspace_name: str | None, override: str = "") -> bool:
    return identify_monitoring_database(display_name, workspace_name=workspace_name, override=override) is not None
