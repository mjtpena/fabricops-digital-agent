"""Settings, ``.env.example``, Bicep and the docs describe the same configuration (invariant: they stay consistent)."""

from __future__ import annotations

import re

import pytest

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


# Key Vault DNS suffixes: public, US Government, China, Germany.
KEY_VAULT_SECRET_URI = re.compile(
    r"https://[\w-]+\.vault\.(?:azure\.net|usgovcloudapi\.net|azure\.cn|microsoftazure\.de)/secrets/[\w-]+"
)


@pytest.mark.parametrize(
    ("uri", "valid"),
    [
        ("https://kv-fabricops.vault.azure.net/secrets/fabric-sp-secret", True),
        ("https://kv-gov.vault.usgovcloudapi.net/secrets/sp", True),
        ("https://kv-cn.vault.azure.cn/secrets/sp", True),
        ("https://kv.vault.azure.net/keys/sp", False),
        ("https://kv.vault.azure.net/secrets/sp/0123456789abcdef0123456789abcdef", False),  # versioned: pins a value
        ("http://kv.vault.azure.net/secrets/sp", False),
        ("hunter2-not-a-reference", False),
    ],
)
def test_key_vault_secret_uri_shape(uri: str, valid: bool) -> None:
    assert bool(KEY_VAULT_SECRET_URI.fullmatch(uri)) is valid


CREDENTIAL_NAME = re.compile(r"(?i)secret|password|key|certificate|token|credential|passphrase")


def credential_problems(body: str) -> list[str]:
    """What is wrong with the credential-like parameters of a ``.bicepparam`` file's text.

    * A parameter whose name suggests a credential (secret, password, key, certificate, token, ...) and that ends in
      ``Uri`` may only point at a Key Vault secret (a versionless secrets URI, any national-cloud suffix).
    * Any other credential-named parameter with a value is a credential in the file, except a resource id
      (``...Id`` holding a ``/subscriptions/...`` path, such as the vault's).
    * Every parameter whose value is a Key Vault URL must be a proper Key Vault secret URI.
    """
    problems: list[str] = []
    for name, value in re.findall(r"^param (\w+) = '([^']*)'", body, flags=re.MULTILINE):
        if not value:
            continue
        credential_like = bool(CREDENTIAL_NAME.search(name))
        if name.endswith("Uri") and credential_like and not KEY_VAULT_SECRET_URI.fullmatch(value):
            problems.append(f"{name}: not a Key Vault secret URI")
        elif (
            credential_like
            and not name.endswith("Uri")
            and not (name.endswith("Id") and value.startswith("/subscriptions/"))
        ):
            problems.append(f"{name}: looks like a credential value in a parameter file")
        elif ".vault." in value and not KEY_VAULT_SECRET_URI.fullmatch(value):
            problems.append(f"{name}: a Key Vault URL that is not a versionless secret URI")
    return problems


@pytest.mark.security
def test_parameter_files_only_reference_secrets_in_key_vault() -> None:
    """A parameter file may point at a Key Vault secret (a versionless URI); it never holds a value."""
    files = sorted(BICEP.rglob("*.bicepparam"))
    assert files
    for path in files:
        assert credential_problems(path.read_text(encoding="utf-8")) == [], path.name
    cross_tenant = (BICEP / "examples" / "cross-tenant.bicepparam").read_text(encoding="utf-8")
    assert "param fabricServicePrincipalSecretUri = 'https://" in cross_tenant  # the check has something to check


@pytest.mark.security
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # What the guard must refuse ...
        (
            "param tlsCertificate = 'MIIB1TCCAX...'",
            ["tlsCertificate: looks like a credential value in a parameter file"],
        ),
        ("param otherSecretUri = 'https://evil.example.com/x'", ["otherSecretUri: not a Key Vault secret URI"]),
        ("param apiKey = 'hunter2-hunter2'", ["apiKey: looks like a credential value in a parameter file"]),
        ("param adminPassword = 'Pa55w0rd!'", ["adminPassword: looks like a credential value in a parameter file"]),
        (
            "param fabricServicePrincipalSecretUri = 'hunter2'",
            ["fabricServicePrincipalSecretUri: not a Key Vault secret URI"],
        ),
        (
            "param fabricServicePrincipalCertificateUri = 'https://kv.vault.azure.net/keys/sp'",
            ["fabricServicePrincipalCertificateUri: not a Key Vault secret URI"],
        ),
        (
            "param fabricServicePrincipalSecretUri = 'https://kv.vault.azure.net/secrets/sp/0123456789abcdef0123456789abcdef'",
            ["fabricServicePrincipalSecretUri: not a Key Vault secret URI"],
        ),
        (
            "param sourceUrl = 'https://kv.vault.azure.net/keys/sp'",
            ["sourceUrl: a Key Vault URL that is not a versionless secret URI"],
        ),
        # ... and what it must accept.
        ("param fabricServicePrincipalSecretUri = 'https://kv.vault.azure.net/secrets/sp'", []),
        ("param fabricServicePrincipalCertificateUri = 'https://kv.vault.usgovcloudapi.net/secrets/sp-cert'", []),
        ("param fabricServicePrincipalSecretUri = 'https://kv.vault.azure.cn/secrets/sp'", []),
        ("param fabricServicePrincipalSecretUri = ''", []),
        ("param keyVaultResourceId = '/subscriptions/0/resourceGroups/rg/providers/Microsoft.KeyVault/vaults/kv'", []),
        ("param fabricServicePrincipalClientId = '22222222-2222-2222-2222-222222222222'", []),
    ],
)
def test_credential_guard_refuses_credentials_and_foreign_uris(line: str, expected: list[str]) -> None:
    assert credential_problems(line) == expected


def test_main_bicep_checks_service_principal_inputs_and_scopes_the_key_vault_role() -> None:
    main = text("infra/bicep/main.bicep")
    assert "module servicePrincipalInputCheck 'modules/input-check.bicep'" in main
    assert "servicePrincipalInputCheck" in main.split("dependsOn: [")[-1]  # the Container App waits for the check
    role = text("infra/bicep/modules/keyvault-role.bicep")
    assert "param secretNames array" in role and "scope: secrets[index]" in role  # per secret, never the vault
    assert "scope: vault" not in role
