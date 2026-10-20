#!/usr/bin/env bash
# Read-only inventory of the live sandbox estate: capacity state, the four workspaces (ids, capacity, the test
# principal's role), their items, and the fake connection. Prints ids, so keep the output out of the repository.
#
#   scripts/live-estate/status.sh
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

echo "capacity $CAPACITY_NAME: $(capacity_state)"
for name in "${SANDBOX_WORKSPACES[@]}"; do
  id=$(workspace_id "$name")
  if [[ -z $id ]]; then
    echo "workspace '$name': absent"
    continue
  fi
  capacity=$(fab GET "/workspaces/$id" --query "capacityId || '-'" -o tsv)
  role=$(fab GET "/workspaces/$id/roleAssignments" --query "value[?principal.id=='$TEST_SP_OBJECT_ID'].role | [0] || '-'" -o tsv)
  echo "workspace '$name': $id capacity=$([[ $capacity == "$CAPACITY_ID" ]] && echo sandbox || echo "$capacity") test-principal-role=$role"
  fab GET "/workspaces/$id/items" --query "sort_by(value, &displayName)[].[type, displayName, id]" -o tsv | sed 's/^/    /'
done
connection=$(fab_find "/connections" "$CONNECTION_NAME")
echo "connection '$CONNECTION_NAME': ${connection:-absent}"
