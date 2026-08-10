import ipaddress
import socket

import httpx

from arxiv_updater import journal_network
from arxiv_updater.journal_network import (
    FAKE_IP_NETWORK,
    JournalNetwork,
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
    monkeypatch.setattr(journal_network, "system_dns_uses_fake_ip", lambda: True)

    rebuilt = get_journal_network()

    assert rebuilt is not existing
    assert rebuilt.direct
    assert rebuilt.local_address == "192.0.2.10"
