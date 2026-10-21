"""``signature_match`` is our own error classification, not a signature: it must survive redaction. Real ones must not."""

from __future__ import annotations

import pytest

from orqentis.incidents.classification import ErrorClassifier
from orqentis.observability.redaction import REDACTED, redact

SECRETISH = [
    "signature",
    "Signature",
    "x-ms-signature",
    "sas_signature",
    "sasToken",
    "request_signature",
    "signature_value",  # a new key that merely contains "signature" is redacted until someone decides otherwise
    "signature_matches",
    "password",
    "client_secret",
    "token",
    "connection_string",
    "Authorization",
]


@pytest.mark.parametrize("key", SECRETISH)
def test_keys_that_look_like_secrets_are_still_redacted(key: str) -> None:
    assert redact({key: "value"}) == {key: REDACTED}
    assert redact({key: {"nested": "value"}}) == {key: REDACTED}


def test_the_classification_metadata_of_an_error_is_kept() -> None:
    match = ErrorClassifier().classify("AnalysisException: column `customer_id` cannot be resolved [UNRESOLVED_COLUMN]")
    assert match is not None
    payload = {
        "message": "boom",
        "signature_match": match.model_dump(),
        "unconfirmed_signature": "SIG-X",
        "signature_id": "SIG-Y",
    }
    out = redact(payload)
    assert out["signature_match"]["signature_id"] == match.signature_id
    assert out["signature_match"]["category"] == "SCHEMA_DRIFT"
    assert out["unconfirmed_signature"] == "SIG-X" and out["signature_id"] == "SIG-Y"


def test_what_is_kept_is_still_scanned_for_secrets() -> None:
    out = redact(
        {"signature_match": {"matched_text": "client_secret=hunter2-very-secret", "entities": {"password": "p"}}}
    )
    assert "hunter2" not in str(out)
    assert out["signature_match"]["entities"]["password"] == REDACTED  # a sensitive key inside is redacted as usual
