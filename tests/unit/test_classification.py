"""Incident classification by deterministic error signatures, and taxonomy extensibility."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from orqentis.domain.taxonomy import TAXONOMY, IncidentCategory, validate_category
from orqentis.incidents.classification import ErrorClassifier, ErrorSignature


@pytest.fixture
def classifier() -> ErrorClassifier:
    return ErrorClassifier()


@pytest.mark.parametrize(
    ("message", "category", "entities"),
    [
        (
            'AnalysisException: [UNRESOLVED_COLUMN] expected column "customer_id" is missing from input schema of '
            "sales_orders_raw.",
            IncidentCategory.SCHEMA_DRIFT,
            {"column": "customer_id", "table": "sales_orders_raw"},
        ),
        ("cannot resolve 'order_ts' given input columns", IncidentCategory.SCHEMA_DRIFT, {"column": "order_ts"}),
        (
            "ErrorCode=UserErrorFailedToConnectToSource ... AADSTS7000222: The provided client secret keys for app "
            "'app-fabric-erp-reader' are expired.",
            IncidentCategory.AUTH_FAILURE,
            {"app": "app-fabric-erp-reader"},
        ),
        (
            "Spark session could not be started. Capacity 'Contoso PROD F64' has exceeded its limits "
            "(CapacityLimitExceeded, HTTP 430 TooManyRequestsForCapacity).",
            IncidentCategory.CAPACITY_PRESSURE,
            {"capacity": "Contoso PROD F64"},
        ),
        ("Request failed with 429 TooManyRequests", IncidentCategory.THROTTLING, {}),
        (
            "ConcurrentAppendException: Files were added to the root of the table",
            IncidentCategory.LAKEHOUSE_WRITE_FAILURE,
            {},
        ),
        ("Activity timed out after 12:00:00", IncidentCategory.PIPELINE_TIMEOUT, {}),
        ("Unable to connect to the remote server: connection refused", IncidentCategory.CONNECTIVITY_FAILURE, {}),
        ("The specified blob was not found", IncidentCategory.SOURCE_UNAVAILABLE, {}),
    ],
)
def test_signature_classification(
    classifier: ErrorClassifier, message: str, category: str, entities: dict[str, str]
) -> None:
    match = classifier.classify(message)
    assert match is not None
    assert match.category == category
    for key, value in entities.items():
        assert match.entities[key] == value


def test_unknown_error_is_not_classified(classifier: ErrorClassifier) -> None:
    assert classifier.classify("Activity failed with internal error code 0x80131500. Contact support.") is None


def test_highest_priority_signature_wins(classifier: ErrorClassifier) -> None:
    # Mentions connectivity *and* an Entra error: authentication is the more specific diagnosis.
    match = classifier.classify("Failed to connect to source: AADSTS50126 invalid username or password")
    assert match is not None and match.category == IncidentCategory.AUTH_FAILURE
    assert len(classifier.matches("Failed to connect to source: AADSTS50126")) >= 2


@pytest.fixture
def custom_category() -> Iterator[str]:
    name = TAXONOMY.register("DBT_TEST_FAILURE", "dbt data test failed")
    yield name
    TAXONOMY.unregister(name)


def test_taxonomy_is_extensible(classifier: ErrorClassifier, custom_category: str) -> None:
    classifier.register(
        ErrorSignature(
            id="SIG-DBT",
            category=custom_category,
            description="dbt test failure",
            patterns=[r"dbt test .+ failed"],
            priority=99,
        )
    )
    match = classifier.classify("dbt test not_null_orders_id failed")
    assert match is not None and match.category == "DBT_TEST_FAILURE"
    assert validate_category("dbt_test_failure") == "DBT_TEST_FAILURE"


def test_unregistered_category_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ErrorSignature(id="SIG-X", category="NOT_A_CATEGORY", description="x", patterns=["x"])
