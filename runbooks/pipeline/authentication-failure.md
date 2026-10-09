---
id: RB-AUTH-FAILURE-001
version: "1.1.0"
title: Connection authentication failure (expired or invalid credential)
category: AUTH_FAILURE
severity: HIGH
signals:
  - "AADSTS\\d+"
  - "client secret"
  - "expired"
  - "invalid_client"
  - "unauthori[sz]ed|forbidden"
  - "failed to acquire token"
applies_to: [DataPipeline, SemanticModel, ExternalSource]
risk: MEDIUM_RISK
approval_required: true
owner: platform-reliability@contoso.com
last_reviewed: 2026-08-30
remediation_actions:
  - action_type: UPDATE_CONNECTION_CREDENTIAL
    title: Rotate the expired credential and update the Fabric connection
    target: upstream_source
    parameters:
      secret_store: "Azure Key Vault (never paste secrets into tickets or chat)"
  - action_type: RERUN_PIPELINE
    title: Rerun the pipeline after the credential is updated
    target: primary_item
    idempotent: true
---
# Connection authentication failure (expired or invalid credential)

## Symptoms
- Copy or lookup activity fails before reading any rows.
- Error contains an Entra ID code (`AADSTS7000222` expired secret, `AADSTS700016` unknown app, `AADSTS50126` invalid credentials) or HTTP 401/403.
- Every run that uses the same connection fails from the same point in time.

## Diagnostic Steps
1. Extract the AADSTS code, application (client) id and connection id from the error.
2. Check the credential expiry recorded for the connection's service principal.
3. Confirm no other pipelines using the same connection succeeded after the failure time.
4. Check Entra ID sign-in logs for the service principal for the exact failure reason.

## Possible Causes
- Client secret or certificate expired (most common; check the rotation calendar).
- Secret rotated in Key Vault but the Fabric connection was not updated.
- Service principal disabled, deleted, or its permissions on the source revoked.
- Conditional Access policy change blocking the service principal.

## Recommended Remediation
1. Issue a new client secret (or certificate) for the service principal and store it in Key Vault.
2. Update the Fabric connection credential from Key Vault. This is a production change and needs approval.
3. Rerun the failed pipeline.
4. Add the new expiry date to the credential rotation calendar and alerting.

## Validation
- A test connection from the Fabric connection settings succeeds.
- The pipeline copy activity reads the expected number of rows.
- Downstream refreshes complete.

## Rollback
- Re-enable the previous credential if it is still valid, or revert the connection to the previous Key Vault secret version.

## Escalation
- Identity / Entra ID administrators for service principal issues.
- Source system owner when permissions on the source were revoked.
