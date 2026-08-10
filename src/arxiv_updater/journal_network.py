"""Network helpers for journal sources that must bypass a local VPN Fake-IP DNS."""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpcore
import httpx

FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
DOH_HOST = "doh.pub"
DOH_BOOTSTRAP_ADDRESSES = ("1.12.12.12",)
DNS_CACHE_SECONDS = 300

AddressResolver = Callable[[str], list[str]]
GetAddrInfo = Callable[..., list[tuple[Any, ...]]]


def _ipv4_addresses(records: list[tuple[Any, ...]]) -> list[str]:
    addresses: list[str] = []
    for record in records:
        try:
            address = str(record[4][0])
            parsed = ipaddress.ip_address(address)
        except (IndexError, TypeError, ValueError):
            continue
        if isinstance(parsed, ipaddress.IPv4Address) and address not in addresses:
            addresses.append(address)
    return addresses


def system_dns_uses_fake_ip(
    hostname: str = "www.nature.com",
    *,
    resolver: GetAddrInfo = socket.getaddrinfo,
) -> bool:
    """Return whether system DNS is being intercepted by a Fake-IP VPN."""

    try:
        records = resolver(hostname, 443, type=socket.SOCK_STREAM)
    except OSError:
        return True
    addresses = _ipv4_addresses(records)
    return not addresses or any(
        ipaddress.ip_address(value) in FAKE_IP_NETWORK for value in addresses
    )


def _default_route_local_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 443))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def find_direct_local_address(
    *,
    resolver: GetAddrInfo = socket.getaddrinfo,
) -> str:
    """Pick a physical IPv4 address instead of the active tunnel address."""

    try:
        records = resolver(
            socket.gethostname(),
            0,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return ""
    routed_address = _default_route_local_address()
    candidates: list[tuple[int, str]] = []
    for value in _ipv4_addresses(records):
        address = ipaddress.ip_address(value)
        if (
            value == routed_address
            or address.is_loopback
            or address.is_link_local
            or address.is_unspecified
            or address.is_multicast
            or address in FAKE_IP_NETWORK
        ):
            continue
        candidates.append((0 if address.is_global else 1, value))
    candidates.sort()
    return candidates[0][1] if candidates else ""


class _DirectBackend(httpcore.SyncBackend):
    def __init__(self, resolver: AddressResolver, local_address: str) -> None:
        self._resolver = resolver
        self._local_address = local_address

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        try:
            ipaddress.ip_address(host)
            addresses = [host]
        except ValueError:
            try:
                addresses = self._resolver(host)
            except OSError as exc:
                raise httpcore.ConnectError(str(exc)) from exc

        last_error: Exception | None = None
        for address in addresses:
            try:
                return super().connect_tcp(
                    address,
                    port,
                    timeout,
                    local_address or self._local_address,
                    socket_options,
                )
            except (httpcore.ConnectTimeout, httpcore.ConnectError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError(f"No public IPv4 address for {host}")


def _direct_transport(resolver: AddressResolver, local_address: str) -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(trust_env=False, local_address=local_address)
    pool = transport._pool
    pool._network_backend = _DirectBackend(resolver, local_address)
    return transport


class DnsOverHttpsResolver:
    def __init__(self, local_address: str, *, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            transport=_direct_transport(
                lambda host: list(DOH_BOOTSTRAP_ADDRESSES) if host == DOH_HOST else [],
                local_address,
            ),
            timeout=10,
            follow_redirects=False,
        )
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def resolve(self, hostname: str) -> list[str]:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None:
            return [str(address)]

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(hostname)
            if cached and cached[0] > now:
                return list(cached[1])
        try:
            response = self._client.get(
                f"https://{DOH_HOST}/resolve",
                params={"name": hostname, "type": "A"},
                headers={"Accept": "application/dns-json"},
            )
            response.raise_for_status()
            answers = response.json().get("Answer", [])
            addresses = []
            for answer in answers:
                if answer.get("type") != 1:
                    continue
                parsed = ipaddress.ip_address(str(answer.get("data") or ""))
                if isinstance(parsed, ipaddress.IPv4Address) and parsed.is_global:
                    addresses.append(str(parsed))
        except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
            raise socket.gaierror(f"Direct DNS lookup failed for {hostname}") from exc
        if not addresses:
            raise socket.gaierror(f"Direct DNS returned no public IPv4 address for {hostname}")
        unique = list(dict.fromkeys(addresses))
        with self._lock:
            self._cache[hostname] = (now + DNS_CACHE_SECONDS, unique)
        return list(unique)

    def getaddrinfo(
        self,
        hostname: str,
        port: int,
        *_args: Any,
        **_kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))
            for address in self.resolve(hostname)
        ]


@dataclass(slots=True)
class JournalNetwork:
    client: httpx.Client
    resolver: GetAddrInfo
    direct: bool = False
    local_address: str = ""


_journal_network: JournalNetwork | None = None
_journal_network_lock = threading.Lock()


def get_journal_network() -> JournalNetwork:
    """Return one thread-safe journal client, with a direct path when Lantern intercepts DNS."""

    global _journal_network
    with _journal_network_lock:
        local_address = find_direct_local_address()
        direct = bool(local_address and system_dns_uses_fake_ip())
        expected_local_address = local_address if direct else ""
        if (
            _journal_network is not None
            and _journal_network.direct == direct
            and _journal_network.local_address == expected_local_address
        ):
            return _journal_network
        if direct:
            resolver = DnsOverHttpsResolver(local_address)
            client = httpx.Client(
                transport=_direct_transport(resolver.resolve, local_address),
                timeout=20,
                follow_redirects=True,
            )
            _journal_network = JournalNetwork(
                client,
                resolver.getaddrinfo,
                direct=True,
                local_address=local_address,
            )
        else:
            _journal_network = JournalNetwork(
                httpx.Client(timeout=20, follow_redirects=True, trust_env=False),
                socket.getaddrinfo,
            )
        return _journal_network
