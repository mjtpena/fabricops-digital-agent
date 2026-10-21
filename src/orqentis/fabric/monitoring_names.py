"""Which KQL database of a workspace is its monitoring database (decision D9, docs/spikes/workspace-monitoring.md).

Nothing in the Fabric API marks a KQL database as the one workspace monitoring created: the listing has an id, a display
name and ``properties`` (``parentEventhouseItemId``, ``queryServiceUri``, ``ingestionServiceUri``, ``databaseType`` =
``ReadWrite`` or ``Shortcut``), none of which says "monitoring". So the display name is all there is, and it differs:

* the legacy monitoring database is called ``Monitoring KQL database`` in every workspace;
* the monitoring item that replaced it names its database ``<workspace name> <monitoring item name>_Database``
  (documentation: workspace name + item name + ``_Database``, illegal characters replaced by ``_``), so it differs per
  workspace and no single configured value can match them all.

**Neither form has been observed on a real monitoring database yet** (the owner has not enabled monitoring); both are
the documented forms and are re-checked then. A deployment whose names follow neither sets
``ORQENTIS_WORKSPACE_MONITORING_DATABASE``, which overrides the rules: only that exact name (case-insensitive) matches.

Two databases matching the rules in one workspace is never resolved by guessing: the callers treat it as an error.
"""

from __future__ import annotations

import re

LEGACY_DATABASE_NAME = "Monitoring KQL database"
_SUFFIX = "_database"
_NOT_WORD = re.compile(r"[\W_]+")


def _slug(text: str) -> str:
    """Case-folded, every run of characters Fabric would replace (spaces, brackets, punctuation) as one ``_``."""
    return _NOT_WORD.sub("_", text.casefold()).strip("_")


def is_monitoring_database(display_name: str, *, workspace_name: str | None, override: str = "") -> bool:
    """Whether a KQL database called ``display_name`` is the monitoring database of the workspace ``workspace_name``.

    ``override`` (the configured name) replaces the rules. Without it, a database is the monitoring database if it
    has the legacy name, or if its name is the workspace's name followed by anything and ``_Database``. The second rule
    needs ``workspace_name``; without one only the legacy name can match.
    """
    name = display_name.strip()
    configured = override.strip()
    if configured:
        return name.casefold() == configured.casefold()
    if name.casefold() == LEGACY_DATABASE_NAME.casefold():
        return True
    workspace = _slug(workspace_name) if workspace_name else ""
    database = _slug(name)
    return bool(workspace) and database.startswith(f"{workspace}_") and database.endswith(_SUFFIX)
