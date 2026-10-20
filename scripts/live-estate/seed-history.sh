#!/usr/bin/env bash
# Leave one failed pipeline run of each kind in the TEST sandbox pipeline's run history, plus a baseline
# success, so the live tests (tests/live) and the live end-to-end walk-through have real failures to read:
#
#   notebook failure (the notebook raises)   -> activity Failed, nested RuntimeError text
#   pipeline timeout (10 s activity timeout) -> activity TimedOut, empty error text
#   auth-style failure (fake-credential Web connection) -> activity Failed, HTTP 401 from the source
#
# Each fault is reverted afterwards, and a final success run proves the revert. Job history is kept by
# Fabric for 30 days, so re-run this when those runs have aged out. Takes about 8 minutes.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
for fault in notebook-failure pipeline-timeout auth-failure; do
  "$DIR/inject/$fault.sh" --env TEST
  "$DIR/inject/$fault.sh" --env TEST --revert
done
"$DIR/inject/notebook-failure.sh" --env TEST --revert --run
