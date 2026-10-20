#!/usr/bin/env bash
# Shared plumbing for the fault injectors. Source it; do not execute it.
#
# Every injector accepts:
#   --env DEV|TEST|PROD   sandbox environment to fault (default DEV)
#   --revert              restore the baseline instead of injecting
#   --run | --no-run      run the pipeline (or refresh) afterwards to produce an observable failure
#                         (default: run when injecting, do not run when reverting)
# A fault only ever changes sandbox items (or the generated contracts file) and is undone by --revert
# (or by create.sh, which converges notebooks and pipelines back to the baseline).
set -euo pipefail
INJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../lib.sh
source "$INJECT_DIR/../lib.sh"
# shellcheck source=../estate.sh
source "$INJECT_DIR/../estate.sh"

INJECT_ENV=DEV
REVERT=0
RUN=''
while (($#)); do
  case $1 in
    --env)
      INJECT_ENV=$2
      shift 2
      ;;
    --revert)
      REVERT=1
      shift
      ;;
    --run)
      RUN=1
      shift
      ;;
    --no-run)
      RUN=0
      shift
      ;;
    -h | --help)
      sed -n '2,/^set -euo/p' "$INJECT_DIR/_common.sh" | sed '$d; s/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1 (try --help)" ;;
  esac
done
case $INJECT_ENV in DEV | TEST | PROD) ;; *) die "--env must be DEV, TEST or PROD" ;; esac
if [[ -z $RUN ]]; then if ((REVERT)); then RUN=0; else RUN=1; fi; fi

# inject_context: resolve the ids of the targeted environment into WS, LH, SEED, TRANSFORM, PIPELINE.
inject_context() {
  require_capacity_active
  WS=$(workspace_id "$(env_workspace "$INJECT_ENV")")
  [[ -n $WS ]] || die "workspace for $INJECT_ENV does not exist; run create.sh first"
  LH=$(item_id "$WS" Lakehouse "$LAKEHOUSE_NAME")
  SEED=$(item_id "$WS" Notebook "$NOTEBOOK_SEED")
  TRANSFORM=$(item_id "$WS" Notebook "$NOTEBOOK_TRANSFORM")
  PIPELINE=$(item_id "$WS" DataPipeline "$PIPELINE_NAME")
  [[ -n $LH && -n $SEED && -n $TRANSFORM && -n $PIPELINE ]] || die "sandbox items missing in $INJECT_ENV; run create.sh first"
}

# run_pipeline: start ingest_customers, wait, and print "<job id> <status>" on stdout.
run_pipeline() {
  log "running $PIPELINE_NAME in $INJECT_ENV"
  run_job "$WS" "$PIPELINE" Pipeline
  printf 'pipeline run %s: %s\n' "$JOB_ID" "$JOB_STATUS"
}

# report FAULT: one line saying what was done.
report() {
  if ((REVERT)); then log "reverted $1 in $INJECT_ENV"; else log "injected $1 into $INJECT_ENV"; fi
}
