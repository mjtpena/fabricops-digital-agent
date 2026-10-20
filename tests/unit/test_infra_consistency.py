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
STORAGE_BLOB_DATA_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
# Built-in role definitions the templates may grant to the app identity (data-plane or narrowly scoped, no management).
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
KEY_VAULT_SECRETS_USER = "4633458b-17de-408a-b874-0445c86b69e6"
GRANTABLE_ROLES = {
    ACR_PULL,
    OPENAI_USER,
    KEY_VAULT_SECRETS_USER,
    STORAGE_BLOB_DATA_CONTRIBUTOR,
    STORAGE_BLOB_DATA_READER,
}
# Roles that can change a policy, a key, a network rule or a role assignment, or bypass immutability.
FORBIDDEN_FOR_THE_APP = {
    "8e3af657-a8ff-443c-a75c-2fe8c4bcb635": "Owner",
    "b24988ac-6180-42a0-ab88-20f7382dd24c": "Contributor",
    "17d1049b-9a84-46fb-8f53-869881c3d3ab": "Storage Account Contributor",
    "18d7d88d-d35e-4fb5-a5c3-7773c20a72d9": "User Access Administrator",
    "f58310d9-a9f6-439a-9e8d-f62e7b41a168": "Role Based Access Control Administrator",
    "b7e6dc6d-f1e8-4753-8033-0f276bb0955b": "Storage Blob Data Owner (runAsSuperUser bypasses immutability)",
}


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
        text = path.read_text(encoding="utf-8")
        assert "fileShareQuotaGiB" not in text, path.name
        # The contracts file is a blob now: no path to set and no text to pass (no pipeline variable either).
        for removed in ("contractsPath", "contractsContent", "CONTRACTS_JSON"):
            assert removed not in text, f"{path.name} still mentions {removed}"


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
    assert code.count("immutableStorageWithVersioning: {") == 5, "the account and each of the four containers"
    assert "immutabilityPeriodSinceCreationInDays: retentionDays" in code
    assert "deleteRetentionPolicy" in code
    assert "containerDeleteRetentionPolicy" in code
    # Locking is irreversible: the template default is Unlocked and the choice is the owner's.
    assert re.search(r"param immutabilityState string = 'Unlocked'", code)
    assert re.search(r"^param evidenceImmutabilityState string = 'Unlocked'$", _code(BICEP), re.M)
    assert re.search(r"^param evidenceRetentionDays int = 30$", _code(BICEP), re.M)


def test_the_immutability_lock_is_an_explicit_decision_and_one_mapping_sets_it() -> None:
    """Locked is irreversible for the retention period: prod has no default, dev and test default to Unlocked."""
    mapping = (
        "param evidenceImmutabilityState = evidenceLocked == 'true' ? 'Locked' : "
        "(evidenceLocked == 'false' ? 'Unlocked' : 'EVIDENCE_IMMUTABILITY_LOCKED must be true or false')"
    )
    assert "var evidenceLocked = readEnvironmentVariable('EVIDENCE_IMMUTABILITY_LOCKED')\n" in ENV_FILES["prod"], (
        "prod must read the decision with NO default"
    )
    for environment in ("dev", "test"):
        assert (
            "var evidenceLocked = readEnvironmentVariable('EVIDENCE_IMMUTABILITY_LOCKED', 'false')\n"
            in (ENV_FILES[environment])
        ), f"{environment} defaults to Unlocked"
    for environment, text in ENV_FILES.items():
        assert mapping in _code(text), (
            f"{environment}: only 'true' and 'false' are valid, anything else fails the build"
        )
    # The mapping is the only place that sets 'Locked': no parameter file (env, main, examples) names it anywhere else.
    for path in sorted(INFRA.rglob("*.bicepparam")):
        locked = [line for line in _code(path.read_text(encoding="utf-8")).splitlines() if "'Locked'" in line]
        expected = [mapping] if path.parent.name == "env" else []
        assert locked == expected, f"{path.name} sets 'Locked' outside the mapping"
    # The template keeps Unlocked as its own default; the deploy job passes the decision through (prod must set it).
    assert re.search(r"^param evidenceImmutabilityState string = 'Unlocked'$", _code(BICEP), re.M)
    deploy = (WORKFLOWS / "deploy-environment.yml").read_text(encoding="utf-8")
    assert "EVIDENCE_IMMUTABILITY_LOCKED_VAR: ${{ vars.EVIDENCE_IMMUTABILITY_LOCKED }}" in deploy
    assert (
        'true | false) echo "EVIDENCE_IMMUTABILITY_LOCKED=${EVIDENCE_IMMUTABILITY_LOCKED_VAR}" >> "$GITHUB_ENV"'
        in deploy
    )
    decision = deploy.split('case "$EVIDENCE_IMMUTABILITY_LOCKED_VAR" in')[1].split("esac")[0]
    assert '"$DEPLOY_ENVIRONMENT" == "prod"' in decision, "an empty variable is an error on prod"
    # Placeholders for the validator and the snapshot are never Locked.
    for script in ("validate-bicep.sh", "bicep-snapshot.sh"):
        text = (REPO / "scripts" / script).read_text(encoding="utf-8")
        assert re.search(r'export EVIDENCE_IMMUTABILITY_LOCKED="(?:\$\{EVIDENCE_IMMUTABILITY_LOCKED:-)?false', text), (
            script
        )
    # What the owner must know, in both documents.
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    for document in (BICEP_README, delivery):
        assert "EVIDENCE_IMMUTABILITY_LOCKED" in document
        assert re.search(r"irreversible", document, re.I)
        assert "Storage Account Contributor" in document, "an Unlocked policy can be removed by those roles"
        assert "sign-off" in document


def test_the_reader_identity_gets_blob_access_on_the_containers_only_and_read_only_on_epochs_and_contracts() -> None:
    code = _code(EVIDENCE_MODULE)
    assert f"'{STORAGE_BLOB_DATA_CONTRIBUTOR}'" in code
    assert f"'{STORAGE_BLOB_DATA_READER}'" in code
    assignments = re.findall(
        r"resource (\w+) 'Microsoft.Authorization/roleAssignments@[^']+' = (?:if (\(.*?\)) )?\{(.*?)\n\}",
        code,
        re.S,
    )
    assert len(assignments) == 5, "one per container for the app, plus the optional operators group, nothing else"
    roles: dict[str, tuple[str, str]] = {}
    for name, condition, body in assignments:
        scope = re.search(r"scope: (\w+)", body)
        role = re.search(r"roleDefinitions', (\w+)\)", body)
        principal = re.search(r"principalId: (\w+)", body)
        assert scope is not None
        assert role is not None
        assert principal is not None
        roles[name] = (scope.group(1), role.group(1))
        assert "name: guid(" in body, "deterministic role assignment names"
        if principal.group(1) == "readerPrincipalId":
            assert "principalType: 'ServicePrincipal'" in body
            assert not condition
        else:
            assert principal.group(1) == "auditOperatorsPrincipalId"
            assert "principalType: 'Group'" in body
            assert "!empty(auditOperatorsPrincipalId)" in condition, "nothing is assigned unless the group is given"
    # Scoped to the containers, never to the account. The app can only READ audit-epochs and contracts; only the
    # optional operators group can write audit-epochs, and nobody is granted contracts uploads by the template.
    assert roles == {
        "readerEvidenceAccess": ("evidenceContainer", "storageBlobDataContributorRoleId"),
        "readerAuditAnchorAccess": ("auditAnchorContainer", "storageBlobDataContributorRoleId"),
        "readerAuditEpochAccess": ("auditEpochContainer", "storageBlobDataReaderRoleId"),
        "readerContractsAccess": ("contractsContainer", "storageBlobDataReaderRoleId"),
        "operatorsAuditEpochAccess": ("auditEpochContainer", "storageBlobDataContributorRoleId"),
    }
    assert "var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'" in code
    assert "var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'" in code
    # The module is handed the reader identity only; the actor (WRITE) identity gets nothing on storage.
    call = re.search(r"module evidenceStorage 'modules/evidence-storage.bicep' = \{(.*?)\n\}", BICEP, re.S)
    assert call is not None
    assert "readerPrincipalId: readerIdentity.properties.principalId" in call.group(1)
    assert "auditOperatorsPrincipalId: auditOperatorsPrincipalId" in call.group(1)
    assert "actorIdentity" not in call.group(1)
    assert "listKeys" not in code


def test_the_audit_epoch_container_is_immutable_and_only_the_operators_can_write_it() -> None:
    code = _code(EVIDENCE_MODULE)
    container = re.search(
        r"resource auditEpochContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@[^']+' = \{(.*?)\n\}",
        code,
        re.S,
    )
    assert container is not None, "the audit-epochs container is missing"
    assert "parent: blobService" in container.group(1)
    assert "name: auditEpochContainerName" in container.group(1)
    assert "publicAccess: 'None'" in container.group(1)
    assert "immutableStorageWithVersioning: {" in container.group(1), "version-level immutability like the others"
    assert re.search(r"^param auditEpochContainerName string = 'audit-epochs'$", code, re.M)
    assert re.search(r"^param auditOperatorsPrincipalId string = ''$", code, re.M), "nothing is assigned by default"
    assert "output auditEpochContainerName string = auditEpochContainer.name" in code
    main = _code(BICEP)
    assert re.search(r"^param auditEpochContainerName string = 'audit-epochs'$", main, re.M)
    assert re.search(r"^param auditOperatorsPrincipalId string = ''$", main, re.M)
    assert "{ name: 'ORQENTIS_AUDIT_EPOCH_CONTAINER', value: evidenceStorage.outputs.auditEpochContainerName }" in main
    assert "audit_epoch_container" in Settings.model_fields
    # The operators group comes from the pipeline, optional, empty by default; the deploy job passes it through.
    for environment, text in ENV_FILES.items():
        assert "readEnvironmentVariable('AUDIT_OPERATORS_OBJECT_ID', '')" in text, environment
    deploy = (WORKFLOWS / "deploy-environment.yml").read_text(encoding="utf-8")
    assert "AUDIT_OPERATORS_OBJECT_ID: ${{ vars.AUDIT_OPERATORS_OBJECT_ID }}" in deploy
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    for token in ("AUDIT_OPERATORS_OBJECT_ID", "audit-epochs"):
        assert token in delivery
        assert token in BICEP_README
    for token in ("auditEpochContainerName", "auditOperatorsPrincipalId", "ORQENTIS_AUDIT_EPOCH_CONTAINER"):
        assert f"`{token}`" in BICEP_README, f"{token} is not documented in infra/bicep/README.md"
    # Who may write where is documented once, as a table, in the README.
    assert "Who may write where" in BICEP_README
    assert "nothing" in BICEP_README.split("Who may write where")[1].split("Audit epoch markers")[0]


def test_the_app_identity_has_no_role_that_could_change_a_policy_a_key_or_a_role() -> None:
    """The reader identity must not be able to change the immutability policies: it holds data-plane roles on
    containers (and one narrow role per secret, registry and OpenAI account), never a role on the account, the resource
    group or the subscription, and none of the management roles that could change a policy, a key or a role."""
    granted: set[str] = set()
    for path in [INFRA / "main.bicep", *MODULES]:
        code = _code(path.read_text(encoding="utf-8"))
        for guid in re.findall(r"= '([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'", code):
            granted.add(guid)
            assert guid not in FORBIDDEN_FOR_THE_APP, f"{path.name} grants {FORBIDDEN_FOR_THE_APP.get(guid)}"
        # No role assignment at the resource group, the subscription or the tenant.
        for body in re.findall(r"'Microsoft.Authorization/roleAssignments@[^']+' = [^{]*\{(.*?)\n\}", code, re.S):
            scope = re.search(r"scope: (\S+)", body)
            if scope is not None:
                assert not scope.group(1).startswith(("resourceGroup(", "subscription(", "tenant("))
    assert granted == GRANTABLE_ROLES, (
        f"unexpected role definitions in the templates: {sorted(granted ^ GRANTABLE_ROLES)}"
    )
    # Every storage role assignment is on a container, never on the account that holds the immutability policies.
    assert not re.search(r"scope: account\b", _code(EVIDENCE_MODULE))


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
    """The template's budget: 2 x maxReplicas x pool + 15 platform + 5 migration and operator connections."""
    found = re.search(r"^param postgresPoolMax int = (\d+)$", BICEP, re.M)
    assert found is not None
    pool = int(found.group(1))
    assert pool == 6
    main = _code(BICEP)
    assert "var postgresConnectionsNeeded = 2 * maxReplicas * postgresPoolMax + " in main
    assert "var postgresPlatformConnections = 15" in main
    assert "var postgresMigrationAndOperatorConnections = 5" in main
    assert "(maxReplicas + minReplicas)" not in main, "the old budget assumed the new revision at minReplicas only"
    # The minimum matches the app: Settings.postgres_pool_max has ge=2 (a migration holds a connection under its lock).
    field = Settings.model_fields["postgres_pool_max"]
    minimum = next(m.ge for m in field.metadata if hasattr(m, "ge"))
    assert minimum == 2
    assert re.search(rf"@minValue\({minimum}\)\n@maxValue\(\d+\)\nparam postgresPoolMax int = 6$", main, re.M)
    for environment, text in ENV_FILES.items():
        if _param_value(text, "postgresSkuName") != "'Standard_B1ms'":
            continue
        replicas = int(str(_param_value(text, "maxReplicas")))
        needed = 2 * replicas * pool + 15 + 5
        assert needed <= 50, f"{environment}: {needed} connections do not fit Standard_B1ms"
    # The template default (Standard_B1ms, 2 replicas) fits too.
    assert 2 * 2 * pool + 20 <= 50
    assert "old and new revision both at maxReplicas" in main


def test_the_estate_contracts_file_is_a_blob_not_a_secret_a_volume_or_a_share() -> None:
    main = _code(BICEP)
    # The removed designs: a Container App secret with a mounted file, an Azure Files path, a pipeline variable.
    for removed in (
        "contractsContent",
        "estate-contracts",
        "CONTRACTS_JSON",
        "ORQENTIS_CONTRACTS_PATH",
        "contractsPath",
    ):
        assert removed not in main, f"main.bicep still mentions {removed}"
        for path in MODULES:
            assert removed not in _code(path.read_text(encoding="utf-8")), f"{path.name} still mentions {removed}"
    for name in ("deploy-environment.yml", "bicep-diff.yml", "cd.yml", "ci.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "CONTRACTS_JSON" not in text, f"{name} still passes the contracts text"
        assert "contractsContent" not in text
    assert "CONTRACTS_JSON" not in (REPO / "scripts" / "validate-bicep.sh").read_text(encoding="utf-8")
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    assert "CONTRACTS_JSON" not in delivery
    assert "estate-contracts" not in delivery
    assert "storageType: 'Secret'" in main, (
        "the Key Vault certificate volume (service_principal mode) is the only secret volume"
    )
    assert main.count("storageType: 'Secret'") == 1
    # The parameters and the two settings they feed. They are not autonomous-runtime parameters.
    assert re.search(r"@maxLength\(63\)\nparam contractsContainerName string = 'contracts'$", main, re.M)
    assert re.search(r"^param contractsBlobName string = 'estate\.json'$", main, re.M)
    section = re.search(r"-+ autonomous runtime\n(.*?)\n// -+ network \+ auth", BICEP, re.S)
    assert section is not None
    assert "contractsContainerName" not in section.group(1)
    assert "contractsBlobName" not in section.group(1)
    for name, value in (
        ("ORQENTIS_CONTRACTS_CONTAINER", "evidenceStorage.outputs.contractsContainerName"),
        ("ORQENTIS_CONTRACTS_BLOB_NAME", "contractsBlobName"),
    ):
        assert f"{{ name: '{name}', value: {value} }}" in BICEP
        assert name.removeprefix("ORQENTIS_").lower() in Settings.model_fields, f"{name} is not a setting"
        assert f"`{name}`" in BICEP_README
    # No extra URL: the contracts live in the evidence account (ORQENTIS_EVIDENCE_BLOB_URL).
    assert "ORQENTIS_CONTRACTS_BLOB_URL" not in BICEP
    assert Settings.model_fields["contracts_container"].default == "contracts"
    assert Settings.model_fields["contracts_blob_name"].default == "estate.json"
    # The container exists, versioned like the others, and its name is an output for the upload instructions.
    evidence = _code(EVIDENCE_MODULE)
    assert re.search(
        r"resource contractsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@[^']+'", evidence
    )
    assert "output contractsContainerName string = contractsContainer.name" in evidence
    assert "contractsContainerName: contractsContainerName" in main
    assert "output contractsContainerName string = evidenceStorage.outputs.contractsContainerName" in main
    for token in (
        "contractsContainerName",
        "contractsBlobName",
        "ContractsFileNotFoundError",
        "ContractsInvalidError",
        "contracts.loaded",
    ):
        assert token in BICEP_README, f"{token} is not documented in infra/bicep/README.md"
    # The upload instruction names the one role the uploader needs, on the contracts container only.
    assert "--assignee-object-id" in BICEP_README
    assert "/containers/$CONTAINER" in BICEP_README
    assert (
        'az storage blob upload --account-name "$(out evidenceStorageAccountName)" --container-name contracts'
        in BICEP_README
    )
    # Only the upgrade notes may still name what was removed.
    upgrade_notes = "### Upgrading from the Azure Files template"
    readme = BICEP_README.split(upgrade_notes)[0] + BICEP_README.split(upgrade_notes)[1].split("\n## ", 1)[1]
    for removed in (
        "estate-contracts",
        "CONTRACTS_JSON",
        "ORQENTIS_CONTRACTS_PATH",
        "contractsPath",
        "contractsContent",
    ):
        assert removed not in readme, f"infra/bicep/README.md still mentions {removed} outside the upgrade notes"
    assert "Azure Files" not in readme.replace("There is no Azure Files share, no SQLite file", "")


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
        "ORQENTIS_AUDIT_EPOCH_CONTAINER": "evidenceStorage.outputs.auditEpochContainerName",
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
    validate = (REPO / "scripts" / "validate-bicep.sh").read_text(encoding="utf-8")
    snapshot = (REPO / "scripts" / "bicep-snapshot.sh").read_text(encoding="utf-8")  # the pull-request Bicep diff
    for name in sorted(required):
        assert f"export {name}=" in validate, f"scripts/validate-bicep.sh has no placeholder for {name}"
        assert f"export {name}=" in snapshot, f"scripts/bicep-snapshot.sh has no placeholder for {name}"
        assert name in deploy, f"deploy-environment.yml does not supply {name}"
    # Identifiers come from GitHub variables, never from secrets.
    assert "POSTGRES_ENTRA_ADMIN_OBJECT_ID: ${{ vars.POSTGRES_ENTRA_ADMIN_OBJECT_ID }}" in deploy
    assert "POSTGRES_ENTRA_ADMIN_NAME: ${{ vars.POSTGRES_ENTRA_ADMIN_NAME }}" in deploy
    assert "CONTRACTS_JSON" not in deploy
    assert not re.search(r"secrets\.\w", deploy)


def test_the_deploy_identity_may_assign_the_storage_role_the_template_grants() -> None:
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    assert STORAGE_BLOB_DATA_CONTRIBUTOR in delivery, "the RBAC Administrator condition must allow the storage role"
    assert STORAGE_BLOB_DATA_READER in delivery, "... and the read-only role on the contracts container"
    condition = delivery.split("CONDITION=")[1].split("\n")[0]
    assert "${STORAGE_BLOB_DATA_READER}" in condition
    # The optional audit operators are an Entra group: the constrained administrator must be allowed to assign to one.
    assert "{'ServicePrincipal', 'Group'}" in condition
    assert "2a2b9908-6b94-4b4a" not in delivery, "the wrong Storage Blob Data Reader id must not come back"
    assert STORAGE_BLOB_DATA_CONTRIBUTOR in EVIDENCE_MODULE
    assert STORAGE_BLOB_DATA_READER in EVIDENCE_MODULE
    assert "Microsoft.DBforPostgreSQL" in delivery, "the resource provider must be registered once"


# --------------------------------------------------------------------------------------------- private networking (M4)


def test_test_and_prod_use_private_networking_and_never_open_postgres_to_azure() -> None:
    private = {
        "infrastructureSubnetId": "CONTAINER_APPS_SUBNET_ID",
        "postgresDelegatedSubnetId": "POSTGRES_DELEGATED_SUBNET_ID",
        "postgresPrivateDnsZoneId": "POSTGRES_PRIVATE_DNS_ZONE_ID",
    }
    for environment in ("test", "prod"):
        text = ENV_FILES[environment]
        code = _code(text)
        assert "postgresAllowAzureServices" not in code, (
            f"{environment}: Allow Azure services opens 5432 to every tenant"
        )
        assert "postgresAllowedIpRanges" not in code, f"{environment}: no firewall rule, the server is private"
        assert _param_value(text, "ingressMode") == "'externalAuthenticated'", (
            "the API keeps its authenticated endpoint"
        )
        for param, variable in private.items():
            assert _param_value(text, param) == f"readEnvironmentVariable('{variable}')", f"{environment}: {param}"
    # dev is the only environment with public access.
    assert _param_value(ENV_FILES["dev"], "postgresAllowAzureServices") == "true"
    assert _param_value(ENV_FILES["dev"], "infrastructureSubnetId") == "''"
    assert "postgresDelegatedSubnetId" not in _code(ENV_FILES["dev"])


def test_main_bicep_rejects_a_public_database_outside_dev() -> None:
    main = _code(BICEP)
    check = main.split("var storageCheck")[1].split("// The PostgreSQL role is the reader identity")[0]
    assert (
        "deploymentEnvironment != 'dev' && (empty(postgresDelegatedSubnetId) || empty(postgresPrivateDnsZoneId))"
        in check
    )
    assert "deploymentEnvironment != 'dev' && empty(infrastructureSubnetId)" in check
    assert "deploymentEnvironment != 'dev' && postgresAllowAzureServices" in check
    assert "postgresDelegatedSubnetId == infrastructureSubnetId" in check, "the two subnets are different subnets"
    assert "public access with a firewall is for dev only" in check
    # The module that builds the checked sentence is what makes the deployment fail, and everything waits for it.
    assert re.search(r"result: any\(storageCheck\)", main)
    # externalAuthenticated + a subnet is a VNet-integrated environment with a public endpoint (not an error any more).
    assert "(effectiveIngressMode == 'externalAuthenticated' && !empty(infrastructureSubnetId))" not in main
    assert "internal: effectiveIngressMode != 'externalAuthenticated'" in main
    # The README says what to provision, and the delivery guide how.
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    for document in (BICEP_README, delivery):
        for token in ("snet-apps", "snet-postgres", "Private DNS Zone Contributor", "Network Contributor"):
            assert token in document or document is BICEP_README, f"{token} is not in docs/delivery.md"
    for token in (
        "Private networking",
        "VNet integration",
        "private endpoint",
        "snet-postgres",
        "virtual network link",
    ):
        assert token in BICEP_README, f"{token} is not in infra/bicep/README.md"
    for variable in ("CONTAINER_APPS_SUBNET_ID", "POSTGRES_DELEGATED_SUBNET_ID", "POSTGRES_PRIVATE_DNS_ZONE_ID"):
        assert variable in delivery
        assert variable in (WORKFLOWS / "deploy-environment.yml").read_text(encoding="utf-8")


def test_every_parameter_file_is_private_unless_it_is_a_dev_deployment() -> None:
    """A file that does not say dev is a prod deployment (the template's default), so it must be private."""
    checked = 0
    for path in sorted(INFRA.rglob("*.bicepparam")):
        text = path.read_text(encoding="utf-8")
        if (_param_value(text, "deploymentEnvironment") or "'prod'") == "'dev'":
            continue
        checked += 1
        code = _code(text)
        assert "postgresAllowAzureServices" not in code, path.name
        for param in ("infrastructureSubnetId", "postgresDelegatedSubnetId", "postgresPrivateDnsZoneId"):
            assert _param_value(text, param) not in (None, "''"), f"{path.name} does not set {param}"
    assert checked >= 5, "main.bicepparam, test, prod and the examples that show a prod deployment"


# --------------------------------------------------------------------------------------------- bootstrap script (m2)

BOOTSTRAP_SQL = (REPO / "scripts" / "postgres-bootstrap.sql").read_text(encoding="utf-8")


def test_the_bootstrap_script_verifies_the_server_and_the_principal_and_gives_public_nothing() -> None:
    # A token travels over this connection: the certificate is verified everywhere psql is shown.
    delivery = (REPO / "docs" / "delivery.md").read_text(encoding="utf-8")
    for name, text in (("script", BOOTSTRAP_SQL), ("README", BICEP_README), ("delivery.md", delivery)):
        assert "sslmode=require" not in text, f"{name} shows sslmode=require"
    assert "sslmode=verify-full" in BOOTSTRAP_SQL
    assert "sslmode=verify-full sslrootcert=system" in BICEP_README
    assert "DigiCert Global Root G2" in BICEP_README, "the README carries the Azure CA note"
    # The role check matches the Entra principal (name AND object id), not just a role of that name.
    assert "pgaadauth_list_principals(false)" in BOOTSTRAP_SQL
    assert "strpos(lower(p::text), lower(:'app_oid'))" in BOOTSTRAP_SQL
    assert "\\if :name_taken" in BOOTSTRAP_SQL, "a plain role of the same name stops the script"
    # PUBLIC keeps nothing extra, and the application gets only CONNECT on the database.
    assert "REVOKE ALL ON DATABASE %I FROM PUBLIC" in BOOTSTRAP_SQL
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in BOOTSTRAP_SQL
    assert 'GRANT CONNECT ON DATABASE :"app_db" TO :"app_role";' in BOOTSTRAP_SQL
    assert "GRANT ALL" not in BOOTSTRAP_SQL
    statements = "\n".join(line for line in BOOTSTRAP_SQL.splitlines() if not line.lstrip().startswith("--")).upper()
    assert not re.search(r"(ALTER|CREATE) ROLE", statements), "the script creates the principal, never a plain role"
    # The operators block is optional (skipped without the variable) and grants without inheritance.
    optional = BOOTSTRAP_SQL.split("\\if :{?operator_role}")[1]
    assert "WITH INHERIT FALSE, SET TRUE" in optional
    before = "\n".join(
        line for line in BOOTSTRAP_SQL.split("\\if :{?operator_role}")[0].splitlines() if not line.startswith("--")
    )
    assert "INHERIT FALSE, SET TRUE" not in before, "only the optional block grants the role to an operators group"
    # Every step that creates something checks first, so the script can be run again.
    assert BOOTSTRAP_SQL.count("WHERE NOT") + BOOTSTRAP_SQL.count("WHERE EXISTS") >= 5
    for token in ("operator_role", "ORQENTIS_POSTGRES_SET_ROLE", "REVOKE", "pgaadauth_list_principals"):
        assert token in BICEP_README or token == "REVOKE", f"{token} is not explained in the README"
    assert "PUBLIC" in BICEP_README
