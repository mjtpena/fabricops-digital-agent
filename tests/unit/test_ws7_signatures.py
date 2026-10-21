"""Error signatures added for the live findings: Spark's own column message, notebook exceptions, refresh errors.

The messages are the forms Fabric returned in the sandbox (sanitised): the notebook activity nests the exception inside a
JSON string, so its quotes arrive escaped.
"""

from __future__ import annotations

import json

import pytest

from orqentis.domain.taxonomy import IncidentCategory
from orqentis.incidents.classification import ErrorClassifier


def fabric_notebook_error(name: str, value: str) -> str:
    detail = json.dumps(
        {
            "errorCode": "200",
            "errorName": name,
            "errorValue": value,
            "message": "Notebook execution is in Failed state, runId: r-1",
        }
    )
    return (
        "Operation on target transform_customers failed: Notebook execution failed at Notebook service with http status "
        f"code - '200', please check the Run logs on Notebook, additional details - '{detail}' : "
    )


@pytest.fixture
def classifier() -> ErrorClassifier:
    return ErrorClassifier()


def test_a_generic_notebook_exception_is_an_activity_failure_with_its_exception(classifier: ErrorClassifier) -> None:
    match = classifier.classify(
        fabric_notebook_error("RuntimeError", "fabricops-sandbox injected failure: it raised on purpose")
    )
    assert match is not None and match.signature_id == "SIG-NOTEBOOK-EXCEPTION"
    assert match.category == IncidentCategory.ACTIVITY_FAILURE
    assert match.entities["exception"] == "RuntimeError"
    assert match.entities["exception_message"] == "fabricops-sandbox injected failure: it raised on purpose"


@pytest.mark.parametrize(
    ("message", "exception"),
    [
        (
            'Traceback (most recent call last):\n  File "x.py", line 3\nValueError: could not convert string to float',
            "ValueError",
        ),
        ("py4j.protocol.Py4JJavaError: An error occurred while calling o44.save.", "Py4JJavaError"),
        ("org.apache.spark.SparkException: Job aborted due to stage failure", "SparkException"),
        ("KeyError: 'unit_cost'", "KeyError"),
    ],
)
def test_python_and_spark_traceback_shapes_are_recognised(
    classifier: ErrorClassifier, message: str, exception: str
) -> None:
    match = classifier.classify(message)
    assert match is not None and match.category == IncidentCategory.ACTIVITY_FAILURE
    assert match.entities.get("exception", exception) == exception


def test_spark_s_own_column_message_names_the_column_and_suggests_a_replacement(classifier: ErrorClassifier) -> None:
    value = (
        "[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column, variable, or function parameter with name `customer_id` cannot be "
        "resolved. Did you mean one of the following? [`client_id`, `full_name`, `country`]. SQLSTATE: 42703"
    )
    match = classifier.classify(fabric_notebook_error("AnalysisException", value))
    assert match is not None and match.category == IncidentCategory.SCHEMA_DRIFT  # not the generic exception
    assert match.entities["column"] == "customer_id"  # the live run showed 'unknown'
    assert match.entities["suggestions"] == "`client_id`, `full_name`, `country`"


def test_the_unescaped_form_is_read_the_same_way(classifier: ErrorClassifier) -> None:
    text = "[UNRESOLVED_COLUMN.WITHOUT_SUGGESTION] A column, variable, or function parameter with name `order_ts` cannot be resolved."
    match = classifier.classify(text)
    assert match is not None and match.entities["column"] == "order_ts" and "suggestions" not in match.entities


def test_a_refresh_engine_error_is_a_refresh_failure(classifier: ErrorClassifier) -> None:
    text = (
        "We cannot access the source Delta table 'customers_clean_removed' referenced by table 'customers_clean'. Either the "
        "source Delta table does not exist, or you don't have access permissions."
    )
    match = classifier.classify(text)
    assert match is not None and match.category == IncidentCategory.SEMANTIC_MODEL_REFRESH_FAILURE
    assert match.entities == {"table": "customers_clean_removed", "model_table": "customers_clean"}
    short = classifier.classify("ModelRefresh_ShortMessage_ProcessingError: retry limit exceeded")
    assert short is not None and short.signature_id == "SIG-MODEL-REFRESH"


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Notebook execution failed: AADSTS7000222 client secret keys are expired", IncidentCategory.AUTH_FAILURE),
        (
            "Notebook execution failed: capacity exceeded its limits (CapacityLimitExceeded)",
            IncidentCategory.CAPACITY_PRESSURE,
        ),
        ("Notebook execution failed: the activity timed out", IncidentCategory.PIPELINE_TIMEOUT),
        ("RuntimeError: connection refused by the source", IncidentCategory.CONNECTIVITY_FAILURE),
    ],
)
def test_the_generic_exception_never_outranks_a_specific_cause(
    classifier: ErrorClassifier, message: str, category: str
) -> None:
    match = classifier.classify(message)
    assert match is not None and match.category == category
    assert any(m.signature_id == "SIG-NOTEBOOK-EXCEPTION" for m in classifier.matches(message))  # it matched, and lost


def test_an_opaque_error_stays_unknown(classifier: ErrorClassifier) -> None:
    assert classifier.classify("Activity failed with internal error code 0x80131500. Contact support.") is None
