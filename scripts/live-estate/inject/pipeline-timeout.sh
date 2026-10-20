#!/usr/bin/env bash
# Fault: the notebook activity gets a 10 second timeout, far shorter than the notebook needs, so the
# activity times out and the pipeline run fails. --revert restores the 30 minute timeout.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
inject_context

if ((REVERT)); then
  apply_pipeline "$WS" "$TRANSFORM" "$DEFS_DIR/ingest_customers.pipeline.json" 0.00:30:00 >/dev/null
else
  apply_pipeline "$WS" "$TRANSFORM" "$DEFS_DIR/ingest_customers.pipeline.json" 0.00:00:10 >/dev/null
fi
report "pipeline timeout (activity timeout 10s)"
if ((RUN)); then run_pipeline; fi
