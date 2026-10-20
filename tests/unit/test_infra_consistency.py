"""The Bicep template, the settings and the health routes must keep agreeing with each other."""

from __future__ import annotations

import re
from pathlib import Path

from orqentis.config.settings import Settings

REPO = Path(__file__).resolve().parents[2]
BICEP = (REPO / "infra" / "bicep" / "main.bicep").read_text(encoding="utf-8")
BICEP_README = (REPO / "infra" / "bicep" / "README.md").read_text(encoding="utf-8")


def test_every_orqentis_variable_in_bicep_is_a_real_setting() -> None:
    names = set(re.findall(r"name: '(ORQENTIS_[A-Z0-9_]+)'", BICEP))
    assert names, "expected the template to set ORQENTIS_* variables"
    unknown = sorted(n for n in names if n.removeprefix("ORQENTIS_").lower() not in Settings.model_fields)
    assert unknown == [], f"main.bicep sets variables that settings.py does not know: {unknown}"


def test_every_autonomous_runtime_parameter_reaches_the_app_and_is_documented() -> None:
    section = re.search(r"-+ autonomous runtime\n(.*?)\n// -+ network \+ auth", BICEP, re.S)
    assert section is not None, "the autonomous runtime parameter section is missing from main.bicep"
    params = re.findall(r"^param (\w+) ", section.group(1), re.M)
    assert len(params) >= 15
    for param in params:
        # Booleans are passed as `param ? 'true' : 'false'`, numbers as `string(param)`.
        assert f"{param} ? " in BICEP or f"string({param})" in BICEP, f"{param} is declared but never passed to the app"
        assert f"`{param}`" in BICEP_README, f"{param} is not documented in infra/bicep/README.md"


def test_the_probes_separate_liveness_from_readiness() -> None:
    probes = dict(re.findall(r"type: '(Startup|Liveness|Readiness)'\s+httpGet: \{\s+path: '([^']+)'", BICEP))
    assert probes == {"Startup": "/health", "Liveness": "/health", "Readiness": "/health/ready"}
    startup_budget = re.search(
        r"type: 'Startup'.*?initialDelaySeconds: (\d+)\s+periodSeconds: (\d+).*?failureThreshold: (\d+)", BICEP, re.S
    )
    assert startup_budget is not None
    initial, period, failures = (int(g) for g in startup_budget.groups())
    # The container build (estate enumeration) no longer counts against the startup probe, so the budget is short.
    assert initial + period * failures <= 70
