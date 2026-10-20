"""Power BI wraps identifiers in its messages in privacy markers. End-user and customer-content markers are
redacted (never passed to evidence, logs, audit or a model); organization-identifier markers are unwrapped."""

from __future__ import annotations

import json

import httpx
import pytest

from orqentis.fabric.errors import FabricAuthError
from orqentis.fabric.rest.client import _failure, _refresh_error
from orqentis.fabric.rest.http import FabricHttp, parse_error_body, scrub_markers, strip_privacy_tags

SUBJECT = "00000000-0000-4000-8000-0000000000aa"


@pytest.mark.parametrize("tag", ["eupi", "euii", "pii", "pi", "ccon", "EUPI", "Euii"])
def test_end_user_and_customer_content_markers_are_redacted(tag: str) -> None:
    assert strip_privacy_tags(f"user <{tag}>{SUBJECT}</{tag}> denied") == "user [redacted] denied"


@pytest.mark.parametrize("tag", ["oii", "ii", "OII"])
def test_organization_markers_are_unwrapped_keeping_the_content(tag: str) -> None:
    assert strip_privacy_tags(f"table '<{tag}>customers_clean</{tag}>' missing") == "table 'customers_clean' missing"


def test_markers_inside_json_strings_with_escaped_closers_are_handled() -> None:
    raw = r"table '<oii>sales<\/oii>' for <eupi>abc<\/eupi>"
    assert strip_privacy_tags(raw) == "table 'sales' for [redacted]"


def test_an_unclosed_end_user_marker_redacts_everything_after_it() -> None:
    assert strip_privacy_tags(f"failed for <euii>{SUBJECT} and more text") == "failed for [redacted]"
    assert strip_privacy_tags("stray </eupi> closer and </oii> too") == "stray  closer and  too"


def test_several_markers_in_one_message() -> None:
    text = "<oii>t1</oii> read by <eupi>u1</eupi> and <ccon>row 7: secret</ccon> via <oii>t2</oii>"
    assert strip_privacy_tags(text) == "t1 read by [redacted] and [redacted] via t2"


def test_text_without_markers_is_untouched() -> None:
    for text in ("no markers here", "a <b>bold</b> move", "if a < b > c", "<pie>not a marker</pie>", ""):
        assert strip_privacy_tags(text) == text


def test_scrub_markers_walks_nested_json() -> None:
    value = {"a": ["x <eupi>1</eupi>", {"b": "<oii>n</oii>"}], "n": 3, "none": None}
    assert scrub_markers(value) == {"a": ["x [redacted]", {"b": "n"}], "n": 3, "none": None}


def test_activity_and_job_errors_are_scrubbed_too() -> None:
    failure = _failure({"message": f"denied for <eupi>{SUBJECT}</eupi> on <oii>customers</oii>", "errorCode": "2108"})
    assert (
        failure is not None and failure.message == "denied for [redacted] on customers" and failure.error_code == "2108"
    )
    assert _failure({"message": f"<eupi>{SUBJECT}</eupi>", "errorCode": ""}).message == "[redacted]"  # type: ignore[union-attr]
    assert _failure({"message": "", "errorCode": ""}) is None


def test_refresh_service_exceptions_are_scrubbed() -> None:
    payload = json.dumps({"errorCode": "X", "errorDescription": f"table '<oii>t</oii>' by <eupi>{SUBJECT}</eupi>"})
    error = _refresh_error(payload)
    assert error is not None and error.message == "table 't' by [redacted]" and error.error_code == "X"
    assert _refresh_error("plain <eupi>x</eupi> text").message == "plain [redacted] text"  # type: ignore[union-attr]


def test_error_body_details_are_scrubbed() -> None:
    body = {
        "error": {
            "code": "Unauthorized",
            "message": f"user <eupi>{SUBJECT}</eupi>",
            "details": [{"message": "see <eupi>x</eupi> and <oii>y</oii>"}],
            "pbi.error": {"code": "c", "parameters": {"who": "<euii>a@b.example</euii>"}},
        }
    }
    code, message, _, details = parse_error_body(
        httpx.Response(403, json=body, request=httpx.Request("GET", "http://x"))
    )
    assert (code, message) == ("Unauthorized", "user [redacted]")
    serialised = json.dumps(details)
    assert (
        "[redacted]" in serialised
        and "<" not in serialised
        and "a@b.example" not in serialised
        and SUBJECT not in serialised
    )


class _Tokens:
    async def get_token(self, scope: str) -> str:
        return "t"


async def test_a_403_with_an_end_user_marker_never_exposes_the_subject() -> None:
    body = {
        "error": {
            "code": "Unauthorized",
            "message": f"Api accessed by user <eupi>{SUBJECT}</eupi> with insufficient privileges",
        }
    }
    http = FabricHttp(
        _Tokens(), max_retries=0, transport=httpx.MockTransport(lambda request: httpx.Response(403, json=body))
    )
    try:
        with pytest.raises(FabricAuthError) as caught:
            await http.get_json("https://api.powerbi.com/v1.0/myorg/groups/g/datasets/d/refreshes", scope="s")
    finally:
        await http.aclose()
    assert SUBJECT not in caught.value.message and "[redacted]" in caught.value.message
    assert SUBJECT not in json.dumps(caught.value.to_dict())
