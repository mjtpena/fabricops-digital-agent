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
    assert len(params) >= 25
    for param in params:
        # Booleans are passed as `param ? 'true' : 'false'`, numbers as `string(param)`.
        assert f"{param} ? " in BICEP or f"string({param})" in BICEP, f"{param} is declared but never passed to the app"
        assert f"`{param}`" in BICEP_README, f"{param} is not documented in infra/bicep/README.md"


def test_every_runtime_setting_an_operator_may_need_to_tune_has_a_bicep_parameter() -> None:
    wanted = {
        name
        for name in Settings.model_fields
        if name.startswith(("scheduler_", "investigation_", "collection_", "build_retry_"))
        or name in {"startup_grace_seconds", "fabric_max_concurrency", "fabric_rate_per_second", "fabric_rate_burst"}
        or name == "fabric_job_instances_max_pages"
    }
    missing = sorted(f"ORQENTIS_{name.upper()}" for name in wanted if f"name: 'ORQENTIS_{name.upper()}'" not in BICEP)
    assert missing == [], f"main.bicep has no parameter for: {missing}"


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


# --------------------------------------------------------------------------------------------- durable storage (WS6)

INFRA = REPO / "infra" / "bicep"
MODULES = sorted((INFRA / "modules").glob("*.bicep"))
ENV_FILES = {
    name: (INFRA / "env" / f"{name}.bicepparam").read_text(encoding="utf-8") for name in ("dev", "test", "prod")
}
WORKFLOWS = REPO / ".github" / "workflows"
EVIDENCE_MODULE = (INFRA / "modules" / "evidence-storage.bicep").read_text(encoding="utf-8")
POSTGRES_MODULE = (INFRA / "modules" / "postgres.bicep").read_text(encoding="utf-8")
STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"


def _code(text: str) -> str:
    """The Bicep source without ``//`` comments, so a comment that explains what was removed cannot trip a check."""
    return "\n".join(re.sub(r"\s//.*$", "", line) for line in text.splitlines() if not line.lstrip().startswith("//"))


def _param_value(file_text: str, name: str) -> str | None:
    match = re.search(rf"^param {name} = (.+?)\s*(?://.*)?$", file_text, re.M)
    return match.group(1) if match else None


def test_no_template_mounts_azure_files_or_reads_a_storage_key() -> None:
    forbidden = [
        "accountKey",
        "AzureFile",
        "azureFile",
        "fileServices",
        "shareQuota",
        "mountOptions",
        "nobrl",
        "listAccountSas",
        "listServiceSas",
        "primaryEndpoints.file",
    ]
    for path in [INFRA / "main.bicep", *MODULES]:
        code = _code(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} still uses {token}"
        # The one key the template reads is the Log Analytics workspace key for the environment's log destination.
        readers = set(re.findall(r"(\w+)\.listKeys\(", code))
        assert readers <= {"logAnalytics"}, f"{path.name} reads keys of {sorted(readers - {'logAnalytics'})}"
    main = _code(BICEP)
    assert "storageType: 'AzureFile'" not in main
    assert "'orqentis-data'" not in main, "the app has no volume of its own; ORQENTIS_DATA_DIR is the container's disk"
    assert "name: 'ORQENTIS_DATA_DIR', value: dataDir" in main
    assert "fileShareQuotaGiB" not in main
    assert "dataFileShareName" not in main


def test_removed_azure_files_parameters_are_gone_from_every_parameter_file() -> None:
    for path in sorted(INFRA.rglob("*.bicepparam")):
        assert "fileShareQuotaGiB" not in path.read_text(encoding="utf-8"), path.name


def test_the_evidence_account_has_no_shared_key_and_is_immutable() -> None:
    code = _code(EVIDENCE_MODULE)
    assert "allowSharedKeyAccess: false" in code
    assert "allowSharedKeyAccess: true" not in _code(BICEP)
    for setting in (
        "defaultToOAuthAuthentication: true",
        "minimumTlsVersion: 'TLS1_2'",
        "supportsHttpsTrafficOnly: true",
        "allowBlobPublicAccess: false",
        "allowCrossTenantReplication: false",
        "isHnsEnabled: false",
        "isVersioningEnabled: true",
    ):
        assert setting in code, f"evidence-storage.bicep lacks {setting}"
    # Version-level immutability on the account (a default retention policy) and on both containers; soft delete.
    assert code.count("immutableStorageWithVersioning: {") == 3
    assert "immutabilityPeriodSinceCreationInDays: retentionDays" in code
    assert "deleteRetentionPolicy" in code
    assert "containerDeleteRetentionPolicy" in code
    # Locking is irreversible: the template default is Unlocked and the choice is the owner's.
    assert re.search(r"param immutabilityState string = 'Unlocked'", code)
    assert re.search(r"^param evidenceImmutabilityState string = 'Unlocked'$", _code(BICEP), re.M)
    assert re.search(r"^param evidenceRetentionDays int = 30$", _code(BICEP), re.M)


def test_prod_keeps_the_immutability_policy_unlocked_until_the_owner_approves() -> None:
    """Locked is irreversible. Change this test in the same pull request that carries the owner's approval."""
    for environment, text in ENV_FILES.items():
        assert _param_value(text, "evidenceImmutabilityState") == "'Unlocked'", (
            f"{environment}: Locked is irreversible and needs the owner's approval (docs/delivery.md, README Storage)"
        )


def test_the_reader_identity_gets_blob_access_on_the_two_containers_only() -> None:
    code = _code(EVIDENCE_MODULE)
    assert f"'{STORAGE_BLOB_DATA_CONTRIBUTOR}'" in code
    assignments = re.findall(
        r"resource (\w+) 'Microsoft.Authorization/roleAssignments@[^']+' = \{(.*?)\n\}", code, re.S
    )
    assert len(assignments) == 2, "one role assignment per container, nothing else"
    scopes = []
    for _, body in assignments:
        found = re.search(r"scope: (\w+)", body)
        assert found is not None
        scopes.append(found.group(1))
        assert "principalType: 'ServicePrincipal'" in body
        assert "name: guid(" in body, "deterministic role assignment names"
    assert sorted(scopes) == ["auditAnchorContainer", "evidenceContainer"], "scoped to the containers, not the account"
    # The module is handed the reader identity only; the actor (WRITE) identity gets nothing on storage.
    call = re.search(r"module evidenceStorage 'modules/evidence-storage.bicep' = \{(.*?)\n\}", BICEP, re.S)
    assert call is not None
    assert "readerPrincipalId: readerIdentity.properties.principalId" in call.group(1)
    assert "actorIdentity" not in call.group(1)
    assert "listKeys" not in code


def test_postgres_is_entra_only_with_a_required_administrator() -> None:
    code = _code(POSTGRES_MODULE)
    assert "passwordAuth: 'Disabled'" in code
    assert "activeDirectoryAuth: 'Enabled'" in code
    assert "administratorLogin" not in code, "no administrator login or password exists on the server"
    assert "name: 'require_secure_transport'" in code
    main = _code(BICEP)
    assert re.search(r"^param postgresEntraAdminObjectId string$", main, re.M), "the administrator has no default"
    assert re.search(r"^param postgresEntraAdminName string$", main, re.M), "the administrator has no default"
    assert re.search(r"^param postgresAllowAzureServices bool = false$", main, re.M), "closed unless an env opens it"
    assert "module storageInputCheck 'modules/input-check.bicep'" in main
    assert "storageInputCheck" in main.split("dependsOn: [")[-1], "the Container App waits for the input check"
    for name in ("postgres", "evidenceStorage"):
        block = re.search(rf"module {name} '[^']+' = \{{(.*?)\n\}}", main, re.S)
        assert block is not None
        assert "storageInputCheck" in block.group(1), f"{name} waits for the input check"
    # The pipeline supplies the administrator from variables, never from the repository.
    for text in ENV_FILES.values():
        assert "readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_OBJECT_ID')" in text
        assert "readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_NAME')" in text
        assert "readEnvironmentVariable('POSTGRES_ENTRA_ADMIN_TYPE', 'Group')" in text


def test_postgres_names_and_tiers_are_what_the_adr_decided() -> None:
    main = _code(BICEP)
    assert re.search(r"^param postgresSkuName string = 'Standard_B1ms'$", main, re.M)
    assert re.search(r"^param postgresBackupRetentionDays int = 7$", main, re.M)
    assert re.search(r"^param postgresDatabaseName string = 'orqentis'$", main, re.M)
    assert "'psql-${prefix}-${uniqueString(resourceGroup().id)}'" in main
    for environment in ("dev", "test"):
        assert str(_param_value(ENV_FILES[environment], "postgresSkuName")).startswith("'Standard_B"), environment
    assert not str(_param_value(ENV_FILES["prod"], "postgresSkuName")).startswith("'Standard_B"), (
        "prod is not Burstable"
    )


def test_replicas_are_parameters_and_the_environments_set_them() -> None:
    main = _code(BICEP)
    for name in ("minReplicas", "maxReplicas"):
        assert re.search(rf"@minValue\(1\)\n@maxValue\(\d+\)\nparam {name} int = 2$", main, re.M), name
    scale = re.search(r"scale: \{(.*?)\}", main, re.S)
    assert scale is not None
    assert "minReplicas: minReplicas" in scale.group(1)
    assert "maxReplicas: maxReplicas" in scale.group(1)
    assert "maxReplicas must be at least minReplicas" in main
    assert "activeRevisionsMode: 'Single'" in main
    assert "single writer" not in BICEP.lower()
    assert "do not raise maxreplicas" not in BICEP.lower()
    expected = {"dev": "1", "test": "2", "prod": "2"}
    for environment, replicas in expected.items():
        assert _param_value(ENV_FILES[environment], "minReplicas") == replicas, environment
        assert _param_value(ENV_FILES[environment], "maxReplicas") == replicas, environment


def test_the_connection_budget_fits_the_sku_of_every_environment() -> None:
    """Mirrors the template's check: (maxReplicas + minReplicas) x pool + 15 reserved <= 50 on Standard_B1ms."""
    found = re.search(r"^param postgresPoolMax int = (\d+)$", BICEP, re.M)
    assert found is not None
    pool = int(found.group(1))
    assert pool == 8
    for environment, text in ENV_FILES.items():
        if _param_value(text, "postgresSkuName") != "'Standard_B1ms'":
            continue
        replicas = int(str(_param_value(text, "maxReplicas"))) + int(str(_param_value(text, "minReplicas")))
        assert replicas * pool + 15 <= 50, f"{environment}: {replicas * pool + 15} connections do not fit Standard_B1ms"


def test_the_estate_contracts_file_is_a_mounted_secret_not_a_share() -> None:
    main = _code(BICEP)
    assert re.search(r"@secure\(\)\nparam contractsContent string = ''$", main, re.M)
    assert re.search(r"^param contractsPath string = '/mnt/contracts/estate\.json'$", main, re.M)
    assert "contractsSecretName = 'estate-contracts'" in main
    assert "storageType: 'Secret'" in main
    assert "fabricMode == 'rest' && !hasContracts" in main, "rest mode without contracts fails the deployment"
    for environment, text in ENV_FILES.items():
        assert "readEnvironmentVariable('CONTRACTS_JSON', '')" in text, environment
        assert _param_value(text, "contractsPath") == "'/mnt/contracts/estate.json'", environment
        assert "/var/lib/orqentis/contracts" not in text
    for token in ("contractsContent", "CONTRACTS_JSON", "/mnt/contracts/estate.json", "estate-contracts"):
        assert token in BICEP_README, f"{token} is not documented in infra/bicep/README.md"


def test_the_storage_environment_variables_are_set_and_documented() -> None:
    expected = {
        "ORQENTIS_STORAGE_BACKEND": "'postgres'",
        "ORQENTIS_POSTGRES_HOST": "postgres.outputs.fqdn",
        "ORQENTIS_POSTGRES_DATABASE": "postgres.outputs.databaseName",
        "ORQENTIS_POSTGRES_USER": "readerIdentityName",
        "ORQENTIS_POSTGRES_POOL_MAX": "string(postgresPoolMax)",
        "ORQENTIS_EVIDENCE_BLOB_URL": "evidenceStorage.outputs.blobEndpoint",
        "ORQENTIS_EVIDENCE_CONTAINER": "evidenceStorage.outputs.evidenceContainerName",
        "ORQENTIS_AUDIT_ANCHOR_CONTAINER": "evidenceStorage.outputs.auditAnchorContainerName",
        "ORQENTIS_SCHEDULER_AUDIT_ANCHOR_ENABLED": "auditAnchorEnabled ? 'true' : 'false'",
        "ORQENTIS_SCHEDULER_AUDIT_ANCHOR_INTERVAL_MINUTES": "string(auditAnchorIntervalMinutes)",
        "ORQENTIS_INVESTIGATION_POLL_INTERVAL_SECONDS": "string(investigationPollIntervalSeconds)",
        "ORQENTIS_INVESTIGATION_CLAIM_TTL_SECONDS": "string(investigationClaimTtlSeconds)",
    }
    for name, value in expected.items():
        assert f"{{ name: '{name}', value: {value} }}" in BICEP, f"{name} is not set to {value}"
        assert name.removeprefix("ORQENTIS_").lower() in Settings.model_fields, f"{name} is not a setting"
        assert f"`{name}`" in BICEP_README, f"{name} is not documented in infra/bicep/README.md"
    # No password, key or connection string reaches the app.
    assert "ORQENTIS_POSTGRES_PASSWORD" not in BICEP
    assert "ORQENTIS_EVIDENCE_KEY" not in BICEP


def test_every_durable_storage_parameter_is_documented() -> None:
    section = re.search(r"-+ scale\n(.*?)\n// =+ variables", BICEP, re.S)
    assert section is not None, "the scale and durable storage parameter sections are missing from main.bicep"
    params = re.findall(r"^param (\w+) ", section.group(1), re.M)
    assert {
        "minReplicas",
        "maxReplicas",
        "postgresPoolMax",
        "postgresEntraAdminObjectId",
        "evidenceRetentionDays",
    } <= set(params)
    assert len(params) >= 25
    for param in params:
        assert f"`{param}`" in BICEP_README, f"{param} is not documented in infra/bicep/README.md"


def test_the_startup_delay_is_a_courtesy_not_a_workaround_for_a_shared_volume() -> None:
    assert re.search(r"^param schedulerStartupDelaySeconds int = 30$", BICEP, re.M)
    for environment, text in ENV_FILES.items():
        assert _param_value(text, "schedulerStartupDelaySeconds") == "30", environment
    assert "Azure Files" not in _code(BICEP)
    assert "courtesy" in BICEP_README


def test_every_variable_an_environment_file_requires_is_supplied_by_the_pipeline_and_the_validator() -> None:
    required: set[str] = set()
    for text in ENV_FILES.values():
        required |= set(re.findall(r"readEnvironmentVariable\('([A-Z_]+)'\)", text))
    assert {"POSTGRES_ENTRA_ADMIN_OBJECT_ID", "POSTGRES_ENTRA_ADMIN_NAME", "CONTAINER_IMAGE"} <= required
    deploy = (WORKFLOWS / "deploy-environment.yml").read_text(encoding="utf-8")
    whatif = (WORKFLOWS / "whatif.yml").read_text(encoding="utf-8")
    validate = (REPO / "scripts" / "validate-bicep.sh").read_text(encoding="utf-8")
    for name in sorted(required):
        assert f"export {name}=" in validate, f"scripts/validate-bicep.sh has no placeholder for {name}"
        assert name in deploy, f"deploy-environment.yml does not supply {name}"
        assert name in whatif, f"whatif.yml does not supply {name}"
    # Identifiers come from GitHub variables, never from secrets.
    assert "POSTGRES_ENTRA_ADMIN_OBJECT_ID: ${{ vars.POSTGRES_ENTRA_ADMIN_OBJECT_ID }}" in deploy
    assert "POSTGRES_ENTRA_ADMIN_NAME: ${{ vars.POSTGRES_ENTRA_ADMIN_NAME }}" in deploy
    assert "CONTRACTS_JSON: ${{ vars.CONTRACTS_JSON }}" in deploy
    assert not re.search(r"secrets\.\w", deploy)
    assert not re.search(r"secrets\.\w", whatif)


def test_the_deploy_identity_may_assign_the_storage_role_the_template_grants() -> None:
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    assert STORAGE_BLOB_DATA_CONTRIBUTOR in delivery, "the RBAC Administrator condition must allow the storage role"
    assert STORAGE_BLOB_DATA_CONTRIBUTOR in EVIDENCE_MODULE
    assert "Microsoft.DBforPostgreSQL" in delivery, "the resource provider must be registered once"
