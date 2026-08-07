import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from .base import PaperCandidate, SourceAdapter
from .cache import DailyResponseCache


@dataclass(slots=True)
class SciRateRecord:
    arxiv_id: str
    scites_count: int


def parse_scirate_page(content: str) -> list[SciRateRecord]:
    soup = BeautifulSoup(content, "html.parser")
    records: list[SciRateRecord] = []
    for item in soup.select("li.paper"):
        uid_text = item.select_one(".uid")
        if not uid_text:
            continue
        match = re.search(r"arXiv:([\w./-]+?)(?:v\d+)?(?:\s|$)", uid_text.get_text(" "))
        if not match:
            continue
        count_node = item.select_one(".scites-count .count")
        count_text = count_node.get_text(strip=True) if count_node else "0"
        try:
            count = int(count_text)
        except ValueError:
            count = 0
        records.append(SciRateRecord(arxiv_id=match.group(1), scites_count=count))
    return records


class SciRateAdapter(SourceAdapter):
    name = "scirate"

    def __init__(
        self,
        client: httpx.Client | None = None,
        cache: DailyResponseCache | None = None,
    ) -> None:
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.cache = cache or DailyResponseCache("scirate")
        self.records: list[SciRateRecord] = []

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        # The site exposes its rolling community list through this explicit three-day range.
        url = "https://scirate.com/?range=3"
        content = self.cache.get(url)
        if content is None:
            last_error: httpx.HTTPError | None = None
            for attempt in range(3):
                try:
                    response = self.client.get(
                        url,
                        headers={
                            "User-Agent": "arxiv-article-updater/0.1 (low-frequency research feed)"
                        },
                    )
                    response.raise_for_status()
                    content = response.text
                    self.cache.put(url, content)
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    if attempt < 2:
                        time.sleep(2**attempt)
            if content is None:
                raise RuntimeError("SciRate request failed after limited retries") from last_error
        self.records = parse_scirate_page(content)
        if not self.records:
            raise RuntimeError("SciRate page structure changed or returned no papers")
        return []
