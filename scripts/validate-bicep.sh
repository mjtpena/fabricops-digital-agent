#!/usr/bin/env bash
# Validate the Bicep template and every parameter file (run by CI; run it before opening a PR that touches infra/).
#
#   bash scripts/validate-bicep.sh
#
# The per-environment parameter files read pipeline-supplied values with readEnvironmentVariable() and have no
# defaults for them, so a forgotten variable fails the deployment instead of deploying a wrong value. This script
# supplies obvious placeholders (never real identifiers) so the files can be compiled without a deployment.
#
# It also asserts, for dev, test and prod: enableWriteActions = false (production writes stay disabled), an explicit
# ingressMode without the deprecated externalIngress, anonymous health endpoints (the smoke test needs them), the
# scheduler on, (test and prod) at least two replicas now that the database serialises every writer, and (test and
# prod) a private PostgreSQL server in a VNet-integrated environment, with no public firewall rule.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export CONTAINER_IMAGE="${CONTAINER_IMAGE:-registry.invalid/orqentis-fabricops@sha256:0000000000000000000000000000000000000000000000000000000000000000}"
export ACR_LOGIN_SERVER="${ACR_LOGIN_SERVER:-registry.invalid}"
export ACR_RESOURCE_ID="${ACR_RESOURCE_ID:-/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ContainerRegistry/registries/crexample}"
export ENTRA_API_CLIENT_ID="${ENTRA_API_CLIENT_ID:-00000000-0000-0000-0000-000000000000}"
export POSTGRES_ENTRA_ADMIN_OBJECT_ID="${POSTGRES_ENTRA_ADMIN_OBJECT_ID:-00000000-0000-0000-0000-000000000000}"
export POSTGRES_ENTRA_ADMIN_NAME="${POSTGRES_ENTRA_ADMIN_NAME:-sg-example-postgres-admins}"
# prod has no default for it: whether the evidence immutability policy is Locked is an explicit decision. Never Locked here.
export EVIDENCE_IMMUTABILITY_LOCKED="${EVIDENCE_IMMUTABILITY_LOCKED:-false}"
# The private network of test and prod (resource ids of the subnets and the private DNS zone, obviously fake here).
export CONTAINER_APPS_SUBNET_ID="${CONTAINER_APPS_SUBNET_ID:-/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example-network/providers/Microsoft.Network/virtualNetworks/vnet-example/subnets/snet-example-apps}"
export POSTGRES_DELEGATED_SUBNET_ID="${POSTGRES_DELEGATED_SUBNET_ID:-/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example-network/providers/Microsoft.Network/virtualNetworks/vnet-example/subnets/snet-example-postgres}"
export POSTGRES_PRIVATE_DNS_ZONE_ID="${POSTGRES_PRIVATE_DNS_ZONE_ID:-/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example-network/providers/Microsoft.Network/privateDnsZones/example.private.postgres.database.azure.com}"

# `python3` is the Microsoft Store stub on Windows; use whichever interpreter actually runs.
python_bin=""
for candidate in "${PYTHON:-}" python3 python; do
  if [[ -n "$candidate" ]] && "$candidate" -c 'import sys' > /dev/null 2>&1; then
    python_bin="$candidate"
    break
  fi
done
if [[ -z "$python_bin" ]]; then
  echo "No working Python interpreter found (set PYTHON)" >&2
  exit 1
fi

out_dir="$(mktemp -d)"
trap 'rm -rf "$out_dir"' EXIT

echo "az bicep build infra/bicep/main.bicep"
az bicep build --file infra/bicep/main.bicep --stdout > /dev/null

shopt -s nullglob
param_files=(infra/bicep/main.bicepparam infra/bicep/examples/*.bicepparam infra/bicep/env/*.bicepparam)
for file in "${param_files[@]}"; do
  echo "az bicep build-params ${file}"
  az bicep build-params --file "$file" --stdout > "${out_dir}/$(basename "$file").json"
done

# prod must not guess whether the immutability policy is Locked (irreversible): a missing or invalid variable has to fail.
if (unset EVIDENCE_IMMUTABILITY_LOCKED; az bicep build-params --file infra/bicep/env/prod.bicepparam --stdout > /dev/null 2>&1); then
  echo "infra/bicep/env/prod.bicepparam compiled without EVIDENCE_IMMUTABILITY_LOCKED: it must have no default" >&2
  exit 1
fi
if (EVIDENCE_IMMUTABILITY_LOCKED=maybe az bicep build-params --file infra/bicep/env/prod.bicepparam --stdout > /dev/null 2>&1); then
  echo "infra/bicep/env/prod.bicepparam accepted EVIDENCE_IMMUTABILITY_LOCKED=maybe: only true and false are valid" >&2
  exit 1
fi

for env in dev test prod; do
  if [[ ! -f "infra/bicep/env/${env}.bicepparam" ]]; then
    echo "missing infra/bicep/env/${env}.bicepparam" >&2
    exit 1
  fi
done

# `build-params --stdout` prints {"parametersJson": "<ARM parameters file as a string>", ...}; the parameters file
# itself is {"parameters": {"enableWriteActions": {"value": false}, ...}}.
"$python_bin" -I - "$out_dir" <<'PY'
import json
import pathlib
import sys

out_dir = pathlib.Path(sys.argv[1])


def parameters(environment: str) -> dict:
    document = json.loads((out_dir / f"{environment}.bicepparam.json").read_text(encoding="utf-8"))
    return json.loads(document["parametersJson"])["parameters"]


def value(params: dict, name: str):
    return params.get(name, {}).get("value")


for environment in ("dev", "test", "prod"):
    params = parameters(environment)
    where = f"infra/bicep/env/{environment}.bicepparam"
    if value(params, "deploymentEnvironment") != environment:
        sys.exit(f"{where} must set deploymentEnvironment = '{environment}'")
    if value(params, "enableWriteActions") is not False:
        sys.exit(f"{where} must set enableWriteActions = false")
    # The template fails a deployment that sets both; the deprecated bool is not used in this repository.
    if "externalIngress" in params:
        sys.exit(f"{where} uses the deprecated externalIngress; set ingressMode instead")
    if value(params, "ingressMode") not in ("externalAuthenticated", "vnet", "internal"):
        sys.exit(f"{where} must set ingressMode explicitly")
    # The post-deployment smoke test calls /health without a token.
    if value(params, "anonymousHealthEndpoints") is not True and value(params, "ingressMode") == "externalAuthenticated":
        sys.exit(f"{where}: the smoke test needs anonymousHealthEndpoints = true")
    if value(params, "schedulerEnabled") is not True:
        sys.exit(f"{where} must keep the autonomous scheduler on (schedulerEnabled = true)")
    # The app is stateless (PostgreSQL serialises the audit chain, the lease and approvals): test and prod run two
    # replicas so a revision change or a node failure does not take the API down. dev may run one.
    minimum, maximum = value(params, "minReplicas"), value(params, "maxReplicas")
    if not (isinstance(minimum, int) and isinstance(maximum, int) and 1 <= minimum <= maximum):
        sys.exit(f"{where} must set minReplicas and maxReplicas explicitly (1 <= minReplicas <= maxReplicas)")
    if environment != "dev" and minimum < 2:
        sys.exit(f"{where} must run at least two replicas (minReplicas >= 2)")
    # Outside dev the database is private: a delegated subnet and a private DNS zone, never the "Allow Azure services"
    # firewall rule or any firewall range, and a VNet-integrated Container Apps environment to reach it.
    if environment != "dev":
        for name in ("postgresDelegatedSubnetId", "postgresPrivateDnsZoneId", "infrastructureSubnetId"):
            if not value(params, name):
                sys.exit(f"{where} must set {name} (private networking is required outside dev)")
        if value(params, "postgresAllowAzureServices") or value(params, "postgresAllowedIpRanges"):
            sys.exit(f"{where} must not open PostgreSQL to public access (postgresAllowAzureServices, postgresAllowedIpRanges)")
    # The PostgreSQL administrator comes from the pipeline variables; the file must not hard-code an identity.
    for name in ("postgresEntraAdminObjectId", "postgresEntraAdminName"):
        if name not in params:
            sys.exit(f"{where} must set {name} (from the POSTGRES_ENTRA_ADMIN_* variables)")
print("env parameter files: deploymentEnvironment matches, enableWriteActions = false everywhere (prod included),")
print("ingressMode explicit (no deprecated externalIngress), anonymous health endpoints, scheduler on,")
print("replicas explicit (test and prod at least two), PostgreSQL administrator set, test and prod private")
PY

echo "Bicep validation passed"
