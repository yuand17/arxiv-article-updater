import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from arxiv_updater.arxiv_network import ArxivCooldownError, ArxivRequestGate
from arxiv_updater.services import abstracts

URL = "https://export.arxiv.org/api/query"
NOW = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds

    def utcnow(self):
        return NOW + timedelta(seconds=self.value)


@pytest.fixture
def paced_gate():
    clock = Clock()
    gate = ArxivRequestGate(
        clock=clock.monotonic, sleep=clock.sleep, utcnow=clock.utcnow,
    )
    return gate, clock


def test_quiet_interval_starts_after_response_finishes(paced_gate):
    gate, clock = paced_gate
    starts = []

    def handle(request):
        starts.append(clock.value)
        clock.value += 10
        return httpx.Response(200, text="ok")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gate.get(client, URL)
        gate.get(client, URL)

    assert starts == [0, 13]
    assert clock.sleeps == [3]


def test_transport_failures_also_leave_quiet_interval(paced_gate):
    gate, clock = paced_gate
    starts = []

    def handle(request):
        starts.append(clock.value)
        clock.value += 5
        raise httpx.ReadTimeout("read timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for _ in range(2):
            with pytest.raises(httpx.ReadTimeout):
                gate.get(client, URL)

    assert starts == [0, 8]
    assert clock.sleeps == [3]


@pytest.mark.parametrize(
    ("retry_after", "seconds"),
    [
        (None, 1800),
        ("invalid", 1800),
        ("NaN", 1800),
        ("inf", 1800),
        ("7200", 7200),
        (format_datetime(NOW + timedelta(hours=3), usegmt=True), 10800),
    ],
)
def test_long_cooldowns_preserve_server_delay_and_fail_fast(paced_gate, retry_after, seconds):
    gate, clock = paced_gate
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": retry_after} if retry_after else {})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert gate.get(client, URL).status_code == 429
        assert gate.remaining_cooldown_seconds() == seconds
        clock.value += 10
        with pytest.raises(ArxivCooldownError, match="429") as exc_info:
            gate.get(client, URL)

    assert exc_info.value.response.status_code == 429
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == seconds - 10
    assert gate.remaining_cooldown_seconds() == seconds - 10
    assert len(requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("status_code", [408, 500, 503, 599])
def test_retryable_server_errors_preserve_explicit_long_cooldown(paced_gate, status_code):
    gate, clock = paced_gate
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status_code, headers={"Retry-After": "7200"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert gate.get(client, URL).status_code == status_code
        assert gate.remaining_cooldown_seconds() == 7200
        clock.value += 10
        with pytest.raises(ArxivCooldownError, match=f"HTTP {status_code}") as exc_info:
            gate.get(client, URL)

    assert exc_info.value.status_code == status_code
    assert exc_info.value.response.status_code == status_code
    assert exc_info.value.retry_after_seconds == 7190
    assert gate.remaining_cooldown_seconds() == 7190
    assert len(requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("retry_after", [None, "invalid", "inf"])
def test_server_errors_without_valid_retry_after_can_retry_normally(paced_gate, retry_after):
    gate, clock = paced_gate
    starts = []

    def handle(request):
        starts.append(clock.value)
        return httpx.Response(503, headers={"Retry-After": retry_after} if retry_after else {})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert gate.get(client, URL).status_code == 503
        assert gate.remaining_cooldown_seconds() == 0
        assert gate.get(client, URL).status_code == 503

    assert starts == [0, 3]
    assert clock.sleeps == [3]
    assert gate.remaining_cooldown_seconds() == 0


@pytest.mark.parametrize("status_code", [200, 404])
def test_other_responses_do_not_trigger_server_cooldown(paced_gate, status_code):
    gate, clock = paced_gate
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status_code, headers={"Retry-After": "7200"})
        )
    ) as client:
        assert gate.get(client, URL).status_code == status_code
        assert gate.get(client, URL).status_code == status_code
    assert gate.remaining_cooldown_seconds() == 0
    assert clock.sleeps == [3]


def test_short_cooldown_waits_until_the_full_deadline(paced_gate):
    gate, clock = paced_gate
    starts = []

    def handle(request):
        starts.append(clock.value)
        return httpx.Response(429 if len(starts) == 1 else 200, headers={"Retry-After": "12"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gate.get(client, URL)
        gate.get(client, URL)

    assert starts == [0, 12]
    assert clock.sleeps == [12]
    assert gate.remaining_cooldown_seconds() == 0


def test_expired_long_cooldown_allows_request(paced_gate):
    gate, clock = paced_gate
    starts = []

    def handle(request):
        starts.append(clock.value)
        return httpx.Response(429 if len(starts) == 1 else 200)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gate.get(client, URL)
        clock.value = 1800
        assert gate.get(client, URL).status_code == 200

    assert starts == [0, 1800]
    assert clock.sleeps == []
    assert gate.remaining_cooldown_seconds() == 0


def test_client_status_hook_still_records_rate_limit(paced_gate):
    gate, clock = paced_gate
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(429)),
        event_hooks={"response": [lambda response: response.raise_for_status()]},
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            gate.get(client, URL)
        assert gate.remaining_cooldown_seconds() == 1800
        with pytest.raises(ArxivCooldownError):
            gate.get(client, URL)
    assert clock.sleeps == []


def test_requests_from_different_clients_cannot_overlap(paced_gate):
    gate, clock = paced_gate
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    second_entered = threading.Event()
    starts = []

    def first_handle(request):
        starts.append(clock.value)
        entered.set()
        assert release.wait(5)
        clock.value += 7
        return httpx.Response(200)

    def second_handle(request):
        starts.append(clock.value)
        second_entered.set()
        return httpx.Response(200)

    with (
        httpx.Client(transport=httpx.MockTransport(first_handle)) as first_client,
        httpx.Client(transport=httpx.MockTransport(second_handle)) as second_client,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(gate.get, first_client, URL)
        try:
            assert entered.wait(5)

            def second_request():
                second_started.set()
                return gate.get(second_client, URL)

            second = executor.submit(second_request)
            assert second_started.wait(5)
            assert not second_entered.wait(0.05)
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200

    assert starts == [0, 10]
    assert clock.sleeps == [3]


def test_abstract_enrichment_uses_shared_api_cooldown(paced_gate, monkeypatch):
    gate, clock = paced_gate
    monkeypatch.setattr(abstracts, "get_arxiv_request_gate", lambda: gate)
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            abstracts._arxiv_abstract("2609.12345", client)
        with pytest.raises(ArxivCooldownError):
            abstracts._arxiv_abstract("2609.54321", client)

    assert len(requests) == 1
    assert requests[0].url.params["id_list"] == "2609.12345"
    assert clock.sleeps == []
