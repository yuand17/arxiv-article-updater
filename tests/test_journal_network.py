import ipaddress
import socket

import httpx
import pytest

from arxiv_updater import journal_network
from arxiv_updater.journal_network import (
    FAKE_IP_NETWORK,
    JournalNetwork,
    JournalNetworkError,
    get_journal_network,
    system_dns_uses_fake_ip,
)


def test_lantern_fake_ip_dns_is_detected():
    def fake_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.2.91", 443))]

    assert system_dns_uses_fake_ip(resolver=fake_dns)
    assert ipaddress.ip_address("198.18.2.91") in FAKE_IP_NETWORK


def test_public_system_dns_does_not_trigger_direct_mode():
    def public_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]

    assert not system_dns_uses_fake_ip(resolver=public_dns)


def test_cached_normal_client_is_rebuilt_when_lantern_becomes_active(monkeypatch):
    existing = JournalNetwork(
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        socket.getaddrinfo,
    )
    monkeypatch.setattr(journal_network, "_journal_network", existing)
    monkeypatch.setattr(journal_network, "find_direct_local_address", lambda: "192.0.2.10")
    monkeypatch.setattr(journal_network, "system_dns_uses_fake_ip", lambda _hostname: True)

    rebuilt = get_journal_network()

    assert rebuilt is not existing
    assert rebuilt.direct
    assert rebuilt.local_address == "192.0.2.10"


def test_network_mode_is_probed_with_the_actual_primary_feed_hostname(monkeypatch):
    existing = JournalNetwork(
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))),
        socket.getaddrinfo,
    )
    observed: list[str] = []
    monkeypatch.setattr(journal_network, "_journal_network", existing)
    monkeypatch.setattr(journal_network, "find_direct_local_address", lambda: "")
    monkeypatch.setattr(
        journal_network,
        "system_dns_uses_fake_ip",
        lambda hostname: observed.append(hostname) or False,
    )

    selected = get_journal_network("www.science.org")

    assert selected is existing
    assert observed == ["www.science.org"]


def test_fake_ip_without_a_physical_interface_fails_clearly(monkeypatch):
    monkeypatch.setattr(journal_network, "_journal_network", None)
    monkeypatch.setattr(journal_network, "find_direct_local_address", lambda: "")
    monkeypatch.setattr(journal_network, "system_dns_uses_fake_ip", lambda _hostname: True)

    with pytest.raises(JournalNetworkError, match="www.science.org.*no physical"):
        get_journal_network("www.science.org")
