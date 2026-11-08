"""The TLS probe: proving the proxy is serving the right certificate.

``proxy.crunchtools.com`` terminates TLS for every site on the fleet. Before
these two keys the only probe that could be written against it was
``http://127.0.0.1:80/`` expecting a 301, which is answered by an Apache that
is redirecting to a vhost whose certificate expired last week, or that is
serving the default vhost's certificate to every name. The deploy would have
been called a success.

So the probe names the identity instead: ``server_name`` goes out as both the
SNI and the ``Host`` header, ``ca_file`` names the bundle the chain must verify
against, and ``ssl.create_default_context`` does the rest with
``check_hostname`` left alone. Chain verification is not a MITM defence on a
loopback socket -- it *is* the check.

Which is why none of these tests stub the transport. A fake answers whatever it
was told to answer; it cannot refuse a certificate, so against a fake the whole
feature is a no-op that passes. Each test here mints a real certificate with
``trustme``, serves it from a real TLS listener, and lets OpenSSL inside the
client under test decide.
"""

from __future__ import annotations

import ssl
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import trustme
from tlslistener import RESPONSE_BODY, TlsListener

from deployer.registry import Probe, Service
from deployer.runner import _proof_phrase
from deployer.verify import probe_once

SERVED_NAME = "crunchtools.com"
OTHER_NAME = "rootsofthevalley.org"


def authority(tmp_path: Path) -> tuple[trustme.CA, str]:
    """A CA and the ``ca_file`` path a registry row would name."""
    certificate_authority = trustme.CA()
    trust_file = tmp_path / "origin-ca.pem"
    certificate_authority.cert_pem.write_to_path(trust_file)
    return certificate_authority, str(trust_file)


def expired(certificate_authority: trustme.CA, name: str) -> trustme.LeafCert:
    """A certificate for ``name`` whose ``notAfter`` was yesterday."""
    now = datetime.now(tz=UTC)
    return certificate_authority.issue_cert(
        name, not_before=now - timedelta(days=2), not_after=now - timedelta(days=1)
    )


def shared_client_that_would_pass() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    """A stand-in for the fleet's one client that answers everything with a pass.

    Handed this, a probe that reached it would succeed without a handshake
    happening at all -- so the list it records being empty is the assertion.
    """
    reached: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        reached.append(request)
        return httpx.Response(200, text=f"{RESPONSE_BODY}\n")

    return httpx.AsyncClient(transport=httpx.MockTransport(answer)), reached


def pinned(listener: TlsListener, trust_file: str, name: str = SERVED_NAME) -> Probe:
    """The registry row this feature exists for, pointed at the test listener."""
    return Probe(
        kind="http",
        url=listener.url(),
        server_name=name,
        ca_file=trust_file,
        expect_body=RESPONSE_BODY,
    )


@pytest.mark.asyncio
async def test_the_matching_certificate_verifies_and_the_name_reaches_both_places(tmp_path: Path) -> None:
    """One ``server_name``, two places on the wire, and a real handshake.

    The SNI decides which certificate Apache offers and the ``Host`` header
    decides which vhost answers. A probe that sent only one of them would be
    reporting on a different request than the one it made.
    """
    certificate_authority, trust_file = authority(tmp_path)
    with TlsListener(certificate_authority.issue_cert(SERVED_NAME)) as listener:
        result = await probe_once(pinned(listener, trust_file))

    assert result.ok, result.detail
    assert listener.sni_names == [SERVED_NAME], "the name was not sent as the TLS SNI"
    assert listener.host_headers == [SERVED_NAME], "the name was not sent as the Host header"


@pytest.mark.asyncio
async def test_a_certificate_from_another_authority_is_refused(tmp_path: Path) -> None:
    """The right name, a chain that does not reach the bundle the row names.

    A proxy whose certificate was reissued by something else, or never
    replaced at all. The verdict asserted is the specific one -- no issuer --
    because a chain that cannot be built and a name that does not match are
    different outages and a test that accepted either would pass for the wrong
    reason.
    """
    _, trust_file = authority(tmp_path)
    stranger = trustme.CA()
    with TlsListener(stranger.issue_cert(SERVED_NAME)) as listener:
        result = await probe_once(pinned(listener, trust_file))

    assert not result.ok
    assert "unable to get local issuer certificate" in result.detail, result.detail
    # And the refusal reached the wire, rather than being inferred from the
    # client's side of it alone.
    assert listener.refusals, "the listener was never told the certificate was refused"


@pytest.mark.asyncio
async def test_a_certificate_for_another_host_is_refused(tmp_path: Path) -> None:
    """A trusted chain is not enough. This is the default-vhost failure.

    Apache answering every name with one site's certificate is the exact
    outage these keys were added for, and it verifies as a chain -- only the
    hostname check catches it.
    """
    certificate_authority, trust_file = authority(tmp_path)
    with TlsListener(certificate_authority.issue_cert(OTHER_NAME)) as listener:
        result = await probe_once(pinned(listener, trust_file))

    assert not result.ok
    assert "Hostname mismatch" in result.detail, result.detail


@pytest.mark.asyncio
async def test_an_expired_certificate_is_refused(tmp_path: Path) -> None:
    """Right name, right chain, past its notAfter.

    Nothing in this deployer reads a date: ``check_hostname`` and the default
    context mean OpenSSL does it, which is the point of not hand-rolling any
    of this.
    """
    certificate_authority, trust_file = authority(tmp_path)
    with TlsListener(expired(certificate_authority, SERVED_NAME)) as listener:
        result = await probe_once(pinned(listener, trust_file))

    assert not result.ok
    assert "certificate has expired" in result.detail, result.detail


@pytest.mark.asyncio
async def test_two_names_on_one_shared_client_are_not_both_verified(tmp_path: Path) -> None:
    """Why a pinned row gets its own client, measured rather than assumed.

    httpcore 1.0.9 keys a pooled connection by origin alone. Handed one client,
    two rows probing ``https://127.0.0.1:443/`` under different names reuse the
    same handshake, and the second row is "verified" against the first row's
    name -- a false pass on the exact check being bought here. Measured against
    httpx 0.28.1: the second request came back 200 with its own name never
    offered.

    So the shared client is refused for a row that pins TLS, and the assertion
    is that the second name reached the server in a handshake of its own.
    """
    certificate_authority, trust_file = authority(tmp_path)
    with TlsListener(certificate_authority.issue_cert(SERVED_NAME)) as listener:
        first = pinned(listener, trust_file)
        second = replace(first, server_name=OTHER_NAME)
        trusting = httpx.AsyncClient(verify=ssl.create_default_context(cafile=trust_file))
        async with trusting:
            assert (await probe_once(first, trusting)).ok
            assert not (await probe_once(second, trusting)).ok

    assert listener.sni_names == [SERVED_NAME, OTHER_NAME], (
        f"the second probe reused the first handshake: {listener.sni_names}"
    )


@pytest.mark.asyncio
async def test_a_pinned_row_never_touches_the_shared_client(tmp_path: Path) -> None:
    """``verify`` is per-client, so the row cannot be run on the fleet's client.

    The shared client here would answer everything with a pass. If the probe
    reached it, the probe would succeed without a handshake happening at all,
    which is what this asserts against.
    """
    certificate_authority, trust_file = authority(tmp_path)
    shared, reached = shared_client_that_would_pass()
    with TlsListener(certificate_authority.issue_cert(SERVED_NAME)) as listener:
        async with shared:
            result = await probe_once(pinned(listener, trust_file), shared)

    assert result.ok, result.detail
    assert not reached, "a pinned row was run on the shared client"
    assert listener.sni_names == [SERVED_NAME]


@pytest.mark.asyncio
async def test_a_server_name_without_a_ca_file_also_gets_its_own_client(tmp_path: Path) -> None:
    """``server_name`` alone is enough to need one: it is the pooling hazard.

    Such a row verifies against the system trust store, so this listener's
    private CA is correctly refused. What matters is *where* the refusal comes
    from -- a real handshake, not the shared client's canned pass.
    """
    certificate_authority, _ = authority(tmp_path)
    shared, reached = shared_client_that_would_pass()
    with TlsListener(certificate_authority.issue_cert(SERVED_NAME)) as listener:
        probe = Probe(kind="http", url=listener.url(), server_name=SERVED_NAME)
        async with shared:
            result = await probe_once(probe, shared)

    assert not result.ok
    assert not reached, "a row carrying server_name was run on the shared client"
    assert listener.sni_names == [SERVED_NAME]


@pytest.mark.asyncio
async def test_a_ca_file_that_is_not_a_bundle_fails_the_probe_rather_than_raising(tmp_path: Path) -> None:
    """The narrow case the registry cannot catch: the bundle changed afterwards.

    An unloadable bundle in the registry is exit 2 at load -- see
    ``test_a_ca_file_that_is_not_a_bundle_is_refused_at_load``. One that is
    replaced under a running deployer is still this program's fault rather
    than the service's, and it has to arrive as a failed probe with the path
    in it, not as a traceback out of the middle of a maintenance window.
    """
    junk = tmp_path / "not-a-bundle.pem"
    junk.write_text("this is not a certificate\n", encoding="utf-8")
    probe = Probe(kind="http", url="https://127.0.0.1:443/", server_name=SERVED_NAME, ca_file=str(junk))

    result = await probe_once(probe)

    assert not result.ok
    assert str(junk) in result.detail, result.detail


def test_the_ticket_says_which_name_the_certificate_verified_as() -> None:
    """The comment is the only receipt a deploy leaves.

    "``https://127.0.0.1:443/`` answered throughout" is true of a proxy serving
    the wrong certificate to that port as well, so on a row that pinned a name
    the receipt has to say which name was proved.
    """
    service = Service(
        name="proxy.crunchtools.com",
        repo="crunchtools/proxy",
        image="quay.io/crunchtools/proxy",
        unit="proxy.crunchtools.com.service",
        probe=Probe(kind="http", url="https://127.0.0.1:443/", server_name=SERVED_NAME),
        nagios_host="ctr-proxy.crunchtools.com",
        requestors=("scott",),
    )
    phrase = _proof_phrase(service)
    assert "https://127.0.0.1:443/ answered throughout" in phrase
    assert f"verified as {SERVED_NAME}" in phrase
