import re
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx
from dateutil.parser import isoparse  # type: ignore[import-untyped]

from ..config import Settings, get_settings
from .base import PaperCandidate, SourceAdapter

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"
ARXIV_ID_PATTERN = re.compile(r"(?:abs/)?([^/]+?)(?:v\d+)?$")


def normalize_arxiv_id(value: str) -> str:
    match = ARXIV_ID_PATTERN.search(value.strip())
    return match.group(1) if match else value.strip()


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
        authors = [
            _text(author, f"{ATOM}name") for author in entry.findall(f"{ATOM}author")
        ]
        links = {
            link.attrib.get("rel"): link.attrib.get("href")
            for link in entry.findall(f"{ATOM}link")
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
        max_results: int = 500,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.max_results = max_results

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        category_query = " OR ".join(
            f"cat:{category}" for category in self.settings.arxiv_categories
        )
        response = self.client.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": category_query,
                "start": 0,
                "max_results": self.max_results,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            },
            headers={
                "User-Agent": "arxiv-article-updater/0.1 (research paper discovery; personal use)"
            },
        )
        response.raise_for_status()
        return parse_arxiv_feed(response.text, since)
