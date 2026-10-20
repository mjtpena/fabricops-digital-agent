#!/usr/bin/env bash
# Fault: the transform notebook raises. The pipeline's notebook activity fails with the notebook's
# exception; the pipeline run fails. --revert restores the baseline notebook.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
inject_context

if ((REVERT)); then
  apply_notebook "$WS" "$LH" "$NOTEBOOK_TRANSFORM" "$DEFS_DIR/transform_customers.ipynb" >/dev/null
else
  apply_notebook "$WS" "$LH" "$NOTEBOOK_TRANSFORM" "$DEFS_DIR/transform_customers.fail.ipynb" >/dev/null
fi
report "notebook failure ($NOTEBOOK_TRANSFORM raises)"
if ((RUN)); then run_pipeline; fi
