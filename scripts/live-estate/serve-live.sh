#!/usr/bin/env bash
# Run the FabricOps API locally in rest mode against the live sandbox, as the least-privilege test
# service principal (workspace Viewer on the four sandbox workspaces; no write role anywhere).
#
#   scripts/live-estate/serve-live.sh [--port 8000]
#
# Credentials come from the owner-provided env file (AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET;
# default ~/.fabricops/live-reader.env, override with FABRICOPS_LIVE_ENV_FILE). They are exported into this
# process only: never printed, logged or written to the repository. State goes to var/live (git-ignored).
# Needs contracts.sandbox.json (create.sh, or create.sh --contracts-only) and the capacity running.
set -euo pipefail
LIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$LIVE_DIR/../.." && pwd)"
ENV_FILE="${FABRICOPS_LIVE_ENV_FILE:-$HOME/.fabricops/live-reader.env}"
[[ -f $ENV_FILE ]] || {
  echo "no credentials file at $ENV_FILE" >&2
  exit 1
}
[[ -f $LIVE_DIR/contracts.sandbox.json ]] || {
  echo "missing contracts.sandbox.json; run scripts/live-estate/create.sh --contracts-only" >&2
  exit 1
}
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

export ORQENTIS_FABRIC_MODE=rest
export ORQENTIS_TENANT_ID="${AZURE_TENANT_ID:?AZURE_TENANT_ID missing from the env file}"
export ORQENTIS_AUTH_MODE=service_principal
export ORQENTIS_CONTRACTS_PATH="$LIVE_DIR/contracts.sandbox.json"
export ORQENTIS_DATA_DIR="${ORQENTIS_DATA_DIR:-$REPO/var/live}"
export ORQENTIS_CLOCK=system
export ORQENTIS_DEFAULT_TIMEZONE=Australia/Sydney
export ORQENTIS_LOG_FORMAT="${ORQENTIS_LOG_FORMAT:-console}"

cd "$REPO"
if [[ -x .venv/Scripts/orqentis.exe ]]; then BIN=.venv/Scripts/orqentis.exe; else BIN=.venv/bin/orqentis; fi
exec "$BIN" serve "$@"
