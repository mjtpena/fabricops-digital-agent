# ADR-0014: Declared environment classification, the contracts schema and portable identity

* Status: Accepted (production programme, WS2)
* Date: 2026-10-20

## Context

FabricOps has to be deployable by someone else, into a fresh resource group and a different Fabric
tenant, from the docs, one parameter file and a contracts file. Five things stood in the way:

1. **PROD, TEST and DEV were guessed from workspace names** (`Sales [PROD]`, `finance-uat`). The
   environment drives incident severity (PROD is HIGH) and policy rules such as `POL-002`, and there
   was no way to correct a wrong guess.
2. **The contracts file** (external sources, data products, dependencies, SLAs) had to be written
   by hand with no schema and no checks. A typo made startup fail, and a failing container is
   restarted by Container Apps in a loop.
3. **Token scopes and the Entra authority were the public cloud's.** Sovereign clouds could not be
   configured.
4. **`default_timezone` was not exposed in Bicep**, and an invalid value only failed when first used.
5. **A managed identity cannot be granted access in another Entra tenant.** `service_principal`
   mode existed, but the template had no way to supply its credential without a secret in a
   parameter.

## Decision

* **Environments are declared, then inferred.** The contracts file gets an optional
  `environments` section: entries select a workspace by `workspace_id`, workspaces by
  `workspace_name_pattern`, or one item by `item_id`, and state its environment. Precedence for a
  workspace is id, then the first matching pattern in file order, then name inference. Items take
  their workspace's resolved environment unless an `item_id` entry says otherwise. The resolver is
  `topology/environment.py` and the REST adapter calls it where it used to call `infer_environment`,
  so severity, `POL-002` and every other consumer of `environment` see the resolved value without
  further change. Patterns are `*`/`?` globs matched by a linear algorithm; `[PROD]` is literal,
  because that is how people name workspaces and a regex or `fnmatch` would read it as a character
  class. A workspace's metadata records `environment_source` when a declaration decided it.
  Mock fixtures are unchanged (they carry explicit environments). Deployment-pipeline stage
  environments keep their position-based rule (first stage DEV, last PROD), and capacity environments
  remain name-inferred; neither is declarable yet.
* **The contracts file has a committed JSON Schema** (`schemas/contracts.schema.json`) generated from
  the Pydantic model `EstateContracts` (`contracts/model.py`; `extra="forbid"` throughout). A test
  fails when the file drifts from the model, and another proves the schema and the model accept and
  reject the same environment selectors. `"$schema"` is an allowed key, so editors can validate and
  complete a contracts file.
* **Loading and validation share one implementation** (`contracts/validate.py`).
  `load_contracts` raises a typed `ContractsError` (`ContractsNotConfiguredError`,
  `ContractsFileNotFoundError`, `ContractsInvalidError`) whose message names the path and lists every
  violation as `path:line:column: error: /json/pointer: message`. Messages carry ids and positions, never
  values from the file. Beyond the schema it rejects duplicate ids, duplicate edges (the dependency store
  has a unique index on source, target and type, which would otherwise surface as a crash later),
  dangling references inside the file, invalid IANA time zones and weekday lists. It is the application
  that decides to stay up or not; this change supplies a precise error instead of a stack trace.
  `orqentis contracts validate` adds checks against a *catalog* (workspaces and items that exist) and a
  `--strict` mode that turns warnings (unreviewed drafts) into failures, for CI.
* **`orqentis contracts scaffold` drafts a file from a live read-only listing.** It uses the same
  read-only `FabricClient` port as the application: workspaces become `environments` entries marked
  `inferred`; `TRIGGERS` (notebooks, pipelines) and `REFRESHES` (semantic models) edges come from the
  `target_item_id` of recent pipeline activity runs; external sources, data products and SLAs are
  placeholders marked `metadata.draft`, because Fabric cannot say who owns a table or when it must be
  fresh. Activity targets that are lakehouses or warehouses are left out (a copy activity may read or
  write them) and reported. The output is a draft: it is never applied, never overwrites without
  `--force`, and `validate` warns about every unreviewed entry until a human edits it.
* **The admin Scanner API is an interactive, human-only option** (`scaffold --scanner`). It adds
  report, semantic-model and data-source lineage, needs a signed-in Fabric administrator
  (`InteractiveBrowserCredential`), refuses to run without a terminal, and is not an `AuthMode`. The
  application never imports it; a test checks that importing the API application does not load it.
  Data source descriptions drop credentials, user info and query strings.
* **Sovereign clouds are configuration.** `ORQENTIS_AUTHORITY_HOST` (https host only) is passed as
  `authority` to every azure-identity credential that talks to Entra ID (`DefaultAzureCredential`,
  `WorkloadIdentityCredential`, `EnvironmentCredential`; a managed identity has no authority). Unset
  means azure-identity's own default, so the public cloud behaves exactly as before.
  `ORQENTIS_FABRIC_SCOPE` and `ORQENTIS_POWERBI_SCOPE` replace the public scopes inside
  `AzureIdentityTokenProvider`, so the REST adapter keeps asking for its logical scopes and nothing above
  the token provider knows which cloud it is in. The base URLs were already settings and are now Bicep
  parameters too.
* **The time zone is a Bicep parameter and is validated at startup.** `defaultTimezone` (default
  `Australia/Sydney`) becomes `ORQENTIS_DEFAULT_TIMEZONE`; `Settings` rejects anything that is not an IANA
  zone.
* **Cross-tenant Fabric access uses a service principal whose credential is a Key Vault reference.**
  Bicep selects `service_principal` mode and supplies the client id, the Fabric tenant id and either the URI
  of a Key Vault secret (the client secret) or of a Key Vault secret holding a PEM certificate. Container
  Apps resolves it with the reader managed identity and exposes it as `AZURE_CLIENT_SECRET` or as a read-only
  file for `AZURE_CLIENT_CERTIFICATE_PATH`. No secret value is a parameter, an output or a file in the
  repository. An optional module grants the reader identity `Key Vault Secrets User` on the vault. The
  Azure OpenAI call must not use the foreign-tenant service principal, so `ORQENTIS_LLM_AUTH_MODE` lets the
  LLM credential stay a managed identity (Bicep sets it in this mode). The runtime fails fast, naming
  the missing variables, when `service_principal` mode lacks its inputs or has both a secret and a
  certificate.

## Consequences

* A wrong or missing environment guess can be fixed in a reviewed file, and the fix reaches severity and
  policy without code changes. The dangerous direction (declaring a PROD workspace DEV) weakens
  `POL-002`, so the contracts file is reviewed like code and `validate --strict` can gate it in CI. No
  mutation becomes allowed by any of this: `POL-020`/`POL-021` and the code-level invariant still require
  approval in every environment.
* The contracts loader is stricter than before: unknown top-level keys, duplicate ids and edges, dangling
  SLA references and invalid time zones are now errors instead of silently ignored or a later crash.
  Sections of a full estate file that are not contracts (`tenant`, `workspaces`, `items`, ...) are still
  ignored when it is read as a contracts file.
* A first contracts file can be drafted in minutes and then reviewed, instead of written from nothing.
  The draft contains guesses (environments from names, placeholder owners and deadlines), so it is marked
  and `validate` keeps warning until each guess is confirmed.
* A secret-based service principal needs rotation. A keyless alternative exists: an app registration
  in the Fabric tenant with a *federated identity credential that trusts the reader managed identity*,
  used through a client-assertion credential. It removes the Key Vault dependency and the rotation, but
  needs a new auth mode and was not in scope. It is the recommended follow-up.
* Fabric availability differs by cloud. This change makes the endpoints configurable and documents the
  known Power BI sets; it does not claim that Fabric runs in a given sovereign cloud, and no sovereign
  cloud was exercised against a live service.
