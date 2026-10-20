"""Rules for a webhook destination that need no network: the URL, the host allow list and an address.

The checks that need DNS (every address the name resolves to must be public) live in ``orqentis.notifications.ssrf``;
this module is the pure part, shared with the settings validation so that a bad URL fails at startup, with a message
that never contains the URL (it is a secret: the Teams webhook carries its signature in the query string).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_HTTPS_PORT = 443

# Reachable from any virtual machine yet not private addresses: the Azure host/WireServer endpoint and the metadata
# services of the large clouds. The link-local ones are refused anyway; they are listed so the intent is explicit.
_METADATA_ADDRESSES = frozenset(
    ipaddress.ip_address(a)
    for a in ("168.63.129.16", "169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192")
)
# IPv6 ranges that embed an IPv4 address (so could smuggle a private one past the checks) and that nobody needs here.
_EMBEDDING_V6 = tuple(
    ipaddress.ip_network(n)
    for n in ("::ffff:0:0/96", "::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")
)


# Deprecated or special-purpose ranges that are not (or no longer) public unicast addresses. ``ipaddress`` does not call
# every one of them private on every Python version, so they are listed: IPv6 site-local (fec0::/10), the 6to4 relay
# anycast prefix (192.88.99.0/24), the discard-only prefix, documentation prefixes and the SRv6 SID prefix.
_NON_PUBLIC_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("fec0::/10", "192.88.99.0/24", "100::/64", "2001:db8::/32", "3fff::/20", "5f00::/16", "2001:10::/28")
)


class UnsafeDestinationError(Exception):
    """The destination is not acceptable. ``code`` is stable; ``reason`` never contains the URL."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class WebhookTarget:
    host: str  # ASCII, lower case, no trailing dot
    port: int
    path_and_query: str

    @property
    def host_header(self) -> str:
        return self.host if self.port == DEFAULT_HTTPS_PORT else f"{self.host}:{self.port}"


def normalise_host(host: str) -> str:
    return host.rstrip(".").lower()


def host_matches(host: str, patterns: Sequence[str]) -> bool:
    """Whether ``host`` is an exact pattern or below a ``*.suffix`` / ``.suffix`` pattern (at a label boundary)."""
    name = normalise_host(host)
    for raw in patterns:
        pattern = normalise_host(raw.strip())
        if not pattern:
            continue
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
        elif pattern.startswith("."):
            suffix = pattern
        else:
            if name == pattern:
                return True
            continue
        if name.endswith(suffix) and len(name) > len(suffix):
            return True
    return False


def classify_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Why ``address`` may not be called, or ``None`` when it is a public unicast address."""
    if address in _METADATA_ADDRESSES:
        return "cloud metadata"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_unspecified:
        return "unspecified"
    if address.is_multicast:
        return "multicast"
    if isinstance(address, ipaddress.IPv6Address) and any(address in net for net in _EMBEDDING_V6):
        return "IPv4-embedding IPv6"
    if any(address in net for net in _NON_PUBLIC_NETWORKS if net.version == address.version):
        return "deprecated or special-purpose range"
    if address.is_private:
        return "private"
    if address.is_reserved or not address.is_global:
        return "non-public"
    return None


def check_webhook_url(url: str, *, allowed_hosts: Sequence[str] = ()) -> WebhookTarget:
    """Validate a webhook URL without touching the network.

    https only, a DNS name (not an IP literal), no credentials in the URL, and, when ``allowed_hosts`` is not empty,
    a host that matches one of the patterns.
    """
    if not url or any(ch in url for ch in "\\ \t\r\n") or not url.isascii():
        raise UnsafeDestinationError("URL_INVALID", "the webhook URL is empty or contains forbidden characters")
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise UnsafeDestinationError("URL_INVALID", "the webhook URL cannot be parsed") from None
    if parts.scheme != "https":
        raise UnsafeDestinationError("SCHEME_NOT_HTTPS", "only https webhook URLs are accepted")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeDestinationError("URL_HAS_CREDENTIALS", "a webhook URL must not contain credentials")
    if not hostname:
        raise UnsafeDestinationError("URL_INVALID", "the webhook URL has no host")
    host = normalise_host(hostname)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise UnsafeDestinationError("HOST_IS_IP_LITERAL", "the webhook host must be a DNS name, not an IP address")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeDestinationError("HOST_NOT_ALLOWED", "the webhook host is a local name")
    if allowed_hosts and not host_matches(host, allowed_hosts):
        raise UnsafeDestinationError("HOST_NOT_ALLOWED", f"the webhook host {host!r} is not in the allowed hosts")
    path_and_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return WebhookTarget(host=host, port=port or DEFAULT_HTTPS_PORT, path_and_query=path_and_query)
