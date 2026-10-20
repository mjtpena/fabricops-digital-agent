#!/usr/bin/env bash
# Offline snapshot of what every environment would deploy, for the pull-request Bicep diff.
#
#   bash scripts/bicep-snapshot.sh <output dir> [repository root]
#
# For each of infra/bicep/env/{dev,test,prod}.bicepparam in <repository root> (default: the checkout this script is in) it
# runs `bicep snapshot` and writes <output dir>/<env>.json. A snapshot is the template evaluated against its parameters
# with NO Azure access and no credentials: it lists the predicted resources with their properties and the outputs. Every
# input that the pipeline would supply at deploy time (image, registry, API client id, subscription, tenant) is a fixed,
# obviously fake placeholder, identical for every run, so two snapshots differ only because the Bicep or the parameter
# files differ. Use a Bicep CLI that has the `snapshot` command (0.45.15 is what CI pins); it ships in the binary that
# `az bicep install` downloads, but is not an `az bicep` subcommand.
#
# A repository root without an environment file (for example a merge base from before the file existed) is not an error:
# that environment is skipped and the diff reports it as new.
set -euo pipefail

output_dir="${1:?usage: bicep-snapshot.sh <output dir> [repository root]}"
root="${2:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
# Work from the repository root with relative paths, so a native Windows Bicep binary never sees an MSYS path.
cd "$root"

# Git Bash on Windows rewrites arguments and variables that look like POSIX paths (the resource ids below).
export MSYS_NO_PATHCONV=1

bicep_bin=""
for candidate in "$(command -v bicep 2> /dev/null || true)" "$HOME/.azure/bin/bicep" "$HOME/.azure/bin/bicep.exe"; do
  if [[ -n "$candidate" && -x "$candidate" ]]; then
    bicep_bin="$candidate"
    break
  fi
done
if [[ -z "$bicep_bin" ]]; then
  echo "No Bicep CLI found. Install one with: az bicep install --version v0.45.15" >&2
  exit 1
fi
if ! "$bicep_bin" snapshot --help > /dev/null 2>&1; then
  echo "This Bicep CLI ($("$bicep_bin" --version)) has no 'snapshot' command; use v0.45.15 or newer" >&2
  exit 1
fi

# The values readEnvironmentVariable() reads in infra/bicep/env/*.bicepparam, fixed on purpose.
export CONTAINER_IMAGE="registry.invalid/orqentis-fabricops@sha256:0000000000000000000000000000000000000000000000000000000000000000"
export ACR_LOGIN_SERVER="registry.invalid"
export ACR_RESOURCE_ID="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.ContainerRegistry/registries/crexample"
export ENTRA_API_CLIENT_ID="00000000-0000-0000-0000-000000000001"
unset FABRIC_MODE

subscription_id="00000000-0000-0000-0000-000000000000"
tenant_id="00000000-0000-0000-0000-000000000002"

for environment in dev test prod; do
  parameters="infra/bicep/env/${environment}.bicepparam"
  if [[ ! -f "$parameters" ]]; then
    echo "skip ${environment}: ${parameters} does not exist in ${root}"
    continue
  fi
  echo "snapshot ${environment}"
  "$bicep_bin" snapshot "$parameters" --mode overwrite \
    --subscription-id "$subscription_id" --tenant-id "$tenant_id" \
    --resource-group "rg-fabricops-${environment}" --location australiaeast
  # The snapshot is written next to the parameter file; move it out so the tree stays clean.
  mv "infra/bicep/env/${environment}.snapshot.json" "${output_dir}/${environment}.json"
done
