#!/usr/bin/env bash
# Fault: the declared SLA deadline moves to 00:00:01 local time, which no run can meet, by regenerating
# contracts.sandbox.json. The SLA evaluator judges a data product MET if its freshness item succeeded in the
# 24 hours before the deadline, so a contracts change alone breaches nothing while an earlier run sits in that
# window. The injector therefore also runs the targeted environment's pipeline once, successfully and AFTER the
# new deadline: that run is "late", and that environment's SLA becomes BREACHED ("Refreshed late at ...").
# The other environments' SLAs keep their earlier successful runs and stay MET.
#
#   --env DEV|TEST|PROD   environment whose pipeline runs (default DEV)
#   --no-run              change the contracts only (breaches only if a run already finished after local midnight)
#   --revert              restore the configured deadline (SLA_DEADLINE in config.env.example, 23:59:00)
#
# The running app picks the new contracts up on its next catalog refresh (or restart); no Fabric item changes.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

if ((REVERT)); then
  render_contracts
  log "reverted the SLA breach: deadline back to $SLA_DEADLINE $SLA_TIMEZONE"
  exit 0
fi

SLA_DEADLINE=00:00:01 render_contracts
log "injected an SLA breach: every declared deadline is now 00:00:01 $SLA_TIMEZONE"
if ((RUN)); then
  inject_context
  run_pipeline
  [[ $JOB_STATUS == Completed ]] || die "the pipeline run did not complete; the SLA will not read as late"
fi
