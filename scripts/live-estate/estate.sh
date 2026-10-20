#!/usr/bin/env bash
# Estate building blocks used by create.sh and the fault injectors. Source after lib.sh.

# ensure_workspace NAME: create the sandbox workspace on the sandbox capacity if missing; prints its id.
ensure_workspace() {
  local name=$1 id capacity body="$WORK/workspace.json"
  assert_sandbox_workspace "$name"
  id=$(workspace_id "$name")
  if [[ -z $id ]]; then
    log "creating workspace $name"
    printf '{"displayName":"%s","capacityId":"%s","description":"FabricOps programme sandbox. Synthetic data only."}' \
      "$name" "$CAPACITY_ID" >"$body"
    id=$(fab POST /workspaces "$body" --query id -o tsv)
    [[ -n $id ]] || die "workspace $name was not created"
  else
    capacity=$(fab GET "/workspaces/$id" --query "capacityId || '-'" -o tsv)
    if [[ $capacity != "$CAPACITY_ID" ]]; then
      log "assigning workspace $name to the sandbox capacity"
      printf '{"capacityId":"%s"}' "$CAPACITY_ID" >"$body"
      fab_lro POST "/workspaces/$id/assignToCapacity" "$body" >/dev/null
    fi
  fi
  printf '%s' "$id"
}

# ensure_viewer WORKSPACE_ID: the test service principal gets Viewer (and never more) on a sandbox workspace.
ensure_viewer() {
  local ws=$1 body="$WORK/role.json" row assignment role
  row=$(fab GET "/workspaces/$ws/roleAssignments" \
    --query "value[?principal.id=='$TEST_SP_OBJECT_ID'] | [0].{a: id, b: role} || {a: '-', b: '-'}" -o tsv)
  IFS=$'\t' read -r assignment role <<<"$row"
  if [[ $assignment == '-' ]]; then
    log "granting Viewer to the test service principal on workspace $ws"
    printf '{"principal":{"id":"%s","type":"ServicePrincipal"},"role":"Viewer"}' "$TEST_SP_OBJECT_ID" >"$body"
    fab POST "/workspaces/$ws/roleAssignments" "$body" >/dev/null
  elif [[ $role != Viewer ]]; then
    log "reducing the test service principal's role on $ws from $role to Viewer"
    printf '{"role":"Viewer"}' >"$body"
    fab PATCH "/workspaces/$ws/roleAssignments/$assignment" "$body" >/dev/null
  fi
}

# ensure_table_reader_role WORKSPACE_ID LAKEHOUSE_ID: the minimum grant for reading a table's Delta log over OneLake
# (ADR-0009): a OneLake data access role giving the test service principal READ ONLY on Tables/customers. Roles can
# only be defined at the lakehouse root or table level, so this is the narrowest scope there is; the principal gets
# no write right, no other table and no Files access. Replaces the lakehouse's role set with the built-in
# DefaultReader plus this role (idempotent).
ensure_table_reader_role() {
  local ws=$1 lh=$2 body="$WORK/roles.json"
  cat >"$body" <<EOF
{"value":[
 {"name":"DefaultReader","kind":"Policy",
  "decisionRules":[{"effect":"Permit","permission":[{"attributeName":"Action","attributeValueIncludedIn":["Read"]},{"attributeName":"Path","attributeValueIncludedIn":["*"]}]}],
  "members":{"fabricItemMembers":[{"itemAccess":["ReadAll"],"sourcePath":"$ws/$lh"}]}},
 {"name":"fabricopsDeltaLogReader","kind":"Policy",
  "decisionRules":[{"effect":"Permit","permission":[{"attributeName":"Action","attributeValueIncludedIn":["Read"]},{"attributeName":"Path","attributeValueIncludedIn":["Tables/customers"]}]}],
  "members":{"microsoftEntraMembers":[{"tenantId":"$TENANT_ID","objectId":"$TEST_SP_OBJECT_ID"}]}}
]}
EOF
  log "ensuring the test service principal's read-only OneLake access to Tables/customers"
  fab PUT "/workspaces/$ws/items/$lh/dataAccessRoles" "$body" >/dev/null
}

# ensure_lakehouse WORKSPACE_ID: lakehouse crm_source; prints its id.
ensure_lakehouse() {
  local ws=$1 id body="$WORK/lakehouse.json"
  id=$(item_id "$ws" Lakehouse "$LAKEHOUSE_NAME")
  if [[ -z $id ]]; then
    log "creating lakehouse $LAKEHOUSE_NAME"
    printf '{"displayName":"%s","description":"Synthetic CRM source for the FabricOps sandbox."}' "$LAKEHOUSE_NAME" >"$body"
    fab_lro POST "/workspaces/$ws/lakehouses" "$body" >/dev/null
    id=$(wait_for_item "$ws" Lakehouse "$LAKEHOUSE_NAME")
  fi
  printf '%s' "$id"
}

# lakehouse_has_table WORKSPACE_ID LAKEHOUSE_ID TABLE: true if the Delta table is registered.
lakehouse_has_table() {
  local found
  found=$(fab GET "/workspaces/$1/lakehouses/$2/tables" --query "data[?name=='$3'].name | [0] || '-'" -o tsv 2>/dev/null || echo '-')
  [[ $found == "$3" ]]
}

# job_count WORKSPACE_ID ITEM_ID: number of job instances recorded for an item.
job_count() { fab GET "/workspaces/$1/items/$2/jobs/instances" --query "length(value)" -o tsv; }

# seed_customers WORKSPACE_ID SEED_NOTEBOOK_ID [ID_COLUMN]: (re)write the synthetic customers table.
seed_customers() {
  local ws=$1 notebook=$2 column=${3:-customer_id} body="$WORK/seed-params.json"
  printf '{"executionData":{"parameters":{"id_column":{"value":"%s","type":"string"}}}}' "$column" >"$body"
  run_job "$ws" "$notebook" RunNotebook "$body"
  [[ $JOB_STATUS == Completed ]] || die "seed_customers ended $JOB_STATUS"
}

# create_environment ENV RUN_JOBS: DEV | TEST | PROD workspace and its items.
create_environment() {
  local env=$1 run=$2 name ws lh seed transform pipeline
  name=$(env_workspace "$env")
  log "== $env: $name"
  ws=$(ensure_workspace "$name")
  ensure_viewer "$ws"
  lh=$(ensure_lakehouse "$ws")
  seed=$(apply_notebook "$ws" "$lh" "$NOTEBOOK_SEED" "$DEFS_DIR/seed_customers.ipynb")
  transform=$(apply_notebook "$ws" "$lh" "$NOTEBOOK_TRANSFORM" "$DEFS_DIR/transform_customers.ipynb")
  pipeline=$(apply_pipeline "$ws" "$transform" "$DEFS_DIR/ingest_customers.pipeline.json")
  if ((run)); then
    if ! lakehouse_has_table "$ws" "$lh" customers; then
      log "seeding synthetic customers table"
      seed_customers "$ws" "$seed"
    fi
    if [[ $(job_count "$ws" "$pipeline") == 0 ]]; then
      log "running $PIPELINE_NAME once to produce a baseline run and $CLEAN_TABLE"
      run_job "$ws" "$pipeline" Pipeline
      [[ $JOB_STATUS == Completed ]] || log "WARNING: baseline pipeline run ended $JOB_STATUS"
    fi
  fi
  if [[ " $ONELAKE_READ_ENVS " == *" $env "* ]]; then
    # A OneLake role can only name a table that exists, hence after the seed.
    if lakehouse_has_table "$ws" "$lh" customers; then
      ensure_table_reader_role "$ws" "$lh"
    else
      log "skipping the OneLake read role: table customers does not exist yet (run without --no-run)"
    fi
  fi
}

# apply_semantic_model WS SOURCE_WORKSPACE_ID SOURCE_LAKEHOUSE_ID [TEMPLATE]: install customers_model (by default
# Direct Lake over the transformed table of the given lakehouse); prints its id.
apply_semantic_model() {
  local ws=$1 source_ws=$2 source_lh=$3 template=${4:-$DEFS_DIR/customers_model.bim}
  render "$template" "SOURCE_WORKSPACE_ID=$source_ws" "SOURCE_LAKEHOUSE_ID=$source_lh" >"$WORK/model.bim"
  printf '{"version":"1.0"}' >"$WORK/definition.pbism"
  ensure_definition_item "$ws" SemanticModel semanticModels "$MODEL_NAME" TMSL \
    "model.bim=$WORK/model.bim" "definition.pbism=$WORK/definition.pbism"
}

# refresh_model WS MODEL_ID: start a full refresh as the signed-in user and wait. Sets REFRESH_STATUS.
refresh_model() {
  local ws=$1 model=$2 body="$WORK/refresh.json" deadline=$((SECONDS + 600)) before
  before=$(pbi GET "/groups/$ws/datasets/$model/refreshes" --query "length(value)" -o tsv)
  printf '{"type":"full","notifyOption":"NoNotification"}' >"$body"
  pbi POST "/groups/$ws/datasets/$model/refreshes" "$body" >/dev/null
  REFRESH_STATUS=Unknown
  while ((SECONDS < deadline)); do
    sleep 6
    if [[ $(pbi GET "/groups/$ws/datasets/$model/refreshes" --query "length(value)" -o tsv) -gt $before ]]; then
      REFRESH_STATUS=$(pbi GET "/groups/$ws/datasets/$model/refreshes" --query "value[0].status" -o tsv)
      [[ $REFRESH_STATUS != Unknown ]] && break
    fi
  done
  log "refresh of $MODEL_NAME: $REFRESH_STATUS"
}

# create_consumer RUN_JOBS: the Consumer workspace (semantic model and report over PROD's transformed data).
create_consumer() {
  local run=$1 name ws prod_ws prod_lh model report
  name=$(env_workspace CONSUMER)
  log "== CONSUMER: $name"
  ws=$(ensure_workspace "$name")
  ensure_viewer "$ws"
  prod_ws=$(workspace_id "$WS_PROD")
  [[ -n $prod_ws ]] || die "the PROD sandbox workspace must exist first (create.sh --env PROD)"
  prod_lh=$(item_id "$prod_ws" Lakehouse "$LAKEHOUSE_NAME")
  [[ -n $prod_lh ]] || die "PROD lakehouse $LAKEHOUSE_NAME is missing"
  model=$(apply_semantic_model "$ws" "$prod_ws" "$prod_lh")
  render "$DEFS_DIR/customers_report.pbir" "MODEL_ID=$model" >"$WORK/definition.pbir"
  report=$(ensure_definition_item "$ws" Report reports "$REPORT_NAME" '' \
    "definition.pbir=$WORK/definition.pbir" "report.json=$DEFS_DIR/customers_report.json")
  if ((run)) && [[ $(pbi GET "/groups/$ws/datasets/$model/refreshes" --query "length(value)" -o tsv) == 0 ]]; then
    log "refreshing $MODEL_NAME once to produce a baseline refresh"
    refresh_model "$ws" "$model"
  fi
  log "consumer: model $model, report $report"
}

# ensure_fake_connection: one connection holding a deliberately invalid, obviously fake credential.
#
# It is a "Web for pipeline" connection (the type the pipeline Web activity uses) with a Basic credential
# (user fabricops-fake, password not-a-real-secret) against FAKE_SOURCE_URL: this tenant's Microsoft Entra
# token endpoint, which rejects the fake client with an AADSTS error. That type cannot skip the connection
# test, but the test accepts any HTTP answer, so the fake credential is stored as is. A connection of another
# type or URL left by an earlier version of this script is replaced.
FAKE_SOURCE_URL="https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/"
ensure_fake_connection() {
  assert_sandbox_connection "$CONNECTION_NAME"
  local id row type path body="$WORK/connection.json"
  id=$(fab_find "/connections" "$CONNECTION_NAME")
  if [[ -n $id ]]; then
    row=$(fab GET "/connections/$id" --query "{a: connectionDetails.type, b: connectionDetails.path}" -o tsv)
    IFS=$'\t' read -r type path <<<"$row"
    if [[ $type != WebForPipeline || $path != "$FAKE_SOURCE_URL" ]]; then
      log "replacing fake connection $CONNECTION_NAME (type $type)"
      fab DELETE "/connections/$id" >/dev/null
      id=''
    fi
  fi
  if [[ -z $id ]]; then
    log "creating fake connection $CONNECTION_NAME"
    cat >"$body" <<EOF
{
  "connectivityType": "ShareableCloud",
  "displayName": "$CONNECTION_NAME",
  "connectionDetails": {
    "type": "WebForPipeline",
    "creationMethod": "WebForPipeline.Contents",
    "parameters": [{"dataType": "Text", "name": "baseUrl", "value": "$FAKE_SOURCE_URL"}]
  },
  "privacyLevel": "Organizational",
  "credentialDetails": {
    "singleSignOnType": "None",
    "connectionEncryption": "NotEncrypted",
    "skipTestConnection": false,
    "credentials": {"credentialType": "Basic", "username": "fabricops-fake", "password": "not-a-real-secret"}
  }
}
EOF
    fab POST /connections "$body" --query id -o tsv >/dev/null
  fi
}

# render_contracts_if_complete: render only when all four sandbox workspaces exist (a partial create.sh --env run
# cannot resolve every id); otherwise say so.
render_contracts_if_complete() {
  local name
  for name in "${SANDBOX_WORKSPACES[@]}"; do
    if [[ -z $(workspace_id "$name") ]]; then
      log "not rendering contracts.sandbox.json: workspace '$name' does not exist yet"
      return 0
    fi
  done
  render_contracts
}

# render_contracts: resolve the live item ids and write contracts.sandbox.json from the template. The
# output holds tenant item ids, so it is git-ignored; regenerate it with `create.sh --contracts-only`.
# SLA_DEADLINE / SLA_TIMEZONE in the environment override the configured SLA (used by inject/sla-breach.sh).
render_contracts() {
  local out="${CONTRACTS_OUT:-$LIVE_DIR/contracts.sandbox.json}" env ws script="$WORK/contracts.sed" id connection
  : >"$script"
  printf 's|@@TENANT_ID@@|%s|g\n' "$TENANT_ID" >>"$script"
  printf 's|@@SLA_DEADLINE@@|%s|g\n' "$SLA_DEADLINE" >>"$script"
  printf 's|@@SLA_TIMEZONE@@|%s|g\n' "$SLA_TIMEZONE" >>"$script"
  connection=$(fab_find "/connections" "$CONNECTION_NAME")
  printf 's|@@CONNECTION_ID@@|%s|g\n' "${connection:-unknown}" >>"$script"
  for env in "${ENVIRONMENTS[@]}"; do
    ws=$(workspace_id "$(env_workspace "$env")")
    [[ -n $ws ]] || die "workspace for $env does not exist; run create.sh first"
    printf 's|@@%s.workspace@@|%s|g\n' "$env" "$ws" >>"$script"
    for pair in "lakehouse:Lakehouse:$LAKEHOUSE_NAME" "seed:Notebook:$NOTEBOOK_SEED" "transform:Notebook:$NOTEBOOK_TRANSFORM" \
      "pipeline:DataPipeline:$PIPELINE_NAME"; do
      IFS=: read -r key type name <<<"$pair"
      id=$(item_id "$ws" "$type" "$name")
      [[ -n $id ]] || die "$name ($type) is missing in $env; run create.sh first"
      printf 's|@@%s.%s@@|%s|g\n' "$env" "$key" "$id" >>"$script"
    done
  done
  ws=$(workspace_id "$WS_CONSUMER")
  [[ -n $ws ]] || die "consumer workspace does not exist; run create.sh first"
  printf 's|@@CONSUMER.workspace@@|%s|g\n' "$ws" >>"$script"
  for pair in "model:SemanticModel:$MODEL_NAME" "report:Report:$REPORT_NAME"; do
    IFS=: read -r key type name <<<"$pair"
    id=$(item_id "$ws" "$type" "$name")
    [[ -n $id ]] || die "$name ($type) is missing in the consumer workspace; run create.sh first"
    printf 's|@@CONSUMER.%s@@|%s|g\n' "$key" "$id" >>"$script"
  done
  sed -f "$script" "$LIVE_DIR/contracts.sandbox.template.json" >"$out"
  if grep -q '@@' "$out"; then die "unresolved placeholders in $out: $(grep -o '@@[A-Za-z_.]*@@' "$out" | sort -u | tr '\n' ' ')"; fi
  log "wrote $out"
}
