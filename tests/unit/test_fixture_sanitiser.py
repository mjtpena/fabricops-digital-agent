"""The fixture sanitiser must never let tenant identity, tokens or e-mail addresses into the repository."""

from __future__ import annotations

import json

import pytest

from tests.live.record_fixtures import FIXTURE_DIR, SYNTHETIC_GUID, Sanitiser, UnsafeFixtureError

REAL_GUID = "70770688-7a50-4b08-821c-666bf3098897"
OTHER_GUID = "51ffa92a-e631-47fc-99df-01443849c3dd"


def test_guids_are_remapped_consistently_to_synthetic_ones() -> None:
    s = Sanitiser()
    first, second = s.guid(REAL_GUID), s.guid(OTHER_GUID)
    assert SYNTHETIC_GUID.fullmatch(first) and SYNTHETIC_GUID.fullmatch(second)
    assert first != second
    assert s.guid(REAL_GUID.upper()) == first  # case-insensitive, stable
    assert s.guid(first) == first  # idempotent


def test_references_survive_inside_urls_and_free_text() -> None:
    s = Sanitiser()
    out = s.value({"url": f"/v1/workspaces/{REAL_GUID}/items", "message": f"workspace {REAL_GUID} not found"})
    mapped = s.guid(REAL_GUID)
    assert out == {"url": f"/v1/workspaces/{mapped}/items", "message": f"workspace {mapped} not found"}


def test_secrets_emails_and_tenant_hosts_are_scrubbed() -> None:
    s = Sanitiser()
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"  # JWT-shaped placeholder, not a credential
    text = s.text(
        f"Bearer abcdef123456 {token} owner jane.doe@contoso.com "
        "host abcd1234.datawarehouse.fabric.microsoft.com Trial-20260612T214841Z-gJqg2v6lfESHWVqpWI3pXQ"
    )
    assert "abcdef123456" not in text and "eyJ" not in text
    assert "jane.doe" not in text and "contoso" not in text
    assert "abcd1234" not in text and "Trial-2026" not in text
    s.check(text)


@pytest.mark.parametrize(
    "leak",
    ["Datachain Consulting", "Michael", "AZURE_CLIENT_SECRET=x", REAL_GUID, "someone@example.com"],
)
def test_the_deny_list_rejects_leaks(leak: str) -> None:
    with pytest.raises(UnsafeFixtureError):
        Sanitiser.check(json.dumps({"value": leak}))


def test_committed_fixtures_are_clean() -> None:
    files = sorted(FIXTURE_DIR.glob("*.json"))
    assert files, "no recorded Fabric REST fixtures; run python -m tests.live.record_fixtures"
    for path in files:
        Sanitiser.check(path.read_text(encoding="utf-8"))  # raises on any real GUID, e-mail, token or deny-listed name
