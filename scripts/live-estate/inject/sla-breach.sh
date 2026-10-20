#!/usr/bin/env bash
# Fault (contracts only, nothing in Fabric changes): the declared SLA deadline moves to 00:00:01 local
# time, which no run can meet, by regenerating contracts.sandbox.json. Restart the app (or rerun
# /telemetry/collect after a restart) so it reloads the contracts. --revert restores the configured
# deadline (SLA_DEADLINE in config.env.example, 23:59:00 by default).
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if ((REVERT)); then
  render_contracts
else
  SLA_DEADLINE=00:00:01 render_contracts
fi
if ((REVERT)); then log "reverted the SLA breach: deadline back to $SLA_DEADLINE $SLA_TIMEZONE"; else
  log "injected an SLA breach: every declared deadline is now 00:00:01 $SLA_TIMEZONE (restart the app to load it)"; fi
