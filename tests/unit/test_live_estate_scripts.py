"""Static guard rails for ``scripts/live-estate``: the scripts that touch a real tenant stay inside their approval.

The scripts run with a Global Administrator's sign-in (bootstrap only). What they may do is a short, closed list;
these tests fail if a script grows a call outside it, so widening the blast radius needs an explicit, reviewed change
here as well. They read the scripts as text and never run them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[2] / "scripts" / "live-estate"
SCRIPTS = sorted(LIVE.rglob("*.sh"))

# Every mutating REST call a script may make: (helper, method, path with $variables normalised to {}).
APPROVED_CALLS = {
    # capacity: resume and suspend only
    ("arm", "POST", "$CAPACITY_ARM_ID/resume?api-version=2023-11-01"),
    ("arm", "POST", "$CAPACITY_ARM_ID/suspend?api-version=2023-11-01"),
    # the four sandbox workspaces
    ("fab", "POST", "/workspaces"),
    ("fab", "DELETE", "/workspaces/$id"),
    # Viewer for the test principal (the body is asserted below)
    ("fab", "POST", "/workspaces/$ws/roleAssignments"),
    ("fab", "PATCH", "/workspaces/$ws/roleAssignments/$assignment"),
    # items inside them
    ("fab_lro", "POST", "/workspaces/$ws/lakehouses"),
    ("fab_lro", "POST", "/workspaces/$ws/$collection"),
    ("fab_lro", "POST", "/workspaces/$ws/$collection/$id/updateDefinition"),
    ("pbi", "POST", "/groups/$ws/datasets/$model/refreshes"),
    # item-level read-only grant on a sandbox lakehouse
    ("fab", "PUT", "/workspaces/$ws/items/$lh/dataAccessRoles"),
    # the fake-credential connection
    ("fab", "POST", "/connections"),
    ("fab", "DELETE", "/connections/$id"),
    ("fab", "DELETE", "/connections/$connection"),
}
CALL = re.compile(r"""\b(fab|fab_lro|pbi|arm|onelake)\s+(POST|PUT|PATCH|DELETE)\s+"?([^\s"]+)""")


def code_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.lstrip().startswith("#")]


def code(path: Path) -> str:
    return "\n".join(code_lines(path))


def test_the_scripts_exist() -> None:
    names = {p.name for p in SCRIPTS}
    assert {"create.sh", "teardown.sh", "capacity.sh", "lib.sh", "estate.sh", "status.sh", "seed-history.sh"} <= names
    assert {p.name for p in (LIVE / "inject").glob("*.sh")} >= {
        "schema-drift.sh",
        "notebook-failure.sh",
        "pipeline-timeout.sh",
        "auth-failure.sh",
        "refresh-failure.sh",
        "sla-breach.sh",
    }


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(LIVE)))
def test_scripts_parse_with_bash_and_use_lf_line_endings(script: Path) -> None:
    assert b"\r" not in script.read_bytes(), "CRLF breaks bash; .gitattributes pins *.sh to LF"
    bash = shutil.which("bash")
    assert bash is not None, "bash is required (Git Bash on Windows)"
    subprocess.run([bash, "-n", str(script)], check=True, capture_output=True)  # noqa: S603


def test_every_mutating_call_is_on_the_approved_list() -> None:
    seen: set[tuple[str, str, str]] = set()
    for script in SCRIPTS:
        for helper, method, path in CALL.findall(code(script)):
            seen.add((helper, method, path))
    assert seen, "found no mutating calls: the pattern is stale"
    assert seen <= APPROVED_CALLS, f"calls outside the approval: {sorted(seen - APPROVED_CALLS)}"


def test_raw_curl_is_confined_to_one_helper_that_only_gets_or_starts_a_job() -> None:
    """`az rest` cannot expose response headers, so `_http` calls curl. Nothing else may."""
    curl_calls = {s.name: re.findall(r"(?<![\w-])curl\s+(?!--version)", code(s)) for s in SCRIPTS}
    lib = code(LIVE / "lib.sh")
    # `need curl` is a dependency check; the one real invocation is `$(curl "${args[@]}" "$url" ...)` inside _http.
    assert len(re.findall(r'\$\(curl "\$\{args\[@\]\}"', lib)) == 1
    assert all(not hits for name, hits in curl_calls.items() if name != "lib.sh" and hits), curl_calls
    assert not re.search(r"-X\s+(?!\"\$method\")\S+", lib), "curl's method must always be the _http argument"

    # Every _http call: GET (operation polling), the one literal POST that starts a job, or the variable method
    # that fab_lro forwards (its callers are checked against APPROVED_CALLS above).
    calls = re.findall(r'\b_http\s+("\$method"|[A-Z]+)\s+("[^"]*"|\S+)', lib)
    methods = [m for m, _ in calls]
    assert set(methods) <= {"GET", "POST", '"$method"'}, methods
    assert methods.count("POST") == 1
    post_url = next(url for m, url in calls if m == "POST")
    assert "/jobs/instances?jobType=" in post_url
    assert methods.count('"$method"') == 1
    fab_lro_body = lib.split("fab_lro() {", 1)[1].split("\n}\n", 1)[0]
    assert '_http "$method"' in fab_lro_body, "the variable method may only be forwarded by fab_lro"
    for script in SCRIPTS:
        text = code(script)
        if script.name != "lib.sh":
            assert "_http" not in text.replace("_common.sh", ""), f"{script.name} calls the curl helper directly"
        assert "/admin" not in text and "tenantsettings" not in text.lower(), script.name


def test_retry_policy_never_repeats_a_post_after_a_bare_5xx() -> None:
    lib = code(LIVE / "lib.sh")
    # curl path: GET/DELETE retry on 429 and any 5xx; every other method only on 429 and 503.
    assert "GET | HEAD | DELETE) [[ $2 == 429 || $2 == 5?? || $2 == 000 ]] ;;" in lib
    assert "*) [[ $2 == 429 || $2 == 503 ]] ;;" in lib
    # az rest path: the base set is 429/503; 500/502/504 are added for GET and DELETE only.
    assert "transient='429|503|TooManyRequests|Too Many Requests'" in lib
    guarded = lib.split("transient+=", 1)[0].rsplit("if ", 1)[1]
    assert "--method GET" in guarded and "--method DELETE" in guarded and "POST" not in guarded
    assert "transient+='|500|502|504|Internal Server Error'" in lib


def test_a_workspace_with_a_sandbox_name_on_another_capacity_is_never_moved_or_adopted() -> None:
    estate = code(LIVE / "estate.sh")
    assert "refusing to move or adopt" in estate
    for script in SCRIPTS:
        assert "assignToCapacity" not in code(script), script.name


def test_every_entry_point_checks_the_tenant_first() -> None:
    for name in ("create.sh", "teardown.sh", "capacity.sh", "status.sh", "inject/_common.sh"):
        assert re.search(r"^assert_tenant$", code(LIVE / name), re.MULTILINE), name
    assert "az account show --query tenantId" in code(LIVE / "lib.sh")
    # create.sh must check the tenant before it touches the capacity.
    create = code(LIVE / "create.sh")
    assert create.index("assert_tenant") < create.index("require_capacity_active")


def test_the_example_config_holds_placeholders_not_real_ids() -> None:
    example = (LIVE / "config.env.example").read_text(encoding="utf-8")
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", example, re.IGNORECASE)
    for variable in ("TENANT_ID", "SUBSCRIPTION_ID", "CAPACITY_ID", "TEST_SP_OBJECT_ID"):
        assert re.search(rf'^{variable}="<[a-z-]+>"', example, re.MULTILINE), variable
    assert "<placeholders>" in example and "config.env" in example


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(LIVE)))
def test_no_script_touches_entra_azure_resources_or_the_capacity_lifecycle(script: Path) -> None:
    text = code(script)
    for forbidden in (r"\baz ad\b", r"\baz role\b", r"\baz group\b", r"\baz resource\b", r"\baz fabric\b"):
        assert not re.search(forbidden, text), f"{script.name}: {forbidden}"
    # ARM is reached only through the capacity.sh resume/suspend calls and read-only state queries.
    assert not re.search(r"arm\s+(PUT|PATCH|DELETE)\b", text)
    if script.name != "capacity.sh":
        assert not re.search(r"arm\s+POST\b", text)


def test_workspace_names_and_roles_are_pinned() -> None:
    lib = code(LIVE / "lib.sh")
    assert "assert_sandbox_workspace" in lib and "assert_sandbox_connection" in lib
    config = (LIVE / "config.env.example").read_text(encoding="utf-8")
    names = re.findall(r'^WS_\w+="([^"]+)"', config, re.MULTILINE)
    assert names == [
        "FabricOps Sandbox [DEV]",
        "FabricOps Sandbox [TEST]",
        "FabricOps Sandbox [PROD]",
        "FabricOps Sandbox Consumer",
    ]
    assert re.search(r'^CONNECTION_NAME="fabricops-sandbox-', config, re.MULTILINE)

    estate = code(LIVE / "estate.sh")
    roles = set(re.findall(r'"role":"(\w+)"', estate))
    assert roles == {"Viewer"}, "the only workspace role a script may grant is Viewer"
    # OneLake grant is read-only by construction: the only action is Read, never Write.
    grant = estate.split("ensure_table_reader_role()", 1)[1].split("fab PUT", 1)[0]
    assert re.findall(r'"attributeName":"Action","attributeValueIncludedIn":\["(\w+)"\]', grant) == ["Read", "Read"]


def test_teardown_only_deletes_sandbox_workspaces_on_the_sandbox_capacity() -> None:
    text = code(LIVE / "teardown.sh")
    assert 'for name in "${SANDBOX_WORKSPACES[@]}"' in text
    assert '$capacity != "$CAPACITY_ID"' in text and "refusing to delete" in text
    assert "assert_sandbox_connection" in text


def test_the_fake_connection_credential_is_obviously_fake_and_nothing_else_is_stored() -> None:
    estate = code(LIVE / "estate.sh")
    assert '"username": "fabricops-fake"' in estate and '"password": "not-a-real-secret"' in estate
    credentials = re.findall(r'"credentials": \{[^}]*\}', estate)
    assert len(credentials) == 1
    for script in SCRIPTS:
        assert not re.search(r"(?i)client_secret|AZURE_CLIENT_SECRET", code(script)) or script.name == "serve-live.sh"


def test_generated_contracts_and_local_config_are_not_committable() -> None:
    ignore = (LIVE.parents[1] / ".gitignore").read_text(encoding="utf-8")
    assert "scripts/live-estate/contracts.sandbox.json" in ignore and "scripts/live-estate/config.env" in ignore


def test_the_contracts_template_is_valid_json_with_only_placeholders_for_ids() -> None:
    template = (LIVE / "contracts.sandbox.template.json").read_text(encoding="utf-8")
    document = json.loads(template)
    assert set(document) == {"external_sources", "data_products", "dependencies", "slas"}
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", template, re.IGNORECASE)
