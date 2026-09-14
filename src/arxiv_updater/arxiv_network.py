"""Process-wide pacing for all arXiv API consumers."""

import math
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_RATE_LIMIT_COOLDOWN_SECONDS = 30.0 * 60.0
ARXIV_MAX_INLINE_WAIT_SECONDS = 30.0


def retry_after_seconds(
    response: httpx.Response,
    *,
    now: datetime,
    default: float = ARXIV_RATE_LIMIT_COOLDOWN_SECONDS,
) -> float:
    """Preserve server cooldowns and fall back only when the header is unusable."""

    value = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            seconds = (retry_at - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    if not math.isfinite(seconds):
        return default
    return max(seconds, 0.0)


class ArxivCooldownError(httpx.HTTPStatusError):
    """A previous response forbids requests; defer without holding a worker asleep."""

    def __init__(self, url: str, retry_after: float, *, status_code: int = 429) -> None:
        self.retry_after_seconds = retry_after
        self.status_code = status_code
        request = httpx.Request("GET", url)
        response = httpx.Response(
            status_code,
            request=request,
            headers={"Retry-After": str(math.ceil(retry_after))},
        )
        super().__init__(
            f"arXiv HTTP {status_code} cooldown active; "
            f"retry after {math.ceil(retry_after)} seconds",
            request=request,
            response=response,
        )


class ArxivRequestGate:
    """Serialize API requests and leave a quiet interval after each completes."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._utcnow = utcnow
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._cooldown_until = 0.0
        self._cooldown_status_code = 429

    def get(self, client: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        """Perform one GET; callers retain responsibility for HTTP and payload retries."""

        with self._lock:
            now = self._clock()
            cooldown = self._cooldown_until - now
            if cooldown > ARXIV_MAX_INLINE_WAIT_SECONDS:
                raise ArxivCooldownError(url, cooldown, status_code=self._cooldown_status_code)
            delay = max(self._next_request_at, self._cooldown_until) - now
            if delay > 0:
                self._sleep(delay)
            try:
                try:
                    response = client.get(url, **kwargs)
                except httpx.HTTPStatusError as exc:
                    self._remember_cooldown(exc.response)
                    raise
                self._remember_cooldown(response)
                return response
            finally:
                self._next_request_at = self._clock() + ARXIV_REQUEST_INTERVAL_SECONDS

    def _remember_cooldown(self, response: httpx.Response) -> None:
        rate_limited = response.status_code == 429
        retryable = response.status_code == 408 or 500 <= response.status_code < 600
        if not rate_limited and not (retryable and response.headers.get("Retry-After")):
            return
        delay = retry_after_seconds(
            response,
            now=self._utcnow(),
            default=ARXIV_RATE_LIMIT_COOLDOWN_SECONDS if rate_limited else 0.0,
        )
        deadline = self._clock() + delay
        if deadline > self._cooldown_until:
            self._cooldown_until = deadline
            self._cooldown_status_code = response.status_code

    def remaining_cooldown_seconds(self) -> float:
        with self._lock:
            return max(self._cooldown_until - self._clock(), 0.0)


_arxiv_request_gate = ArxivRequestGate()


def get_arxiv_request_gate() -> ArxivRequestGate:
    return _arxiv_request_gate
