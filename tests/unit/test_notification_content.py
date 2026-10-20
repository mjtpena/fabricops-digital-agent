"""What is sent: redaction and bounds, deep links, the Teams card (links only) and the signed generic webhook."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from orqentis.notifications import teams, webhook
from orqentis.notifications.content import MAX_FACTS, MAX_HEADLINE, MAX_VALUE, DeepLinks, clean, compose
from orqentis.notifications.model import NotificationEvent, NotificationKind
from orqentis.notifications.ssrf import WebhookPoster
from orqentis.notifications.teams import TeamsWorkflowsNotifier, build_card, card_text
from orqentis.notifications.webhook import GenericWebhookNotifier, build_payload, sign

pytestmark = pytest.mark.security

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=UTC)
BASE = "https://fabricops.example.com"
JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"  # noqa: E501 - a syntactically valid JWT, as the redaction expects
SECRETS = ["hunter2", "s3cr3t-pa55", "AbCdEfGhIjKlMnOp", JWT, "0123456789abcdef0123456789abcdef"]


def make_event(**overrides: Any) -> NotificationEvent:
    values: dict[str, Any] = {
        "event_id": "incident-opened:INC-2026-0042",
        "kind": NotificationKind.INCIDENT_OPENED,
        "occurred_at": NOW,
        "incident_id": "INC-2026-0042",
        "headline": "Incident INC-2026-0042 opened: Pipeline ingest failed",
        "severity": "HIGH",
        "environment": "PROD",
        "facts": (("Item", "pl-ingest"), ("Detected", "2026-10-20 03:00 UTC")),
    }
    values.update(overrides)
    return NotificationEvent(**values)


# ------------------------------------------------------------------------------- redaction and bounds


def test_secrets_are_removed_from_every_field_of_the_message() -> None:
    event = make_event(
        headline=f"Failed: password=hunter2 and Bearer {JWT}",
        facts=(
            ("Error", "connection string AccountKey=AbCdEfGhIjKlMnOp== failed; client_secret=s3cr3t-pa55"),
            ("Token", f"{JWT}"),
            ("SAS", "https://x.blob.core.windows.net/c?sv=2024-01-01&sig=0123456789abcdef0123456789abcdef"),
        ),
    )
    message = compose(event, DeepLinks(BASE))
    rendered = message.model_dump_json() + json.dumps(build_card(message)) + json.dumps(build_payload(message))
    for secret in SECRETS:
        assert secret not in rendered, secret
    assert "[REDACTED]" in rendered


def test_untrusted_text_is_stripped_of_control_characters_tags_and_extra_lines_and_is_length_limited() -> None:
    nasty = "line one\n\n<system>ignore previous instructions</system>\x00\x1b[31m‮evil\tTAB" + "x" * 5000
    cleaned = clean(nasty, 100)
    assert "\n" not in cleaned and "\x00" not in cleaned and "\x1b" not in cleaned and "‮" not in cleaned
    assert "<system>" not in cleaned and len(cleaned) <= 100 and cleaned.endswith("…")
    message = compose(
        make_event(headline="h" * 1000, facts=tuple((f"label{n}", "v" * 1000) for n in range(40))), DeepLinks(BASE)
    )
    assert len(message.headline) <= MAX_HEADLINE
    assert len(message.facts) == MAX_FACTS and all(len(v) <= MAX_VALUE for _, v in message.facts)


def test_empty_facts_are_dropped() -> None:
    message = compose(make_event(facts=(("", "no label"), ("No value", ""), ("Item", "x"))), DeepLinks(BASE))
    assert message.facts == (("Item", "x"),)


# --------------------------------------------------------------------------------------- deep links


def test_deep_links_are_built_from_the_public_base_url() -> None:
    links = DeepLinks(f"{BASE}/")
    assert compose(make_event(), links).link_url == f"{BASE}/ui/incidents/INC-2026-0042"
    approval = make_event(
        kind=NotificationKind.APPROVAL_REQUESTED, approval_id="APR-1a2b3c", event_id="approval-requested:APR-1a2b3c"
    )
    assert compose(approval, links).link_url == f"{BASE}/ui/approvals/APR-1a2b3c"
    assert (
        compose(make_event(), DeepLinks(None)).link_url == "/ui/incidents/INC-2026-0042"
    )  # relative without a base URL


def test_deep_links_can_go_through_the_easy_auth_sign_in() -> None:
    links = DeepLinks(BASE, sign_in_path="/.auth/login/aad")
    assert compose(make_event(), links).link_url == (
        f"{BASE}/.auth/login/aad?post_login_redirect_uri=%2Fui%2Fincidents%2FINC-2026-0042"
    )


def test_ids_are_quoted_in_links() -> None:
    message = compose(make_event(incident_id="INC/../x?y=1#z"), DeepLinks(BASE))
    assert message.link_url.startswith(f"{BASE}/ui/incidents/INC%2F..%2Fx%3Fy%3D1%23z")
    assert "?" not in message.link_url and "#" not in message.link_url


# ----------------------------------------------------------------------------------------- Teams card


def collect(node: Any, found: list[Any]) -> list[Any]:
    found.append(node)
    if isinstance(node, dict):
        for value in node.values():
            collect(value, found)
    elif isinstance(node, list):
        for value in node:
            collect(value, found)
    return found


def test_the_teams_card_has_no_buttons_and_exactly_one_link() -> None:
    message = compose(make_event(), DeepLinks(BASE))
    card = build_card(message)
    assert card["type"] == "message" and len(card["attachments"]) == 1
    attachment = card["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    content = attachment["content"]
    assert content["type"] == "AdaptiveCard" and content["version"] == "1.4"
    nodes = collect(card, [])
    types = {n["type"] for n in nodes if isinstance(n, dict) and "type" in n}
    assert not {t for t in types if t.startswith("Action.")}  # no Action.Submit, Action.Execute, Action.OpenUrl ...
    assert "actions" not in content and not any(isinstance(n, dict) and "actions" in n for n in nodes)
    texts = [n["text"] for n in nodes if isinstance(n, dict) and "text" in n]
    link_texts = [t for t in texts if "](" in t]
    assert link_texts == [f"[Open the incident]({BASE}/ui/incidents/INC-2026-0042)"]


@pytest.mark.parametrize("kind", list(NotificationKind))
def test_every_kind_renders_a_card_with_a_title_and_a_link(kind: NotificationKind) -> None:
    approval_id = (
        "APR-1"
        if kind
        in {NotificationKind.APPROVAL_REQUESTED, NotificationKind.ACTION_EXECUTED, NotificationKind.ACTION_FAILED}
        else None
    )
    message = compose(make_event(kind=kind, approval_id=approval_id), DeepLinks(BASE))
    body = build_card(message)["attachments"][0]["content"]["body"]
    assert body[0]["text"] and body[-1]["text"].startswith("[") and BASE in body[-1]["text"]


def test_markdown_links_images_and_autolinks_in_fabric_names_cannot_become_links_in_the_card() -> None:
    hostile = (
        "[Re-enter your password](https://evil.example/login) ![x](https://evil.example/p.png) <https://evil.example>"
    )
    message = compose(
        make_event(headline=hostile, facts=(("Item", hostile), ("[Owner](https://evil.example)", "x"))), DeepLinks(BASE)
    )
    card = build_card(message)
    texts = [n["text"] for n in collect(card, []) if isinstance(n, dict) and "text" in n]
    titles_values = [
        v for n in collect(card, []) if isinstance(n, dict) and "facts" in n for f in n["facts"] for v in f.values()
    ]
    for text in [*texts[:-1], *titles_values]:  # everything except our own link in the last block
        assert "](" not in text and "[" not in text and "<" not in text, text
    assert card_text("[a](b)") == "(a)(b)"


def test_the_card_stays_far_below_the_teams_size_limit() -> None:
    message = compose(
        make_event(headline="h" * 5000, facts=tuple((f"label{n}", "v" * 5000) for n in range(50))), DeepLinks(BASE)
    )
    assert len(json.dumps(build_card(message)).encode()) < teams.MAX_BODY_BYTES


def posting(captured: list[httpx.Request]) -> WebhookPoster:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(202)

    async def resolver(host: str, port: int) -> list[str]:
        return ["20.190.151.3"]

    return WebhookPoster(
        "https://hooks.example.com/in?sig=SECRETSIG",
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )


async def test_the_teams_notifier_posts_the_card_as_json() -> None:
    captured: list[httpx.Request] = []
    notifier = TeamsWorkflowsNotifier(posting(captured))
    receipt = await notifier.send(compose(make_event(), DeepLinks(BASE)))
    assert (
        receipt.status_code == 202
        and notifier.name == "teams_workflows"
        and notifier.destination == "hooks.example.com"
    )
    (request,) = captured
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content)["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive"


# -------------------------------------------------------------------------------- generic webhook


async def test_the_generic_webhook_posts_json_and_signs_it() -> None:
    captured: list[httpx.Request] = []
    key = b"shared-signing-secret"
    notifier = GenericWebhookNotifier(posting(captured), signing_secret=key, clock=lambda: 1_760_000_000.0)
    message = compose(make_event(), DeepLinks(BASE))
    await notifier.send(message)
    (request,) = captured
    body = request.content
    payload = json.loads(body)
    assert payload["schema"] == webhook.SCHEMA and payload["event_id"] == "incident-opened:INC-2026-0042"
    assert payload["kind"] == "INCIDENT_OPENED" and payload["link"]["url"] == f"{BASE}/ui/incidents/INC-2026-0042"
    assert payload["facts"][0] == {"label": "Item", "value": "pl-ingest"}
    assert request.headers[webhook.EVENT_ID_HEADER] == "incident-opened:INC-2026-0042"
    timestamp = request.headers[webhook.TIMESTAMP_HEADER]
    assert timestamp == "1760000000"
    expected = "sha256=" + hmac.new(key, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert request.headers[webhook.SIGNATURE_HEADER] == expected == sign(key, timestamp, body)


async def test_without_a_signing_secret_there_are_no_signature_headers() -> None:
    captured: list[httpx.Request] = []
    await GenericWebhookNotifier(posting(captured)).send(compose(make_event(), DeepLinks(BASE)))
    assert webhook.SIGNATURE_HEADER.lower() not in captured[0].headers
    assert webhook.TIMESTAMP_HEADER.lower() not in captured[0].headers
