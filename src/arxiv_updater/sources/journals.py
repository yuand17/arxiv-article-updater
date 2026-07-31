import re
from dataclasses import dataclass
from datetime import UTC, datetime

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil.parser import isoparse

from .base import PaperCandidate, SourceAdapter


@dataclass(frozen=True, slots=True)
class JournalFeed:
    name: str
    url: str
    issn: str


DEFAULT_JOURNAL_FEEDS = [
    JournalFeed("Nature", "https://www.nature.com/nature.rss", "1476-4687"),
    JournalFeed("Nature Physics", "https://www.nature.com/nphys.rss", "1745-2481"),
    JournalFeed("Physical Review Letters", "https://feeds.aps.org/rss/recent/prl.xml", "1079-7114"),
]

EXCLUDED_TITLE_TERMS = (
    "briefing chat",
    "daily briefing",
    "editorial",
    "news & views",
    "podcast",
    "publisher correction",
    "author correction",
    "erratum",
    "retraction note",
)
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


def _clean_html(value: str) -> str:
    return " ".join(BeautifulSoup(value or "", "html.parser").get_text(" ").split())


def _entry_date(entry: dict) -> datetime | None:
    for key in ("published", "updated", "dc_date"):
        value = entry.get(key)
        if value:
            try:
                return isoparse(str(value))
            except ValueError:
                continue
    return None


def _entry_doi(entry: dict) -> str | None:
    values = [
        entry.get("prism_doi"),
        entry.get("dc_identifier"),
        entry.get("id"),
        entry.get("link"),
    ]
    for value in values:
        if not value:
            continue
        match = DOI_PATTERN.search(str(value))
        if match:
            return match.group(0).rstrip(".>").lower()
    return None


def parse_journal_feed(content: str, journal: JournalFeed) -> list[PaperCandidate]:
    parsed = feedparser.parse(content)
    candidates: list[PaperCandidate] = []
    for entry in parsed.entries:
        title = _clean_html(str(entry.get("title") or ""))
        if not title or any(term in title.lower() for term in EXCLUDED_TITLE_TERMS):
            continue
        doi = _entry_doi(entry)
        link = str(entry.get("link") or "") or None
        authors = []
        for author in entry.get("authors", []):
            name = str(author.get("name") or "").strip()
            if name:
                authors.append(name)
        if not authors and entry.get("author"):
            authors = [part.strip() for part in str(entry["author"]).split(",")]
        external_id = doi or str(entry.get("id") or link or title)
        candidates.append(
            PaperCandidate(
                source="journal",
                external_id=external_id,
                title=title,
                authors=[name for name in authors if name],
                abstract=_clean_html(str(entry.get("summary") or entry.get("description") or "")),
                published_at=_entry_date(entry),
                doi=doi,
                canonical_url=link or (f"https://doi.org/{doi}" if doi else None),
                metadata={"journal": journal.name, "issn": journal.issn},
            )
        )
    return candidates


class JournalAdapter(SourceAdapter):
    name = "journals"

    def __init__(
        self,
        feeds: list[JournalFeed] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.feeds = feeds or DEFAULT_JOURNAL_FEEDS
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.errors: list[str] = []

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        candidates: list[PaperCandidate] = []
        for journal in self.feeds:
            try:
                response = self.client.get(
                    journal.url,
                    headers={"User-Agent": "arxiv-article-updater/0.1 (research feed reader)"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                self.errors.append(f"{journal.name}: {type(exc).__name__}")
                continue
            for candidate in parse_journal_feed(response.text, journal):
                published = candidate.published_at
                if published and not published.tzinfo:
                    published = published.replace(tzinfo=UTC)
                if since and published and published < since:
                    continue
                candidates.append(candidate)
        if not candidates and self.errors:
            raise RuntimeError("; ".join(self.errors))
        return candidates
