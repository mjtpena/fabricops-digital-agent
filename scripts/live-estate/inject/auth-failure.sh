#!/usr/bin/env bash
# Fault: the pipeline gains a first activity (a Web activity) that calls its source through the
# connection fabricops-sandbox-fake-source, which holds a deliberately invalid, obviously fake credential.
# The source answers 401 Unauthorized, the activity fails, and the notebook never runs.
# --revert restores the baseline pipeline. The connection itself is created by create.sh and removed by
# teardown.sh; it never holds a real credential.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
inject_context

if ((REVERT)); then
  apply_pipeline "$WS" "$TRANSFORM" "$DEFS_DIR/ingest_customers.pipeline.json" >/dev/null
else
  assert_sandbox_connection "$CONNECTION_NAME"
  connection=$(fab_find "/connections" "$CONNECTION_NAME")
  [[ -n $connection ]] || die "connection $CONNECTION_NAME does not exist; run create.sh first"
  apply_pipeline "$WS" "$TRANSFORM" "$DEFS_DIR/ingest_customers.auth.pipeline.json" 0.00:30:00 "$connection" >/dev/null
fi
report "authentication failure (fake-credential connection)"
if ((RUN)); then run_pipeline; fi
