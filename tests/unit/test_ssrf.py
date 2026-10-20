"""SSRF guard of the webhook adapters: URL rules, the allow list, address checks after DNS and the pinned connection."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable, Sequence

import httpx
import pytest

from orqentis.notifications import ssrf
from orqentis.notifications.port import DeliveryError
from orqentis.notifications.ssrf import WebhookPoster
from orqentis.notifications.urlcheck import (
    UnsafeDestinationError,
    check_webhook_url,
    classify_address,
    host_matches,
)

pytestmark = pytest.mark.security

SECRET_URL = "https://prod-12.westus.logic.azure.com:443/workflows/abc123/triggers/manual/paths/invoke?api-version=1&sig=TOPSECRETSIGNATURE"
PUBLIC = "20.190.151.3"
TEAMS_HOSTS = ("*.logic.azure.com",)
Handler = Callable[[httpx.Request], httpx.Response]


def resolving_to(*addresses: str) -> ssrf.Resolver:
    async def resolver(host: str, port: int) -> Sequence[str]:
        return list(addresses)

    return resolver


def poster(
    handler: Handler | None = None,
    *,
    url: str = SECRET_URL,
    resolver: ssrf.Resolver | None = None,
    allowed: Sequence[str] = TEAMS_HOSTS,
    timeout: float = 5.0,
) -> WebhookPoster:
    return WebhookPoster(
        url,
        allowed_hosts=allowed,
        timeout=timeout,
        resolver=resolver or resolving_to(PUBLIC),
        transport=httpx.MockTransport(handler or (lambda request: httpx.Response(202))),
    )


async def refused(webhook: WebhookPoster) -> DeliveryError:
    with pytest.raises(DeliveryError) as caught:
        await webhook.post(b"{}", {"Content-Type": "application/json"})
    return caught.value


# ------------------------------------------------------------------------------------------------- the URL


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://prod-12.westus.logic.azure.com/workflows/x", "SCHEME_NOT_HTTPS"),
        ("ftp://prod-12.westus.logic.azure.com/x", "SCHEME_NOT_HTTPS"),
        ("//prod-12.westus.logic.azure.com/x", "SCHEME_NOT_HTTPS"),
        ("https://user:pass@prod-12.westus.logic.azure.com/x", "URL_HAS_CREDENTIALS"),
        ("https://prod-12.westus.logic.azure.com@evil.example/x", "URL_HAS_CREDENTIALS"),
        ("https://evil.example\\@prod-12.westus.logic.azure.com/x", "URL_INVALID"),
        ("https://prod-12.westus.logic.azure.com/x y", "URL_INVALID"),
        ("https://prod-12.westus.logic.azure.com/x\r\nHost: evil.example", "URL_INVALID"),
        ("https://prod-12.westus.logic.azure.com:notaport/x", "URL_INVALID"),
        ("https://prod-12.westus.logic.azure.com:99999/x", "URL_INVALID"),
        ("https://" + chr(0x440) + "rod-12.westus.logic.azure.com/x", "URL_INVALID"),  # non-ASCII look-alike letter
        ("https:///x", "URL_INVALID"),
        ("", "URL_INVALID"),
        ("https://10.0.0.5/hook", "HOST_IS_IP_LITERAL"),
        ("https://127.0.0.1/hook", "HOST_IS_IP_LITERAL"),
        ("https://169.254.169.254/latest/meta-data", "HOST_IS_IP_LITERAL"),
        ("https://[::1]/hook", "HOST_IS_IP_LITERAL"),
        ("https://[fd00:ec2::254]/hook", "HOST_IS_IP_LITERAL"),
        ("https://localhost/hook", "HOST_NOT_ALLOWED"),
        ("https://metadata.localhost/hook", "HOST_NOT_ALLOWED"),
    ],
)
def test_unacceptable_urls_are_refused_without_the_network(url: str, code: str) -> None:
    with pytest.raises(UnsafeDestinationError) as caught:
        check_webhook_url(url)
    assert caught.value.code == code


def test_the_url_is_parsed_into_host_port_and_path_with_the_query_untouched() -> None:
    target = check_webhook_url(SECRET_URL, allowed_hosts=TEAMS_HOSTS)
    assert (target.host, target.port) == ("prod-12.westus.logic.azure.com", 443)
    assert (
        target.path_and_query == "/workflows/abc123/triggers/manual/paths/invoke?api-version=1&sig=TOPSECRETSIGNATURE"
    )
    assert target.host_header == "prod-12.westus.logic.azure.com"
    other = check_webhook_url("https://Hooks.Example.COM.:8443/in")
    assert (other.host, other.port, other.host_header) == ("hooks.example.com", 8443, "hooks.example.com:8443")


# ---------------------------------------------------------------------------------------------- allow list


@pytest.mark.parametrize(
    ("host", "patterns", "expected"),
    [
        ("prod-12.westus.logic.azure.com", ("*.logic.azure.com",), True),
        ("a.b.c.logic.azure.com", ("*.logic.azure.com",), True),
        ("PROD-12.WESTUS.LOGIC.AZURE.COM.", ("*.logic.azure.com",), True),
        ("logic.azure.com", ("*.logic.azure.com",), False),  # the apex is not "below" the suffix
        ("evil-logic.azure.com", ("*.logic.azure.com",), False),  # label boundary, not a string suffix
        ("logic.azure.com.evil.example", ("*.logic.azure.com",), False),
        ("evil.example", ("*.logic.azure.com",), False),
        ("hooks.example.com", ("hooks.example.com",), True),
        ("x.hooks.example.com", ("hooks.example.com",), False),  # an exact pattern matches exactly
        ("x.hooks.example.com", (".hooks.example.com",), True),  # a leading dot is a suffix pattern
        ("anything.example", (), False),
        ("anything.example", ("", " "), False),
    ],
)
def test_host_patterns(host: str, patterns: Sequence[str], expected: bool) -> None:
    assert host_matches(host, patterns) is expected


def test_the_allow_list_is_enforced_when_the_poster_is_built() -> None:
    with pytest.raises(UnsafeDestinationError) as caught:
        poster(url="https://evil.example/hook?sig=SECRETSIG")
    assert caught.value.code == "HOST_NOT_ALLOWED"
    assert "SECRETSIG" not in str(caught.value)
    poster(url="https://prod-99.eastus.logic.azure.com/x")  # on the list: fine
    poster(url="https://anything.example/x", allowed=())  # no list: any public https host


# ------------------------------------------------------------------------------- addresses after DNS


@pytest.mark.parametrize(
    "address",
    [
        "10.0.0.5",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.254",
        "192.168.1.1",
        "127.0.0.1",
        "127.1.2.3",
        "0.0.0.0",  # noqa: S104 - the address under test
        "169.254.169.254",  # instance metadata
        "169.254.1.1",
        "168.63.129.16",  # Azure WireServer / host endpoint (public range, but must never be called)
        "169.254.170.2",
        "100.64.0.1",  # carrier-grade NAT
        "100.100.100.200",
        "192.0.0.192",
        "198.18.0.1",
        "224.0.0.1",
        "240.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fd00::1",
        "fd00:ec2::254",
        "::ffff:10.0.0.1",  # IPv4-mapped
        "::ffff:127.0.0.1",
        "::ffff:8.8.8.8",  # mapped public address: refused as well, nobody needs the form
        "64:ff9b::a00:1",  # NAT64
        "2002:c0a8:0101::1",  # 6to4 embedding 192.168.1.1
        "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
        "ff02::1",
        "fec0::1",  # IPv6 site-local (deprecated)
        "fec0:0:0:1::53",
        "febf::1",  # link-local edge
        "192.88.99.1",  # 6to4 relay anycast (deprecated)
        "192.88.99.255",
        "100::1",  # discard-only
        "2001:db8::1",  # documentation
        "3fff::1",  # documentation
        "5f00::1",  # SRv6 SIDs
        "2001:10::1",  # ORCHID (deprecated)
    ],
)
def test_non_public_addresses_are_classified(address: str) -> None:
    assert classify_address(ipaddress.ip_address(address)) is not None, address


@pytest.mark.parametrize(
    "address", ["20.190.151.3", "52.123.45.6", "8.8.8.8", "2603:1030:408:5::2", "2a01:111:f100::1"]
)
def test_public_addresses_are_accepted(address: str) -> None:
    assert classify_address(ipaddress.ip_address(address)) is None


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "127.0.0.1", "169.254.169.254", "168.63.129.16", "::1", "fe80::1", "fd00::1", "::ffff:10.0.0.1"],
)
async def test_a_name_that_resolves_to_a_non_public_address_is_refused_before_any_connection(address: str) -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(202)

    error = await refused(poster(handler, resolver=resolving_to(address)))
    assert (error.code, error.retryable) == ("DESTINATION_NOT_PUBLIC", False)
    assert called == []  # nothing was sent


async def test_one_non_public_answer_among_public_ones_refuses_the_whole_call() -> None:
    called: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(202)

    error = await refused(poster(handler, resolver=resolving_to(PUBLIC, "10.1.2.3", "20.190.151.4")))
    assert error.code == "DESTINATION_NOT_PUBLIC" and called == []


@pytest.mark.parametrize(
    ("alias", "resolved"),
    [("2130706433", "127.0.0.1"), ("0x7f.1", "127.0.0.1"), ("127.1", "127.0.0.1"), ("0177.0.0.1", "127.0.0.1")],
)
async def test_numeric_host_spellings_are_caught_after_resolution(alias: str, resolved: str) -> None:
    # `getaddrinfo` turns these into loopback; the URL looks like a name, so only the address check can catch them.
    webhook = WebhookPoster(
        f"https://{alias}/hook",
        resolver=resolving_to(resolved),
        transport=httpx.MockTransport(lambda r: httpx.Response(202)),
    )
    assert (await refused(webhook)).code == "DESTINATION_NOT_PUBLIC"


async def test_dns_rebinding_is_not_possible_because_the_checked_address_is_the_one_connected_to() -> None:
    """The first lookup is public, any later one is private: the call must use the first, and never look again."""
    lookups: list[str] = []

    async def rebinding(host: str, port: int) -> Sequence[str]:
        lookups.append(host)
        return [PUBLIC] if len(lookups) == 1 else ["169.254.169.254"]

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    webhook = poster(handler, resolver=rebinding)
    assert await webhook.post(b"{}", {}) == 202
    assert len(lookups) == 1  # one resolution per call: no gap between the check and the connection
    assert [r.url.host for r in seen] == [PUBLIC]  # the request went to the address that was validated
    # The next call resolves again, gets the private answer, and is refused.
    assert (await refused(webhook)).code == "DESTINATION_NOT_PUBLIC"
    assert len(seen) == 1


async def test_the_flip_the_other_way_is_refused_the_first_time() -> None:
    answers = iter([["10.0.0.9"], [PUBLIC]])

    async def resolver(host: str, port: int) -> Sequence[str]:
        return next(answers)

    assert (await refused(poster(resolver=resolver))).code == "DESTINATION_NOT_PUBLIC"


# ----------------------------------------------------------------------------------- the connection


async def test_the_request_is_pinned_to_the_validated_address_with_the_real_name_for_host_and_tls() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    assert await poster(handler).post(b'{"a":1}', {"Content-Type": "application/json", "X-Custom": "1"}) == 202
    (request,) = seen
    assert request.method == "POST" and request.content == b'{"a":1}'
    assert request.url.scheme == "https" and request.url.host == PUBLIC and request.url.port in (None, 443)
    assert (
        request.url.raw_path.decode()
        == "/workflows/abc123/triggers/manual/paths/invoke?api-version=1&sig=TOPSECRETSIGNATURE"
    )
    assert request.headers["host"] == "prod-12.westus.logic.azure.com"
    assert (
        request.extensions["sni_hostname"] == "prod-12.westus.logic.azure.com"
    )  # certificate is checked for this name
    assert request.headers["x-custom"] == "1"


async def test_ipv6_addresses_and_ports_are_formatted_for_the_connection() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    webhook = poster(
        handler, url="https://hooks.example.com:8443/in?x=1", allowed=(), resolver=resolving_to("2603:1030:408:5::2")
    )
    await webhook.post(b"{}", {})
    assert seen[0].url.host == "2603:1030:408:5::2" and seen[0].url.port == 8443
    assert seen[0].headers["host"] == "hooks.example.com:8443"


async def test_the_client_never_follows_redirects_and_ignores_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []
    real = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        created.append(dict(kwargs))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ssrf.httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    await poster().post(b"{}", {})
    assert created and created[0]["follow_redirects"] is False and created[0]["trust_env"] is False


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_refused_and_not_followed(status: int) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    error = await refused(poster(handler))
    assert (error.code, error.retryable, error.status_code) == ("REDIRECT_REFUSED", False, status)
    assert len(calls) == 1  # the Location was never requested


@pytest.mark.parametrize(
    ("status", "retryable"),
    [
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        (408, True),
        (429, True),
        (400, False),
        (401, False),
        (403, False),
        (404, False),
    ],
)
async def test_http_statuses_are_classified(status: int, retryable: bool) -> None:
    error = await refused(poster(lambda request: httpx.Response(status, headers={"Retry-After": "7"})))
    assert error.code == f"HTTP_{status}" and error.retryable is retryable and error.status_code == status
    assert error.retry_after == (7.0 if retryable else None)


@pytest.mark.parametrize("status", [200, 201, 202, 204])
async def test_success_statuses(status: int) -> None:
    assert await poster(lambda request: httpx.Response(status)).post(b"{}", {}) == status


async def test_retry_after_is_capped_and_http_dates_are_ignored() -> None:
    huge = await refused(poster(lambda request: httpx.Response(429, headers={"Retry-After": "999999"})))
    assert huge.retry_after == 600.0
    dated = await refused(
        poster(lambda request: httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}))
    )
    assert dated.retry_after is None


async def test_timeouts_and_network_errors_are_retryable() -> None:
    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def breaks(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("reset", request=request)

    assert (await refused(poster(times_out))).code == "TIMEOUT"
    broken = await refused(poster(breaks))
    assert broken.code == "NETWORK_ERROR" and broken.retryable


async def test_a_slow_resolver_is_bounded_by_the_overall_deadline() -> None:
    async def stalled(host: str, port: int) -> Sequence[str]:
        await asyncio.sleep(3600)
        return [PUBLIC]

    error = await refused(poster(resolver=stalled, timeout=0.05))
    assert (error.code, error.retryable) == ("TIMEOUT", True)


async def test_a_failed_lookup_is_retryable_but_an_empty_one_too() -> None:
    async def failing(host: str, port: int) -> Sequence[str]:
        raise OSError("Name or service not known")

    assert (await refused(poster(resolver=failing))).code == "DNS_FAILED"
    assert (await refused(poster(resolver=resolving_to()))).retryable is True


async def test_the_next_validated_address_is_tried_when_a_connection_fails() -> None:
    attempted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted.append(request.url.host)
        if request.url.host == PUBLIC:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(202)

    assert await poster(handler, resolver=resolving_to(PUBLIC, "52.1.2.3")).post(b"{}", {}) == 202
    assert attempted == [PUBLIC, "52.1.2.3"]


async def test_when_every_address_fails_to_connect_the_error_is_retryable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    error = await refused(poster(handler, resolver=resolving_to(PUBLIC, "52.1.2.3")))
    assert (error.code, error.retryable) == ("CONNECT_FAILED", True)


# ---------------------------------------------------------------------------------- the URL is a secret


async def test_the_secret_path_and_query_never_reach_the_log_even_when_the_client_logs_requests(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs ``HTTP Request: POST <url>`` at INFO; raising the level must not put the webhook secret in a log."""
    with caplog.at_level(logging.DEBUG):
        assert await poster().post(b"{}", {}) == 202
        logging.getLogger("httpx").warning("an unrelated warning of the client")  # kept: only the secret is dropped
        logging.getLogger("httpx").info("an unrelated info record of the client")  # below the pinned level
    text = " ".join(record.getMessage() for record in caplog.records)
    assert "TOPSECRETSIGNATURE" not in text and "/workflows/abc123" not in text
    assert "an unrelated warning of the client" in text and "an unrelated info record" not in text


@pytest.mark.parametrize(
    "path",
    [
        "/work flows/a%20b/c%2Fd?sig=SECRET%2FSIG%3D%3D&x=%7Bbrace%7D",  # characters the client re-encodes when it logs
        "/workflows/a%2fb?sig=sECRET%2bSIG",
    ],
)
async def test_the_client_loggers_are_pinned_so_a_reencoded_url_cannot_be_logged(
    caplog: pytest.LogCaptureFixture, path: str
) -> None:
    """The filter compares strings and the client logs the URL re-encoded, so the filter cannot be the only guard."""
    logging.getLogger("httpx").setLevel(logging.NOTSET)  # as an operator who raised the log level would
    logging.getLogger("httpcore").setLevel(logging.NOTSET)
    webhook = poster(url=f"https://hooks.example.com{path}".replace(" ", "%20"), allowed=())
    with caplog.at_level(logging.DEBUG):
        assert await webhook.post(b"{}", {}) == 202
    assert (
        logging.getLogger("httpx").level == logging.WARNING and logging.getLogger("httpcore").level == logging.WARNING
    )
    shown = " ".join(record.getMessage() for record in caplog.records if record.name.startswith(("httpx", "httpcore")))
    assert shown == "" and "SECRET" not in shown.upper()


async def test_the_url_never_appears_in_an_error() -> None:
    errors: list[BaseException] = []
    for handler in (
        lambda request: httpx.Response(500),
        lambda request: httpx.Response(302, headers={"Location": SECRET_URL}),
        lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("x", request=request)),
    ):
        errors.append(await refused(poster(handler)))
    errors.append(await refused(poster(resolver=resolving_to("10.0.0.1"))))
    for error in errors:
        text = f"{error!s} {error!r} {error.__dict__}"
        assert "TOPSECRETSIGNATURE" not in text and "abc123" not in text and "?" not in str(error)
    assert poster().destination == "prod-12.westus.logic.azure.com"
