#!/usr/bin/env bash
# Offline check of the two ways the alerts reach people (docs/operations.md, "Alert receivers"). No Azure access.
#
#   bash scripts/check-alert-action-group.sh
#
# 1. As committed, the template creates its own action group and every alert uses it.
# 2. With ALERT_ACTION_GROUP_ID set (a separately managed action group), the template creates no action group at all and
#    every alert uses that one, for dev, test and prod.
# 3. A malformed id, and an id together with receivers, stop the deployment with a readable reason. Receivers given next to
#    a separately managed group would otherwise be ignored without a word.
# It evaluates the templates with `bicep snapshot` through scripts/bicep-snapshot.sh (same Bicep CLI, fixed placeholders).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Git Bash on Windows rewrites arguments that look like POSIX paths, such as the resource id below.
export MSYS_NO_PATHCONV=1
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

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

external="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-example/providers/Microsoft.Insights/actionGroups/ag-managed-elsewhere"

bash scripts/bicep-snapshot.sh "$work/default" > /dev/null
SNAPSHOT_ALERT_ACTION_GROUP_ID="$external" bash scripts/bicep-snapshot.sh "$work/external" > /dev/null
# A native Windows Python does not understand the MSYS temporary directory; cygpath (Git Bash only) translates it.
native() {
  if command -v cygpath > /dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}
"$python_bin" -I scripts/check_action_group_snapshots.py "$(native "$work/default")" "$(native "$work/external")" "$external"

# expect_refusal <text the failure must contain> <NAME=value>...: the snapshot must fail, and say why.
expect_refusal() {
  local needle="$1"
  shift
  local log="$work/refusal.log"
  if env "$@" bash scripts/bicep-snapshot.sh "$work/refused" > "$log" 2>&1; then
    echo "expected the deployment to be refused (${needle}), but the snapshot succeeded" >&2
    exit 1
  fi
  if ! grep -q -- "$needle" "$log"; then
    echo "refused, but not for the expected reason (${needle}):" >&2
    cat "$log" >&2
    exit 1
  fi
}
expect_refusal "must be the resource id of an action group" SNAPSHOT_ALERT_ACTION_GROUP_ID="not-a-resource-id"
expect_refusal "the receivers belong to that action group" \
  SNAPSHOT_ALERT_ACTION_GROUP_ID="$external" SNAPSHOT_ALERT_EMAIL_RECEIVERS="oncall@example.com"
echo "malformed ids, and receivers next to a separately managed action group, are refused with a readable reason"
