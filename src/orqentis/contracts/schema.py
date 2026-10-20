"""The committed JSON Schema of the contracts file, generated from the Pydantic model.

``schemas/contracts.schema.json`` is what editors use for completion and validation (``"$schema"`` in the
contracts file). A test fails when the committed file drifts from the model; regenerate it with
``orqentis contracts schema --output schemas/contracts.schema.json``.
"""

from __future__ import annotations

import json
from typing import Any

from orqentis.contracts.model import EstateContracts

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_TITLE = "Orqentis FabricOps contracts"


def contracts_json_schema() -> dict[str, Any]:
    schema = EstateContracts.model_json_schema(by_alias=True)
    schema["title"] = SCHEMA_TITLE
    schema["description"] = (
        "Declared environments, external sources, data products, dependencies and SLAs of a Microsoft Fabric "
        "estate. Validate with `orqentis contracts validate`; draft with `orqentis contracts scaffold`."
    )
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def render_contracts_schema() -> str:
    """The schema as committed: two-space indent, trailing newline, stable key order."""
    return json.dumps(contracts_json_schema(), indent=2, ensure_ascii=False) + "\n"
