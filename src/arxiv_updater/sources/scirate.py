import re
from dataclasses import dataclass
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from .base import PaperCandidate, SourceAdapter


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

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.records: list[SciRateRecord] = []

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        response = self.client.get(
            "https://scirate.com/arxiv/quant-ph",
            headers={"User-Agent": "arxiv-article-updater/0.1 (low-frequency research feed)"},
        )
        response.raise_for_status()
        self.records = parse_scirate_page(response.text)
        return []

