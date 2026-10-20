"""The only way a webhook is called: a pinned, redirect-free HTTPS POST to a public address (SSRF guard).

A webhook URL is configuration, but configuration can be wrong, tampered with or point at a name an attacker controls,
and a notification service that posts to whatever it is told is a request-forgery primitive inside the VNet (the
Container Apps environment, the instance metadata services, other apps). Per call:

1. the URL must pass ``check_webhook_url`` (https, a DNS name, no credentials, allowed host);
2. the name is resolved **here** and **every** address it resolves to must be public (not private, loopback,
   link-local, multicast, reserved, cloud metadata, or an IPv6 range embedding an IPv4 address). One bad address
   refuses the whole call, so a DNS answer that mixes public and private records gains nothing;
3. the request goes to one of those **validated addresses**, not to the name, with the original name as ``Host`` and
   TLS server name. There is no second resolution between the check and the connection, which is what a DNS rebinding
   attack needs, and the certificate is still verified against the real host name;
4. redirects are never followed (a 3xx is a failure), proxies from the environment are ignored (a proxy would resolve
   and connect on its own), and the whole call has one deadline.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence

import httpx

from orqentis.notifications.port import DeliveryError
from orqentis.notifications.urlcheck import WebhookTarget, check_webhook_url, classify_address

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

_MAX_RETRY_AFTER_SECONDS = 600.0
_RETRYABLE_STATUS = frozenset({408, 425, 429})


class _KeepSecretOut(logging.Filter):
    """Drops the log records of the HTTP client that carry the webhook's path and query (the secret).

    ``httpx`` logs every request, with its full URL, at INFO. Nothing enables that today, but raising the log level must
    not be a way to put a webhook secret into the logs.
    """

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return self.secret not in record.getMessage()
        except Exception:  # a record that cannot be formatted cannot be inspected: keep it out as well
            return False


def _keep_secret_out_of_logs(secret: str) -> None:
    """The HTTP client's loggers stay at WARNING (they log every request URL at INFO and DEBUG), and a filter drops
    any record that still carries the secret path. The filter alone is not enough: the client re-encodes characters,
    so the URL it logs is not always the string that was configured.
    """
    for name in ("httpx", "httpcore"):
        client_logger = logging.getLogger(name)
        if (
            client_logger.level < logging.WARNING
        ):  # its own level, so raising the root level later cannot lower it again
            client_logger.setLevel(logging.WARNING)
    client_logger = logging.getLogger("httpx")
    if not any(isinstance(f, _KeepSecretOut) and f.secret == secret for f in client_logger.filters):
        client_logger.addFilter(_KeepSecretOut(secret))


async def system_resolver(host: str, port: int) -> Sequence[str]:
    """The addresses the operating system's resolver returns for ``host``, in order, without duplicates."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after", "").strip()
    if not raw.isdigit():
        return None
    return min(float(raw), _MAX_RETRY_AFTER_SECONDS)


class WebhookPoster:
    """POSTs a body to one webhook URL under the rules above. The URL is held, never exposed or logged."""

    def __init__(
        self,
        url: str,
        *,
        allowed_hosts: Sequence[str] = (),
        timeout: float = 10.0,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        # Raises UnsafeDestinationError for a URL or host that is not acceptable (also checked when the settings load).
        self._target: WebhookTarget = check_webhook_url(url, allowed_hosts=allowed_hosts)
        self._timeout = timeout
        if len(self._target.path_and_query) > 1:
            _keep_secret_out_of_logs(self._target.path_and_query)
        self._resolver = resolver
        self._transport = transport
        self._ssl_context = ssl_context  # None: the system trust store (tests pass a context that trusts their own CA)

    @property
    def destination(self) -> str:
        """The host name only: safe for logs and the audit trail (the path and query hold the secret)."""
        return self._target.host

    async def _public_addresses(self) -> list[str]:
        target = self._target
        try:
            resolved = await self._resolver(target.host, target.port)
        except (OSError, UnicodeError) as exc:
            raise DeliveryError(
                "DNS_FAILED", f"{target.host} did not resolve ({type(exc).__name__})", retryable=True
            ) from None
        if not resolved:
            raise DeliveryError("DNS_FAILED", f"{target.host} did not resolve", retryable=True)
        for raw in resolved:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                raise DeliveryError(
                    "DNS_FAILED", f"{target.host} resolved to an invalid address", retryable=True
                ) from None
            problem = classify_address(address)
            if problem is not None:
                raise DeliveryError(
                    "DESTINATION_NOT_PUBLIC", f"{target.host} resolves to a {problem} address", retryable=False
                )
        return list(resolved)

    async def post(self, body: bytes, headers: Mapping[str, str]) -> int:
        """POST ``body``; return the 2xx status. Raises :class:`DeliveryError` for everything else."""
        try:
            async with asyncio.timeout(self._timeout):
                addresses = await self._public_addresses()
                return await self._post_pinned(addresses, body, headers)
        except TimeoutError:
            raise DeliveryError("TIMEOUT", "the webhook did not answer in time", retryable=True) from None

    async def _post_pinned(self, addresses: Sequence[str], body: bytes, headers: Mapping[str, str]) -> int:
        target = self._target
        request_headers = {**headers, "Host": target.host_header}
        failure = DeliveryError("CONNECT_FAILED", f"could not connect to {target.host}", retryable=True)
        async with httpx.AsyncClient(
            transport=self._transport,
            verify=self._ssl_context if self._ssl_context is not None else True,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(self._timeout),
        ) as client:
            for address in addresses:
                host = f"[{address}]" if ":" in address else address
                url = f"https://{host}:{target.port}{target.path_and_query}"
                try:
                    async with client.stream(
                        "POST",
                        url,
                        content=body,
                        headers=request_headers,
                        extensions={"sni_hostname": target.host},  # certificate and SNI use the real name
                    ) as response:
                        return self._outcome(response)
                except httpx.ConnectError:
                    continue  # try the next validated address
                except httpx.TimeoutException:
                    raise DeliveryError("TIMEOUT", "the webhook did not answer in time", retryable=True) from None
                except httpx.InvalidURL:
                    raise DeliveryError("URL_INVALID", "the webhook URL cannot be used", retryable=False) from None
                except httpx.HTTPError as exc:
                    raise DeliveryError(
                        "NETWORK_ERROR", f"delivery failed ({type(exc).__name__})", retryable=True
                    ) from None
        raise failure

    @staticmethod
    def _outcome(response: httpx.Response) -> int:
        status = response.status_code
        if 200 <= status < 300:
            return status
        if 300 <= status < 400:
            raise DeliveryError(
                "REDIRECT_REFUSED",
                f"the webhook answered {status}; redirects are not followed",
                retryable=False,
                status_code=status,
            )
        retryable = status >= 500 or status in _RETRYABLE_STATUS
        raise DeliveryError(
            f"HTTP_{status}",
            f"the webhook answered {status}",
            retryable=retryable,
            status_code=status,
            retry_after=_retry_after(response) if retryable else None,
        )
