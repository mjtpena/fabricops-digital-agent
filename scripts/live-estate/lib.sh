#!/usr/bin/env bash
# Shared helpers for the live sandbox estate scripts. Source this file; do not execute it.
#
# Requirements: bash 4+, Azure CLI signed in as a user who may create Fabric workspaces on the
# sandbox capacity, curl, base64, sed, grep. No jq: JSON is read with `az rest --query` (JMESPath).
#
# Safety rules enforced here (and nowhere bypassed):
#   * Only the four workspaces named in config.env(.example) are ever created, changed or deleted.
#   * The capacity is only ever read, resumed or suspended (capacity.sh). Never created or reassigned.
#   * The only role ever granted is Viewer, to the least-privilege test service principal.
#   * The only connection ever created carries the prefix fabricops-sandbox- and a fake credential.

if [[ -n "${_FABRICOPS_LIB_LOADED:-}" ]]; then return 0; fi
_FABRICOPS_LIB_LOADED=1

set -euo pipefail
# Git Bash on Windows rewrites arguments that look like POSIX paths (/subscriptions/...). Never do that.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

LIVE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFS_DIR="$LIVE_DIR/definitions"

# shellcheck source=config.env.example
source "$LIVE_DIR/config.env.example"
if [[ -f "${FABRICOPS_ESTATE_CONFIG:-$LIVE_DIR/config.env}" ]]; then
  # shellcheck disable=SC1090
  source "${FABRICOPS_ESTATE_CONFIG:-$LIVE_DIR/config.env}"
fi

FABRIC_RESOURCE="https://api.fabric.microsoft.com"
FABRIC_API="$FABRIC_RESOURCE/v1"
POWERBI_RESOURCE="https://analysis.windows.net/powerbi/api"
POWERBI_API="https://api.powerbi.com/v1.0/myorg"
STORAGE_RESOURCE="https://storage.azure.com"
ONELAKE="https://onelake.dfs.fabric.microsoft.com"
ARM="https://management.azure.com"
CAPACITY_ARM_ID="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$CAPACITY_RG/providers/Microsoft.Fabric/capacities/$CAPACITY_NAME"

ENVIRONMENTS=(DEV TEST PROD)
LAKEHOUSE_NAME="crm_source"
NOTEBOOK_SEED="seed_customers"
NOTEBOOK_TRANSFORM="transform_customers"
PIPELINE_NAME="ingest_customers"
MODEL_NAME="customers_model"
REPORT_NAME="customers_report"
CLEAN_TABLE="customers_clean"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ------------------------------------------------------------------------------------------ basics
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*" >&2; }
die() {
  log "ERROR: $*"
  exit 1
}
need() { command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"; }
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
b64() { base64 -w0 <"$1"; }
utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

need az
need curl
need base64
need sed
need grep

# The four ids come from the git-ignored config.env; config.env.example only holds placeholders.
for _id_var in TENANT_ID SUBSCRIPTION_ID CAPACITY_ID TEST_SP_OBJECT_ID; do
  if [[ ! ${!_id_var} =~ ^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$ ]]; then
    die "$_id_var is not set to a real id (it is '${!_id_var}'): copy config.env.example to config.env and fill in the ids"
  fi
done
unset _id_var

# assert_tenant: the Azure CLI must be signed in to the sandbox tenant before anything is read or changed.
assert_tenant() {
  local current
  current=$(az account show --query tenantId -o tsv | tr -d '\r') || die "az is not signed in; run az login"
  [[ $current == "$TENANT_ID" ]] || die "az is signed in to another tenant (expected the sandbox tenant $TENANT_ID)"
}

# Substitute __KEY__ placeholders in a template: render FILE KEY=VALUE ... > output
render() {
  local file=$1 kv
  shift
  local -a expr=()
  for kv in "$@"; do expr+=(-e "s|__${kv%%=*}__|${kv#*=}|g"); done
  sed "${expr[@]}" "$file"
}

# First string value of KEY in JSON read from stdin (flat lookups only).
json_str() { grep -o "\"$1\" *: *\"[^\"]*\"" | head -1 | sed 's/^[^:]*: *"\(.*\)"$/\1/'; }

# --------------------------------------------------------------------------------------- az rest
# Runs `az` with retries on throttling and transient service errors; prints the response body.
az_retry() {
  # Retry policy: reads and deletes are safe to repeat, so any transient 5xx is retried. Anything else (a create, a
  # definition update, a job start) is retried only when the service says it did not process the request:
  # 429 and 503. A bare 500 or 502/504 on a POST may have been executed.
  local attempt=0 err out msg transient='429|503|TooManyRequests|Too Many Requests'
  err="$WORK/az.err"
  if [[ " $* " == *" --method GET "* || " $* " == *" --method DELETE "* ]]; then
    transient+='|500|502|504|Internal Server Error'
  fi
  while :; do
    attempt=$((attempt + 1))
    if out=$(az "$@" 2>"$err"); then
      if [[ -n $out ]]; then printf '%s\n' "$out" | tr -d '\r'; fi
      return 0
    fi
    msg=$(<"$err")
    if ((attempt < 6)) && grep -qiE "(^|[^0-9])($transient)([^0-9]|\$)" <<<"$msg"; then
      log "throttled or transient error (attempt $attempt); retrying"
      sleep $((attempt * 8))
      continue
    fi
    printf '%s\n' "$msg" | tr -d '\r' >&2
    return 1
  done
}

# fab METHOD PATH [BODY_FILE] [az rest options...]   (Fabric REST, as the signed-in user)
fab() {
  local method=$1 path=$2 body=''
  shift 2
  if [[ $# -gt 0 && $1 != -* ]]; then
    body=$1
    shift
  fi
  local -a args=(rest --method "$method" --resource "$FABRIC_RESOURCE" --url "$FABRIC_API$path")
  if [[ -n $body ]]; then args+=(--body "@$(winpath "$body")"); fi
  az_retry "${args[@]}" "$@"
}

# pbi METHOD PATH [BODY_FILE] [az rest options...]   (Power BI REST)
pbi() {
  local method=$1 path=$2 body=''
  shift 2
  if [[ $# -gt 0 && $1 != -* ]]; then
    body=$1
    shift
  fi
  local -a args=(rest --method "$method" --resource "$POWERBI_RESOURCE" --url "$POWERBI_API$path")
  if [[ -n $body ]]; then args+=(--body "@$(winpath "$body")"); fi
  az_retry "${args[@]}" "$@"
}

# arm METHOD PATH [az rest options...]   (Azure Resource Manager; PATH starts with /subscriptions)
arm() {
  local method=$1 path=$2
  shift 2
  az_retry rest --method "$method" --url "$ARM$path" "$@"
}

# onelake METHOD URL [BODY_FILE] [az rest options...]   (OneLake DFS endpoint, user token)
onelake() {
  local method=$1 url=$2 body=''
  shift 2
  if [[ $# -gt 0 && $1 != -* ]]; then
    body=$1
    shift
  fi
  local -a args=(rest --method "$method" --resource "$STORAGE_RESOURCE" --url "$url")
  if [[ -n $body ]]; then args+=(--body "@$(winpath "$body")"); fi
  az_retry "${args[@]}" "$@"
}

# ------------------------------------------------------------------- curl (needs response headers)
# `az rest` cannot expose response headers, which long-running operations need (Location).
_token() { az account get-access-token --resource "$1" --query accessToken -o tsv | tr -d '\r'; }

# _retryable METHOD STATUS: same policy as az_retry. GET and DELETE are retried on 429, any 5xx and transport
# failure (000); every other method only on 429 and 503, honouring Retry-After.
_retryable() {
  case $1 in
    GET | HEAD | DELETE) [[ $2 == 429 || $2 == 5?? || $2 == 000 ]] ;;
    *) [[ $2 == 429 || $2 == 503 ]] ;;
  esac
}

# _http METHOD URL RESOURCE [BODY_FILE]; sets HTTP_STATUS, HTTP_HEADERS, HTTP_BODY. Retries per _retryable.
_http() {
  local method=$1 url=$2 resource=$3 body=${4:-} attempt=0 wait
  local hfile="$WORK/auth.hdr"
  HTTP_HEADERS="$WORK/resp.hdr"
  HTTP_BODY="$WORK/resp.body"
  while :; do
    attempt=$((attempt + 1))
    (
      umask 077
      printf 'Authorization: Bearer %s\n' "$(_token "$resource")" >"$hfile"
    )
    local -a args=(-sS -X "$method" -H "@$(winpath "$hfile")" -H 'Content-Type: application/json'
      -D "$(winpath "$HTTP_HEADERS")" -o "$(winpath "$HTTP_BODY")" -w '%{http_code}')
    if [[ -n $body ]]; then args+=(--data-binary "@$(winpath "$body")"); elif [[ $method == POST ]]; then args+=(-d ''); fi
    HTTP_STATUS=$(curl "${args[@]}" "$url" || true)
    rm -f "$hfile"
    if _retryable "$method" "$HTTP_STATUS" && ((attempt < 6)); then
      wait=$(header_value Retry-After)
      wait=${wait:-$((attempt * 10))}
      log "HTTP $HTTP_STATUS from $method ${url%%\?*}; retrying in ${wait}s"
      sleep "$wait"
      continue
    fi
    return 0
  done
}

header_value() { { grep -i "^$1:" "$HTTP_HEADERS" 2>/dev/null || true; } | tail -1 | sed 's/^[^:]*: *//' | tr -d '\r\n'; }

# fab_lro METHOD PATH [BODY_FILE]: call a Fabric API that may be long-running (202); wait for it.
# Prints the response body (final operation state for 202). Returns non-zero on failure.
fab_lro() {
  local method=$1 path=$2 body=${3:-}
  _http "$method" "$FABRIC_API$path" "$FABRIC_RESOURCE" "$body"
  case $HTTP_STATUS in
    200 | 201 | 204)
      cat "$HTTP_BODY"
      return 0
      ;;
    202) ;;
    *)
      log "HTTP $HTTP_STATUS from $method $path"
      cat "$HTTP_BODY" >&2
      echo >&2
      return 1
      ;;
  esac
  local loc retry state deadline=$((SECONDS + ${LRO_TIMEOUT:-600}))
  loc=$(header_value Location)
  retry=$(header_value Retry-After)
  retry=${retry:-5}
  ((retry > 15)) && retry=15
  while ((SECONDS < deadline)); do
    sleep "$retry"
    _http GET "$loc" "$FABRIC_RESOURCE"
    state=$(json_str status <"$HTTP_BODY")
    case $state in
      Succeeded)
        cat "$HTTP_BODY"
        return 0
        ;;
      Failed | Cancelled | Canceled)
        log "operation $state: $(cat "$HTTP_BODY")"
        return 1
        ;;
    esac
  done
  die "timed out waiting for $method $path"
}

# run_job WORKSPACE_ID ITEM_ID JOB_TYPE [BODY_FILE]: start an item job and wait for it to finish.
# Sets JOB_ID and JOB_STATUS (Completed | Failed | Cancelled | Deduped). Never exits on a failed job.
run_job() {
  local ws=$1 item=$2 type=$3 body=${4:-} loc deadline=$((SECONDS + ${JOB_TIMEOUT:-900}))
  _http POST "$FABRIC_API/workspaces/$ws/items/$item/jobs/instances?jobType=$type" "$FABRIC_RESOURCE" "$body"
  if [[ $HTTP_STATUS != 202 ]]; then
    log "HTTP $HTTP_STATUS starting $type job"
    cat "$HTTP_BODY" >&2
    echo >&2
    die "could not start job"
  fi
  loc=$(header_value Location)
  JOB_ID=${loc##*/}
  JOB_STATUS=NotStarted
  while ((SECONDS < deadline)); do
    sleep 8
    _http GET "$loc" "$FABRIC_RESOURCE"
    JOB_STATUS=$(json_str status <"$HTTP_BODY")
    case $JOB_STATUS in
      Completed | Failed | Cancelled | Deduped)
        log "job $JOB_ID ($type): $JOB_STATUS"
        return 0
        ;;
    esac
  done
  die "timed out waiting for $type job $JOB_ID"
}

# ----------------------------------------------------------------------------- safety guard rails
SANDBOX_WORKSPACES=("$WS_DEV" "$WS_TEST" "$WS_PROD" "$WS_CONSUMER")

assert_sandbox_workspace() {
  local candidate=$1 name
  for name in "${SANDBOX_WORKSPACES[@]}"; do
    [[ $name == "$candidate" ]] && return 0
  done
  die "refusing to act on '$candidate': not one of the FabricOps sandbox workspaces"
}

assert_sandbox_connection() {
  [[ $1 == fabricops-sandbox-* ]] || die "refusing to act on connection '$1': name must start with fabricops-sandbox-"
}

env_workspace() {
  case $1 in
    DEV) printf '%s' "$WS_DEV" ;;
    TEST) printf '%s' "$WS_TEST" ;;
    PROD) printf '%s' "$WS_PROD" ;;
    CONSUMER) printf '%s' "$WS_CONSUMER" ;;
    *) die "unknown environment '$1' (DEV, TEST, PROD or CONSUMER)" ;;
  esac
}

# ------------------------------------------------------------------------------------- capacity
capacity_state() { arm GET "$CAPACITY_ARM_ID?api-version=2023-11-01" --query properties.state -o tsv; }

require_capacity_active() {
  local state
  state=$(capacity_state)
  [[ $state == Active ]] || die "capacity $CAPACITY_NAME is $state; run scripts/live-estate/capacity.sh resume first"
}

# ------------------------------------------------------------------------------------- lookups
_urlenc() { sed -e 's/+/%2B/g' -e 's|/|%2F|g' -e 's/=/%3D/g'; }

# fab_find LIST_PATH DISPLAY_NAME: id of the entry named DISPLAY_NAME in a paged Fabric list, or nothing.
fab_find() {
  local path=$1 name=$2 token='' sep='?' row id next url
  [[ $path == *\?* ]] && sep='&'
  while :; do
    url=$path
    [[ -n $token ]] && url="$path${sep}continuationToken=$(printf '%s' "$token" | _urlenc)"
    row=$(fab GET "$url" --query "{a: value[?displayName=='$name'].id | [0] || '-', b: continuationToken || '-'}" -o tsv)
    IFS=$'\t' read -r id next <<<"$row"
    if [[ $id != '-' ]]; then
      printf '%s' "$id"
      return 0
    fi
    [[ $next == '-' ]] && return 0
    token=$next
  done
}

workspace_id() {
  assert_sandbox_workspace "$1"
  fab_find "/workspaces" "$1"
}

# item_id WORKSPACE_ID TYPE NAME
item_id() { fab_find "/workspaces/$1/items?type=$2" "$3"; }

# wait_for_item WORKSPACE_ID TYPE NAME: poll until the item exists; prints its id.
wait_for_item() {
  local id deadline=$((SECONDS + 240))
  while ((SECONDS < deadline)); do
    id=$(item_id "$1" "$2" "$3")
    if [[ -n $id ]]; then
      printf '%s' "$id"
      return 0
    fi
    sleep 5
  done
  die "item $3 ($2) did not appear in workspace $1"
}

# --------------------------------------------------------------------------------- definitions
# write_definition FILE FORMAT PART=FILE... prints the JSON `definition` object (parts base64-encoded).
_definition_json() { # FORMAT PART=FILE...
  local format=$1 spec sep='' path file
  shift
  printf '{'
  [[ -n $format ]] && printf '"format":"%s",' "$format"
  printf '"parts":['
  for spec in "$@"; do
    path=${spec%%=*}
    file=${spec#*=}
    printf '%s{"path":"%s","payload":"%s","payloadType":"InlineBase64"}' "$sep" "$path" "$(b64 "$file")"
    sep=,
  done
  printf ']}'
}

# ensure_definition_item WS TYPE COLLECTION NAME FORMAT PART=FILE...: create the item if it is missing,
# otherwise converge its definition to the given parts. Prints the item id.
ensure_definition_item() {
  local ws=$1 type=$2 collection=$3 name=$4 format=$5 id body="$WORK/body.json"
  shift 5
  id=$(item_id "$ws" "$type" "$name")
  if [[ -z $id ]]; then
    log "creating $type $name"
    printf '{"displayName":"%s","type":"%s","definition":%s}' "$name" "$type" "$(_definition_json "$format" "$@")" >"$body"
    fab_lro POST "/workspaces/$ws/$collection" "$body" >/dev/null
    id=$(wait_for_item "$ws" "$type" "$name")
  else
    log "converging definition of $type $name"
    printf '{"definition":%s}' "$(_definition_json "$format" "$@")" >"$body"
    fab_lro POST "/workspaces/$ws/$collection/$id/updateDefinition" "$body" >/dev/null
  fi
  printf '%s' "$id"
}

# apply_notebook WS LAKEHOUSE_ID NAME TEMPLATE: install a notebook from a template; prints its id.
apply_notebook() {
  local ws=$1 lakehouse=$2 name=$3 template=$4 rendered="$WORK/notebook.ipynb"
  render "$template" "WORKSPACE_ID=$ws" "LAKEHOUSE_ID=$lakehouse" >"$rendered"
  ensure_definition_item "$ws" Notebook notebooks "$name" ipynb "notebook-content.ipynb=$rendered"
}

# apply_pipeline WS NOTEBOOK_ID TEMPLATE [TIMEOUT] [CONNECTION_ID]: install ingest_customers from a template
# (activity timeout is d.hh:mm:ss); prints its id.
apply_pipeline() {
  local ws=$1 notebook=$2 template=$3 timeout=${4:-0.00:30:00} connection=${5:-none} rendered="$WORK/pipeline.json"
  render "$template" "WORKSPACE_ID=$ws" "NOTEBOOK_ID=$notebook" "ACTIVITY_TIMEOUT=$timeout" "CONNECTION_ID=$connection" \
    >"$rendered"
  ensure_definition_item "$ws" DataPipeline dataPipelines "$PIPELINE_NAME" '' "pipeline-content.json=$rendered"
}
