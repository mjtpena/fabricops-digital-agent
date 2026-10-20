#!/usr/bin/env bash
# Read, resume or suspend (pause) the sandbox capacity. These are the ONLY mutations this repo is
# allowed to make to the capacity. It is shared with non-programme workspaces: pause it only if it
# was paused when you started (check `status` first) and nobody else is using it.
#
#   capacity.sh status          print the state (Active | Paused | ...)
#   capacity.sh resume          start billing (USD 0.84/h for F4) and wait until Active
#   capacity.sh suspend         pause and wait until Paused
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

wait_state() {
  local want=$1 state deadline=$((SECONDS + 600))
  while ((SECONDS < deadline)); do
    state=$(capacity_state)
    [[ $state == "$want" ]] && return 0
    sleep 10
  done
  die "capacity did not reach $want in time (last state: $state)"
}

case "${1:-status}" in
  status)
    printf '%s\n' "$(capacity_state)"
    ;;
  resume)
    log "$(utc_now) resuming $CAPACITY_NAME"
    arm POST "$CAPACITY_ARM_ID/resume?api-version=2023-11-01" >/dev/null
    wait_state Active
    log "$(utc_now) $CAPACITY_NAME is Active"
    ;;
  suspend)
    log "$(utc_now) suspending $CAPACITY_NAME"
    arm POST "$CAPACITY_ARM_ID/suspend?api-version=2023-11-01" >/dev/null
    wait_state Paused
    log "$(utc_now) $CAPACITY_NAME is Paused"
    ;;
  *)
    die "usage: capacity.sh status|resume|suspend"
    ;;
esac
