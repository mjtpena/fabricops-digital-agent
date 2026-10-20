#!/usr/bin/env bash
# Post-deployment smoke test for a deployed Orqentis FabricOps API (see docs/delivery.md).
#
#   SMOKE_BASE_URL=https://ca-fabricops-dev.<env>.australiaeast.azurecontainerapps.io \
#   SMOKE_TOKEN_RESOURCE=api://<API app registration client id> \
#   bash scripts/smoke-test.sh
#
# Checks, in order:
#   0. The container app's LATEST revision is ready, takes 100% of traffic and runs the image that was deployed
#      (only when SMOKE_CONTAINER_APP, SMOKE_RESOURCE_GROUP and SMOKE_EXPECTED_IMAGE are set, as the pipeline does).
#      Without it, a failed rollout would be hidden behind the previous revision still answering the HTTP checks.
#   1. GET /health                          -> 200 (retried until the new revision answers)
#   2. GET /incidents, no token             -> 401 (authentication is enforced at the front door)
#   3. GET /incidents, bearer token         -> 200 and a JSON array (the caller holds an app role)
#
# The token is requested for SMOKE_TOKEN_RESOURCE with `az account get-access-token`, so the caller must be
# signed in as the pipeline's service principal (azure/login over OIDC) and that principal must hold the
# FabricOps.Automation app role on the API. SMOKE_TOKEN (a ready-made token) is accepted for local use.
# The token is never printed. No secrets are read from, or written to, the repository.
#
# Settings (environment variables):
#   SMOKE_BASE_URL               required   Base URL of the API, without a trailing slash.
#   SMOKE_TOKEN_RESOURCE         required unless SMOKE_TOKEN is set. Application ID URI of the API (api://<client id>).
#   SMOKE_TOKEN                  optional   Bearer token to use instead of calling `az`.
#   SMOKE_CONTAINER_APP / SMOKE_RESOURCE_GROUP / SMOKE_EXPECTED_IMAGE
#                                optional   All three or none. Enables check 0 (needs `az`, signed in with read access).
#                                           The expected image is the digest reference that was deployed.
#   SMOKE_HEALTH_AUTHENTICATED   optional   "true": send the token on /health. The default "false" matches the
#                                           deployment, which excludes /health from Container Apps authentication
#                                           (anonymousHealthEndpoints, default true). Set "true" only if you turned that off:
#                                           the front end then answers an anonymous /health with 401.
#   SMOKE_RETRIES / SMOKE_RETRY_DELAY       Attempts (default 30) and seconds between them (default 10), for the
#                                           revision check and for /health.
set -euo pipefail

base_url="${SMOKE_BASE_URL:-}"
token_resource="${SMOKE_TOKEN_RESOURCE:-}"
token="${SMOKE_TOKEN:-}"
health_authenticated="${SMOKE_HEALTH_AUTHENTICATED:-false}"
retries="${SMOKE_RETRIES:-30}"
retry_delay="${SMOKE_RETRY_DELAY:-10}"
container_app="${SMOKE_CONTAINER_APP:-}"
resource_group="${SMOKE_RESOURCE_GROUP:-}"
expected_image="${SMOKE_EXPECTED_IMAGE:-}"
max_time=20

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
  exit 0
fi

fail_count=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; fail_count=$((fail_count + 1)); }

if [[ -z "$base_url" ]]; then
  echo "SMOKE_BASE_URL is required" >&2
  exit 2
fi
if [[ -z "$token" && -z "$token_resource" ]]; then
  echo "Set SMOKE_TOKEN_RESOURCE (api://<client id>) or SMOKE_TOKEN" >&2
  exit 2
fi
if [[ -n "${container_app}${resource_group}${expected_image}" ]] \
  && [[ -z "$container_app" || -z "$resource_group" || -z "$expected_image" ]]; then
  echo "Set SMOKE_CONTAINER_APP, SMOKE_RESOURCE_GROUP and SMOKE_EXPECTED_IMAGE together, or none of them" >&2
  exit 2
fi
base_url="${base_url%/}"
case "$base_url" in
  https://*) ;;
  http://127.0.0.1* | http://localhost*) ;;
  *)
    echo "SMOKE_BASE_URL must be https:// (http:// is accepted for localhost only)" >&2
    exit 2
    ;;
esac

# GET <path> with an optional bearer token. Prints the HTTP status code; the body goes to $body_file.
# The Authorization header is passed through a curl config on stdin so it never appears in a process list.
body_file="$(mktemp)"
trap 'rm -f "$body_file"' EXIT
http_get() {
  local path="$1" bearer="${2:-}"
  if [[ -n "$bearer" ]]; then
    printf 'header = "Authorization: Bearer %s"\n' "$bearer" |
      curl --silent --show-error --max-time "$max_time" --config - \
        --output "$body_file" --write-out '%{http_code}' "${base_url}${path}" || true
  else
    curl --silent --show-error --max-time "$max_time" \
      --output "$body_file" --write-out '%{http_code}' "${base_url}${path}" || true
  fi
}

# Print the identifiers in a token that explain a rejected call (audience, roles). Never prints the token.
describe_token() {
  local python_bin=""
  for candidate in python3 python; do
    if "$candidate" -c 'import sys' > /dev/null 2>&1; then
      python_bin="$candidate"
      break
    fi
  done
  [[ -n "$python_bin" ]] || return 0
  TOKEN="$1" "$python_bin" -I - <<'PY' || true
import base64, json, os

try:
    payload = os.environ["TOKEN"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
except Exception:  # noqa: BLE001 - diagnostics only
    raise SystemExit(0)
print(f"      token claims: aud={claims.get('aud')} roles={claims.get('roles')} idtyp={claims.get('idtyp')}")
PY
}

# ---------------------------------------------------------------------------------------------- 0. revision
# Reads the app with `az resource` (core CLI, no extension to install). Single-revision mode: the latest revision must be
# the latest READY revision, must hold all traffic, and must run exactly the image the pipeline deployed.
revision_api_version="2024-03-01"
check_revision() {
  local attempt app_id latest ready details image traffic state
  for attempt in $(seq 1 "$retries"); do
    app_id=""
    latest=""
    ready=""
    image=""
    traffic=""
    state=""
    if info="$(az resource show --resource-group "$resource_group" --name "$container_app" \
      --resource-type Microsoft.App/containerApps --api-version "$revision_api_version" \
      --query "[id, properties.latestRevisionName, properties.latestReadyRevisionName]" \
      --output tsv 2> /dev/null)"; then
      { read -r app_id; read -r latest; read -r ready; } <<< "$info"
    fi
    if [[ -n "$latest" && "$latest" == "$ready" ]]; then
      if details="$(az resource show --ids "${app_id}/revisions/${latest}" --api-version "$revision_api_version" \
        --query "[properties.template.containers[0].image, properties.trafficWeight, properties.runningState]" \
        --output tsv 2> /dev/null)"; then
        { read -r image; read -r traffic; read -r state; } <<< "$details"
      fi
      if [[ "$image" == "$expected_image" && "$traffic" == "100" && "$state" == Running* ]]; then
        pass "revision ${latest} is ready, serves 100% of traffic and runs the deployed image"
        return 0
      fi
    fi
    echo "      revision attempt ${attempt}/${retries}: latest=${latest:-?} ready=${ready:-?} traffic=${traffic:-?} state=${state:-?}"
    sleep "$retry_delay"
  done
  fail "revision: latest=${latest:-?} ready=${ready:-?} traffic=${traffic:-?} state=${state:-?} image=${image:-?} (expected ${expected_image} at 100% traffic)"
  return 1
}

echo "Smoke test against ${base_url}"
if [[ -n "$container_app" ]]; then
  check_revision || true
else
  echo "      (revision check skipped: SMOKE_CONTAINER_APP is not set)"
fi

# ---------------------------------------------------------------------------------------------- token
if [[ -z "$token" ]]; then
  echo "Requesting a token for ${token_resource} as the signed-in principal"
  if ! token="$(az account get-access-token --resource "$token_resource" --query accessToken --output tsv)"; then
    echo "Could not get a token. Check that the pipeline identity holds the FabricOps.Automation app role" >&2
    echo "on the API's enterprise application (docs/delivery.md, section 'Smoke-test identity')." >&2
    exit 1
  fi
fi
if [[ -z "$token" ]]; then
  echo "Token request returned nothing" >&2
  exit 1
fi
if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "::add-mask::${token}"
fi

# ---------------------------------------------------------------------------------------------- 1. /health
health_token=""
[[ "$health_authenticated" == "true" ]] && health_token="$token"
status="000"
for attempt in $(seq 1 "$retries"); do
  status="$(http_get /health "$health_token")"
  if [[ "$status" == "200" ]]; then
    break
  fi
  echo "      /health attempt ${attempt}/${retries}: HTTP ${status}"
  sleep "$retry_delay"
done
if [[ "$status" == "200" ]]; then
  pass "GET /health -> 200"
else
  fail "GET /health -> ${status} (expected 200)"
  if [[ "$status" == "401" && "$health_authenticated" != "true" ]]; then
    echo "      Authentication also covers /health here; set SMOKE_HEALTH_AUTHENTICATED=true or exclude /health" >&2
    echo "      from the container app authentication (excludedPaths)." >&2
  fi
fi

# ---------------------------------------------------------------------------------------------- 2. anonymous
status="$(http_get /incidents)"
if [[ "$status" == "401" ]]; then
  pass "GET /incidents without a token -> 401"
else
  fail "GET /incidents without a token -> ${status} (expected 401: authentication must be enforced)"
fi

# ---------------------------------------------------------------------------------------------- 3. authenticated
status="$(http_get /incidents "$token")"
first_char="$(head -c 64 "$body_file" | tr -d '[:space:]' | head -c 1)"
if [[ "$status" == "200" && "$first_char" == "[" ]]; then
  pass "GET /incidents with the Automation token -> 200 (JSON array)"
elif [[ "$status" == "200" ]]; then
  fail "GET /incidents with the Automation token -> 200 but the body is not a JSON array"
else
  fail "GET /incidents with the Automation token -> ${status} (expected 200)"
  describe_token "$token"
fi

echo
if ((fail_count > 0)); then
  echo "Smoke test FAILED (${fail_count} check(s))"
  exit 1
fi
echo "Smoke test passed"
