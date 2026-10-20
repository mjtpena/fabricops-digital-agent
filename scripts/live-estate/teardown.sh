#!/usr/bin/env bash
# Remove the FabricOps live sandbox estate. Idempotent: anything already gone is skipped.
#
#   scripts/live-estate/teardown.sh [--dry-run]
#
# Deletes ONLY: the four workspaces named in config.env(.example) (and, with them, every item inside),
# and the connection fabricops-sandbox-fake-source. A workspace is deleted only if its name is on that
# list AND it is assigned to the sandbox capacity. The capacity itself is never touched (use
# capacity.sh suspend to pause it), and no other workspace, item or connection is read or changed.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

dry_run=0
case "${1:-}" in
  --dry-run) dry_run=1 ;;
  '') ;;
  -h | --help)
    sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
    ;;
  *) die "unknown argument: $1" ;;
esac

assert_tenant

for name in "${SANDBOX_WORKSPACES[@]}"; do
  id=$(workspace_id "$name")
  if [[ -z $id ]]; then
    log "workspace '$name' does not exist; skipping"
    continue
  fi
  capacity=$(fab GET "/workspaces/$id" --query "capacityId || '-'" -o tsv)
  if [[ $capacity != "$CAPACITY_ID" ]]; then
    die "refusing to delete '$name' ($id): it is not assigned to the sandbox capacity"
  fi
  if ((dry_run)); then
    log "[dry run] would delete workspace '$name' ($id)"
  else
    log "deleting workspace '$name' ($id)"
    fab DELETE "/workspaces/$id" >/dev/null
  fi
done

assert_sandbox_connection "$CONNECTION_NAME"
connection=$(fab_find "/connections" "$CONNECTION_NAME")
if [[ -z $connection ]]; then
  log "connection '$CONNECTION_NAME' does not exist; skipping"
elif ((dry_run)); then
  log "[dry run] would delete connection '$CONNECTION_NAME' ($connection)"
else
  log "deleting connection '$CONNECTION_NAME' ($connection)"
  fab DELETE "/connections/$connection" >/dev/null
fi

if ((dry_run)); then exit 0; fi
rm -f "$LIVE_DIR/contracts.sandbox.json"
log "teardown complete (the capacity is untouched; pause it with capacity.sh suspend if it is idle)"
