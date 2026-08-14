import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from dateutil.parser import isoparse

from ..config import Settings, get_settings
from .base import PaperCandidate, SourceAdapter
from .cache import DailyResponseCache

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID_PATTERN = re.compile(r"(?:abs/)?([^/]+?)(?:v\d+)?$")
ARXIV_CACHE_MAX_AGE = timedelta(minutes=5)
ARXIV_REQUEST_ATTEMPTS = 3
ARXIV_REQUEST_INTERVAL_SECONDS = 3.0
ARXIV_RETRY_BASE_SECONDS = 3.0
ARXIV_MAX_RETRY_AFTER_SECONDS = 30.0
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"


def normalize_arxiv_id(value: str) -> str:
    match = ARXIV_ID_PATTERN.search(value.strip())
    return match.group(1) if match else value.strip()


def _retry_after_seconds(response: httpx.Response, default: float) -> float:
    value = response.headers.get("Retry-After", "").strip()
    seconds = default
    if value:
        try:
            seconds = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                seconds = (retry_at - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                seconds = default
    return min(max(seconds, 0.0), ARXIV_MAX_RETRY_AFTER_SECONDS)


def _retryable_status(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def _text(element: ET.Element, name: str) -> str:
    node = element.find(name)
    return " ".join((node.text or "").split()) if node is not None else ""


def parse_arxiv_feed(content: str, since: datetime | None = None) -> list[PaperCandidate]:
    root = ET.fromstring(content)
    candidates: list[PaperCandidate] = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_id = _text(entry, f"{ATOM}id")
        arxiv_id = normalize_arxiv_id(entry_id)
        published = isoparse(_text(entry, f"{ATOM}published"))
        updated_text = _text(entry, f"{ATOM}updated")
        updated = isoparse(updated_text) if updated_text else published
        if since and max(published, updated) < since:
            continue
        authors = [_text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")]
        links = {
            link.attrib.get("rel"): link.attrib.get("href") for link in entry.findall(f"{ATOM}link")
        }
        pdf_link = next(
            (
                link.attrib.get("href")
                for link in entry.findall(f"{ATOM}link")
                if link.attrib.get("type") == "application/pdf"
            ),
            None,
        )
        doi = _text(entry, f"{ARXIV}doi") or None
        categories = [node.attrib.get("term", "") for node in entry.findall(f"{ATOM}category")]
        candidates.append(
            PaperCandidate(
                source="arxiv",
                external_id=arxiv_id,
                title=_text(entry, f"{ATOM}title"),
                authors=[author for author in authors if author],
                abstract=_text(entry, f"{ATOM}summary"),
                published_at=published,
                updated_at=updated,
                arxiv_id=arxiv_id,
                doi=doi.lower() if doi else None,
                categories=[category for category in categories if category],
                canonical_url=links.get("alternate") or f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=pdf_link or f"https://arxiv.org/pdf/{arxiv_id}",
            )
        )
    return candidates


class ArxivAdapter(SourceAdapter):
    name = "arxiv"

    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
        max_results: int | None = None,
        page_size: int = 100,
        max_pages: int = 100,
        cache: DailyResponseCache | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.max_results = max_results
        self.page_size = min(page_size, max_results) if max_results else page_size
        self.max_pages = max_pages
        self.cache = cache or DailyResponseCache("arxiv")
        self._last_network_request: float | None = None
        self._next_network_request_at = 0.0

    def _wait_for_request_slot(self) -> None:
        delay = self._next_network_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        request_started_at = time.monotonic()
        self._last_network_request = request_started_at
        self._next_network_request_at = (
            request_started_at + ARXIV_REQUEST_INTERVAL_SECONDS
        )

    def _schedule_retry(self, delay: float) -> None:
        self._next_network_request_at = max(
            self._next_network_request_at,
            time.monotonic() + delay,
        )

    def _fetch_page(self, category_query: str, start: int, count: int) -> str:
        cache_key = f"{category_query}|{start}|{count}"
        cached = self.cache.get(cache_key, max_age=ARXIV_CACHE_MAX_AGE)
        if cached is not None:
            return cached
        for attempt in range(ARXIV_REQUEST_ATTEMPTS):
            self._wait_for_request_slot()
            try:
                response = self.client.get(
                    ARXIV_QUERY_URL,
                    params={
                        "search_query": category_query,
                        "start": start,
                        "max_results": count,
                        "sortBy": "submittedDate",
                        "sortOrder": "descending",
                    },
                    headers={
                        "User-Agent": (
                            "arxiv-article-updater/0.1 "
                            "(research paper discovery; personal use)"
                        )
                    },
                )
            except httpx.TransportError:
                if attempt == ARXIV_REQUEST_ATTEMPTS - 1:
                    raise
                self._schedule_retry(ARXIV_RETRY_BASE_SECONDS * (2**attempt))
                continue

            if _retryable_status(response.status_code):
                if attempt == ARXIV_REQUEST_ATTEMPTS - 1:
                    response.raise_for_status()
                retry_delay = _retry_after_seconds(
                    response,
                    ARXIV_RETRY_BASE_SECONDS * (2**attempt),
                )
                self._schedule_retry(retry_delay)
                continue

            response.raise_for_status()
            self.cache.put(cache_key, response.text)
            return response.text

        raise RuntimeError("arXiv request attempts exhausted")

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        category_query = " OR ".join(
            f"cat:{category}" for category in self.settings.arxiv_categories
        )
        candidates: list[PaperCandidate] = []
        start = 0
        for _page_number in range(self.max_pages):
            if self.max_results is not None and start >= self.max_results:
                break
            count = (
                min(self.page_size, self.max_results - start)
                if self.max_results is not None
                else self.page_size
            )
            page = parse_arxiv_feed(self._fetch_page(category_query, start, count))
            candidates.extend(
                candidate
                for candidate in page
                if not since
                or max(
                    candidate.published_at or since,
                    candidate.updated_at or candidate.published_at or since,
                )
                >= since
            )
            if len(page) < count:
                break
            if (
                since
                and page
                and all(
                    max(
                        candidate.published_at or since,
                        candidate.updated_at or candidate.published_at or since,
                    )
                    < since
                    for candidate in page
                )
            ):
                break
            start += count
        return candidates
