#!/usr/bin/env bash
# Fault: the upstream source renames a column (customer_id -> client_id) in the Delta table `customers`.
# transform_customers still selects customer_id, so the notebook activity and the pipeline fail with an
# unresolved-column error. --revert rewrites the table with the original schema.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
inject_context

if ((REVERT)); then
  seed_customers "$WS" "$SEED" customer_id
else
  seed_customers "$WS" "$SEED" client_id
fi
report "schema drift (customers.customer_id <-> client_id)"
if ((RUN)); then run_pipeline; fi
