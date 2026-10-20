#!/usr/bin/env bash
# Validate the Bicep template and every parameter file (run by CI; run it before opening a PR that touches infra/).
#
#   bash scripts/validate-bicep.sh
#
# The per-environment parameter files read pipeline-supplied values with readEnvironmentVariable() and have no
# defaults for them, so a forgotten variable fails the deployment instead of deploying a wrong value. This script
# supplies obvious placeholders (never real identifiers) so the files can be compiled without a deployment.
#
# It also asserts the production safety setting: prod.bicepparam must keep enableWriteActions = false.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

export CONTAINER_IMAGE="${CONTAINER_IMAGE:-registry.invalid/orqentis-fabricops@sha256:0000000000000000000000000000000000000000000000000000000000000000}"
export ACR_LOGIN_SERVER="${ACR_LOGIN_SERVER:-registry.invalid}"
export ACR_RESOURCE_ID="${ACR_RESOURCE_ID:-/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ContainerRegistry/registries/crexample}"
export ENTRA_API_CLIENT_ID="${ENTRA_API_CLIENT_ID:-00000000-0000-0000-0000-000000000000}"

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
param_files=(infra/bicep/main.bicepparam infra/bicep/env/*.bicepparam)
for file in "${param_files[@]}"; do
  echo "az bicep build-params ${file}"
  az bicep build-params --file "$file" --stdout > "${out_dir}/$(basename "$file").json"
done

for env in dev test prod; do
  if [[ ! -f "infra/bicep/env/${env}.bicepparam" ]]; then
    echo "missing infra/bicep/env/${env}.bicepparam" >&2
    exit 1
  fi
done

# `build-params --stdout` prints {"parametersJson": "<ARM parameters file as a string>", ...}; the parameters file
# itself is {"parameters": {"enableWriteActions": {"value": false}, ...}}.
"$python_bin" -I - "${out_dir}/prod.bicepparam.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    parameters = json.loads(json.load(handle)["parametersJson"])["parameters"]
if parameters.get("enableWriteActions", {}).get("value") is not False:
    sys.exit("infra/bicep/env/prod.bicepparam must set enableWriteActions = false")
if parameters.get("deploymentEnvironment", {}).get("value") != "prod":
    sys.exit("infra/bicep/env/prod.bicepparam must set deploymentEnvironment = 'prod'")
print("prod.bicepparam: enableWriteActions = false")
PY

echo "Bicep validation passed"
