"""Settings, ``.env.example``, Bicep and the docs describe the same configuration (invariant: they stay consistent)."""

from __future__ import annotations

import re

from orqentis.config.settings import Settings
from tests.conftest import REPO

BICEP = REPO / "infra" / "bicep"
PREFIX = "ORQENTIS_"


def setting_variables() -> set[str]:
    return {f"{PREFIX}{name.upper()}" for name in Settings.model_fields}


def text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def test_every_orqentis_variable_in_env_example_is_a_setting() -> None:
    names = set(re.findall(rf"\b({PREFIX}[A-Z0-9_]+)=", text(".env.example")))
    assert names, "no variables found in .env.example"
    assert names - setting_variables() == set(), "variables in .env.example that Settings does not read"


def test_every_orqentis_variable_set_by_bicep_is_a_setting() -> None:
    names = set(re.findall(rf"'({PREFIX}[A-Z0-9_]+)'", text("infra/bicep/main.bicep")))
    assert names - setting_variables() == set(), "variables Bicep sets that Settings does not read"


def test_new_cloud_and_identity_settings_are_documented_everywhere() -> None:
    new = {
        "ORQENTIS_DEFAULT_TIMEZONE",
        "ORQENTIS_AUTHORITY_HOST",
        "ORQENTIS_FABRIC_SCOPE",
        "ORQENTIS_POWERBI_SCOPE",
        "ORQENTIS_LLM_AUTH_MODE",
    }
    assert new <= setting_variables()
    env_example, bicep, bicep_readme = (
        text(".env.example"),
        text("infra/bicep/main.bicep"),
        text("infra/bicep/README.md"),
    )
    for name in new:
        assert name in env_example, f"{name} missing from .env.example"
        assert name in bicep, f"{name} missing from main.bicep"
        assert name in bicep_readme, f"{name} missing from infra/bicep/README.md"
    development = text("docs/development.md")
    for name in (
        "ORQENTIS_AUTHORITY_HOST",
        "ORQENTIS_FABRIC_SCOPE",
        "ORQENTIS_POWERBI_SCOPE",
        "ORQENTIS_LLM_AUTH_MODE",
    ):
        assert name in development, f"{name} missing from docs/development.md"


def test_bicep_parameters_in_the_readme_exist() -> None:
    declared = set(re.findall(r"^param (\w+) ", text("infra/bicep/main.bicep"), flags=re.MULTILINE))
    for name in (
        "defaultTimezone",
        "fabricAuthMode",
        "fabricServicePrincipalClientId",
        "fabricServicePrincipalSecretUri",
        "fabricServicePrincipalCertificateUri",
        "keyVaultResourceId",
        "authorityHost",
        "fabricApiBaseUrl",
        "powerbiApiBaseUrl",
        "fabricScope",
        "powerbiScope",
    ):
        assert name in declared, f"{name} is not a parameter of main.bicep"
        assert name in text("infra/bicep/README.md"), f"{name} is not documented in infra/bicep/README.md"


def test_every_parameter_file_sets_only_declared_parameters() -> None:
    declared = set(re.findall(r"^param (\w+) ", text("infra/bicep/main.bicep"), flags=re.MULTILINE))
    files = sorted(BICEP.rglob("*.bicepparam"))
    assert len(files) >= 2
    for path in files:
        body = path.read_text(encoding="utf-8")
        assert re.search(r"^using '\.{1,2}/main\.bicep'", body, flags=re.MULTILINE), path
        used = set(re.findall(r"^param (\w+) =", body, flags=re.MULTILINE))
        assert used <= declared, f"{path.name} sets undeclared parameters: {sorted(used - declared)}"


def test_parameter_files_only_reference_secrets_in_key_vault() -> None:
    """A parameter file may point at a Key Vault secret (a versionless URI); it never holds a value."""
    for path in sorted(BICEP.rglob("*.bicepparam")):
        body = path.read_text(encoding="utf-8")
        for name, value in re.findall(
            r"^param (\w*(?:Secret|Password|Key|Certificate)\w*) = '([^']*)'", body, flags=re.M
        ):
            assert name.endswith("Uri") or name.endswith("Id") or not value, (
                f"{path.name}: {name} must be a Key Vault reference"
            )
            if value:
                assert re.fullmatch(r"https://[\w-]+\.vault\.azure\.net/secrets/[\w-]+", value) or value.startswith(
                    "/subscriptions/"
                )
