"""The pinned connection against a real TLS server: connect to the validated address, verify the real host name.

The unit tests of the SSRF guard use ``httpx.MockTransport``, which never makes a TLS handshake. This test does, over
loopback with a certificate it creates, to prove the two properties that matter for DNS-rebinding protection:

* the request really goes to the address that was validated (not to a second resolution of the name), and
* the server certificate is checked against the **real host name**, although the URL that is connected to holds an IP,
  so a server that answers on the validated address with a certificate for another name is refused.
"""

from __future__ import annotations

import asyncio
import datetime
import ssl
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from orqentis.notifications import ssrf
from orqentis.notifications.port import DeliveryError
from orqentis.notifications.ssrf import WebhookPoster

pytestmark = pytest.mark.security

HOST = "hooks.example.test"


def make_certificate(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        # A self-signed certificate that is its own trust anchor must satisfy the strict X.509 checks that Python 3.13
        # turns on by default (a CA with key usage, subject and authority key identifiers).
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(pem)
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path, pem.decode()


class Recorder:
    def __init__(self) -> None:
        self.requests: list[bytes] = []
        self.server_names: list[str | None] = []


async def serve(cert: Path, key: Path, recorder: Recorder) -> tuple[asyncio.Server, int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def sni(sock: ssl.SSLObject, name: str | None, ctx: ssl.SSLContext) -> None:
        recorder.server_names.append(name)

    context.sni_callback = sni

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.readuntil(b"\r\n\r\n")
        length = next(
            (int(line.split(b":")[1]) for line in data.split(b"\r\n") if line.lower().startswith(b"content-length")), 0
        )
        body = await reader.readexactly(length) if length else b""
        recorder.requests.append(data + body)
        writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, server.sockets[0].getsockname()[1]


@pytest.fixture
def allow_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only this test treats loopback as reachable, so the TLS behaviour can be observed without a public server."""
    monkeypatch.setattr(ssrf, "classify_address", lambda address: None)


@pytest.fixture
async def server_with_matching_certificate(tmp_path: Path) -> AsyncIterator[tuple[int, str, Recorder]]:
    cert, key, pem = make_certificate(tmp_path, HOST)
    recorder = Recorder()
    server, port = await serve(cert, key, recorder)
    try:
        yield port, pem, recorder
    finally:
        server.close()
        await server.wait_closed()


def trusting(pem: str) -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=pem)
    return context


def resolver_to(address: str) -> ssrf.Resolver:
    async def resolve(host: str, port: int) -> Sequence[str]:
        return [address]

    return resolve


async def test_the_request_reaches_the_validated_address_with_the_real_name_in_sni_and_host(
    server_with_matching_certificate: tuple[int, str, Recorder], allow_loopback: None
) -> None:
    port, pem, recorder = server_with_matching_certificate
    poster = WebhookPoster(
        f"https://{HOST}:{port}/workflows/abc?sig=SECRETSIG",
        resolver=resolver_to("127.0.0.1"),
        ssl_context=trusting(pem),
        timeout=10,
    )
    assert await poster.post(b'{"hello":"world"}', {"Content-Type": "application/json"}) == 202
    assert recorder.server_names == [HOST]  # TLS server name indication is the real host, not the IP
    request = recorder.requests[0].decode()
    assert request.startswith("POST /workflows/abc?sig=SECRETSIG HTTP/1.1")
    assert f"host: {HOST}:{port}".lower() in request.lower() and request.endswith('{"hello":"world"}')


async def test_a_certificate_for_another_name_is_refused_even_on_the_validated_address(
    tmp_path: Path, allow_loopback: None
) -> None:
    """What an attacker who controls the validated address (or a man in the middle) would present."""
    cert, key, pem = make_certificate(tmp_path, "evil.example")
    recorder = Recorder()
    server, port = await serve(cert, key, recorder)
    try:
        poster = WebhookPoster(
            f"https://{HOST}:{port}/workflows/abc?sig=SECRETSIG",
            resolver=resolver_to("127.0.0.1"),
            ssl_context=trusting(pem),  # the certificate chain is trusted; its NAME is wrong
            timeout=10,
        )
        with pytest.raises(DeliveryError) as caught:
            await poster.post(b"{}", {})
    finally:
        server.close()
        await server.wait_closed()
    assert caught.value.code == "CONNECT_FAILED" and caught.value.retryable
    assert recorder.requests == []  # the body was never sent over the unverified connection


async def test_an_untrusted_certificate_is_refused(
    server_with_matching_certificate: tuple[int, str, Recorder], allow_loopback: None
) -> None:
    port, _, recorder = server_with_matching_certificate
    poster = WebhookPoster(
        f"https://{HOST}:{port}/x", resolver=resolver_to("127.0.0.1"), timeout=10
    )  # default trust store: the test's own CA is not in it
    with pytest.raises(DeliveryError):
        await poster.post(b"{}", {})
    assert recorder.requests == []


async def test_without_the_override_the_loopback_server_is_never_contacted(
    server_with_matching_certificate: tuple[int, str, Recorder],
) -> None:
    port, pem, recorder = server_with_matching_certificate
    poster = WebhookPoster(
        f"https://{HOST}:{port}/x", resolver=resolver_to("127.0.0.1"), ssl_context=trusting(pem), timeout=10
    )
    with pytest.raises(DeliveryError) as caught:
        await poster.post(b"{}", {})
    assert caught.value.code == "DESTINATION_NOT_PUBLIC"
    assert recorder.requests == [] and recorder.server_names == []
