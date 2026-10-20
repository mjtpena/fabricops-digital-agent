#!/usr/bin/env bash
# Create (or converge) the FabricOps live sandbox estate. Idempotent: every step looks the object up by
# name first, so running it twice changes nothing the second time.
#
#   scripts/live-estate/create.sh [--env DEV|TEST|PROD|CONSUMER]... [--no-run] [--contracts-only]
#
# Creates, on the sandbox capacity:
#   FabricOps Sandbox [DEV|TEST|PROD]  lakehouse crm_source (+ synthetic customers table),
#                                      notebooks seed_customers / transform_customers,
#                                      data pipeline ingest_customers (runs transform_customers)
#   FabricOps Sandbox Consumer         semantic model customers_model and report customers_report
#   connection fabricops-sandbox-fake-source (deliberately invalid, obviously fake credential)
# and grants the least-privilege test service principal the Viewer role on all four workspaces.
# Finally it renders contracts.sandbox.json (item ids) from contracts.sandbox.template.json.
#
# Notebook and pipeline definitions are always converged to the baseline, so re-running this also
# reverts any injected notebook/pipeline fault. Data faults are reverted by inject/schema-drift.sh --revert.
#
# Needs: the capacity Active (capacity.sh resume) and an `az login` that may create workspaces.
set -euo pipefail
# shellcheck source=lib.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
# shellcheck source=estate.sh
source "$LIVE_DIR/estate.sh"

selected=()
run_jobs=1
contracts_only=0
while (($#)); do
  case $1 in
    --env)
      selected+=("$2")
      shift 2
      ;;
    --no-run)
      run_jobs=0
      shift
      ;;
    --contracts-only)
      contracts_only=1
      shift
      ;;
    -h | --help)
      sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) die "unknown argument: $1" ;;
  esac
done
((${#selected[@]})) || selected=(DEV TEST PROD CONSUMER)

assert_tenant
log "signed in as $(az account show --query user.name -o tsv | tr -d '\r'); tenant $TENANT_ID"

if ((contracts_only)); then
  render_contracts
  exit 0
fi

require_capacity_active

for env in "${selected[@]}"; do
  case $env in
    DEV | TEST | PROD) create_environment "$env" "$run_jobs" ;;
    CONSUMER) create_consumer "$run_jobs" ;;
    *) die "unknown environment $env" ;;
  esac
done

ensure_fake_connection
render_contracts_if_complete
log "estate converged"
