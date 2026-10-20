"""The fixture sanitiser must never let tenant identity, tokens or e-mail addresses into the repository."""

from __future__ import annotations

import json

import pytest

from tests.live.record_fixtures import FIXTURE_DIR, SYNTHETIC_GUID, Sanitiser, UnsafeFixtureError

# Obviously made-up values: these tests must not carry anything that resembles a real tenant.
SAMPLE_GUID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER_GUID = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
SAMPLE_TRIAL_CAPACITY = "Trial-20990101T000000Z-EXAMPLEEXAMPLE"
SAMPLE_CLUSTER = "wabi-example-region-b-primary-redirect.analysis.windows.net"


def test_guids_are_remapped_consistently_to_synthetic_ones() -> None:
    s = Sanitiser()
    first, second = s.guid(SAMPLE_GUID), s.guid(OTHER_GUID)
    assert SYNTHETIC_GUID.fullmatch(first) and SYNTHETIC_GUID.fullmatch(second)
    assert first != second
    assert s.guid(SAMPLE_GUID.upper()) == first  # case-insensitive, stable
    assert s.guid(first) == first  # idempotent


def test_references_survive_inside_urls_and_free_text() -> None:
    s = Sanitiser()
    out = s.value({"url": f"/v1/workspaces/{SAMPLE_GUID}/items", "message": f"workspace {SAMPLE_GUID} not found"})
    mapped = s.guid(SAMPLE_GUID)
    assert out == {"url": f"/v1/workspaces/{mapped}/items", "message": f"workspace {mapped} not found"}


def test_secrets_emails_and_tenant_hosts_are_scrubbed() -> None:
    s = Sanitiser()
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"  # JWT-shaped placeholder, not a credential
    text = s.text(
        f"Bearer abcdef123456 {token} owner jane.doe@contoso.example "
        f"host abcd1234.datawarehouse.fabric.microsoft.com {SAMPLE_TRIAL_CAPACITY}"
    )
    assert "abcdef123456" not in text and "eyJ" not in text
    assert "jane.doe" not in text and "contoso" not in text
    assert "abcd1234" not in text and "Trial-2099" not in text and "EXAMPLEEXAMPLE" not in text
    s.check(text)


def test_the_power_bi_cluster_host_is_remapped_to_a_synthetic_host() -> None:
    s = Sanitiser()
    out = s.text(f"https://{SAMPLE_CLUSTER}/v1.0/myorg/groups/{SAMPLE_GUID}/$metadata#refreshes")
    assert "wabi-" not in out and "example-region" not in out
    assert out.startswith("https://powerbi-cluster.example.invalid/v1.0/myorg/groups/")
    s.check(out)
    # the public Power BI resource and API hosts are not cluster hosts and stay as they are
    assert s.text("https://analysis.windows.net/powerbi/api") == "https://analysis.windows.net/powerbi/api"
    with pytest.raises(UnsafeFixtureError):
        Sanitiser.check(f"https://{SAMPLE_CLUSTER}/x")


@pytest.mark.parametrize(
    "leak",
    ["AZURE_CLIENT_SECRET=x", "client_secret", SAMPLE_GUID, "someone@example.com", f"https://{SAMPLE_CLUSTER}"],
)
def test_the_deny_list_rejects_leaks(leak: str) -> None:
    with pytest.raises(UnsafeFixtureError):
        Sanitiser.check(json.dumps({"value": leak}))


def test_names_that_identify_the_tenant_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    Sanitiser.check("Acme Contoso Consulting owned by Jane")  # not on any built-in list
    monkeypatch.setenv("FABRICOPS_FIXTURE_DENY", "acme contoso, jane")
    for leak in ("Acme Contoso Consulting", "owned by JANE"):
        with pytest.raises(UnsafeFixtureError):
            Sanitiser.check(leak)


def test_committed_fixtures_are_clean() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert files, "no recorded Fabric REST fixtures; run python -m tests.live.record_fixtures"
    for path in files:
        Sanitiser.check(path.read_text(encoding="utf-8"))  # raises on any real GUID, e-mail, token or cluster host
