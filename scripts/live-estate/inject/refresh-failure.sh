#!/usr/bin/env bash
# Fault: customers_model (Consumer workspace) is repointed at a Delta table that does not exist, so its
# next refresh fails. --env is accepted for symmetry but the model lives in the Consumer workspace.
# --revert restores the baseline model. --run starts a refresh and waits for it.
# shellcheck source=_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"
require_capacity_active

consumer=$(workspace_id "$WS_CONSUMER")
prod=$(workspace_id "$WS_PROD")
[[ -n $consumer && -n $prod ]] || die "sandbox workspaces missing; run create.sh first"
prod_lh=$(item_id "$prod" Lakehouse "$LAKEHOUSE_NAME")
model=$(item_id "$consumer" SemanticModel "$MODEL_NAME")
[[ -n $prod_lh && -n $model ]] || die "sandbox items missing; run create.sh first"

if ((REVERT)); then
  apply_semantic_model "$consumer" "$prod" "$prod_lh" >/dev/null
else
  # Fabric refuses to import a Direct Lake model that points at a missing lakehouse and will not convert
  # a Direct Lake partition to import, so the fault is a partition that points at a Delta table that does
  # not exist: the definition imports, the refresh (framing) always fails.
  apply_semantic_model "$consumer" "$prod" "$prod_lh" "$DEFS_DIR/customers_model.refresh-fail.bim" >/dev/null
fi
log "$([[ $REVERT == 1 ]] && echo reverted || echo injected) refresh failure on $MODEL_NAME"
if ((RUN)); then
  refresh_model "$consumer" "$model"
  printf 'refresh: %s\n' "$REFRESH_STATUS"
fi
