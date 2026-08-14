import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup
from dateutil.parser import isoparse, parse

from ..journal_network import get_journal_network
from .base import PaperCandidate, SourceAdapter


@dataclass(frozen=True, slots=True)
class JournalFeed:
    name: str
    url: str
    issn: str
    kind: str = "rss"


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
                try:
                    return parse(str(value))
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


def _entry_document_type(entry: dict) -> str:
    for key in (
        "prism_section",
        "dc_type",
        "type",
        "content_type",
        "article_type",
    ):
        value = entry.get(key)
        if value:
            return _clean_html(str(value))
    return ""


def _entry_subjects(entry: dict) -> list[str]:
    values: list[str] = []
    for tag in entry.get("tags", []):
        term = str(tag.get("term") or "").strip()
        if term:
            values.append(term)
    for key in ("dc_subject", "prism_category"):
        value = str(entry.get(key) or "").strip()
        if value:
            values.append(value)
    return sorted(set(values))


def parse_journal_feed(content: str, journal: JournalFeed) -> list[PaperCandidate]:
    parsed = feedparser.parse(content)
    candidates: list[PaperCandidate] = []
    for entry in parsed.entries:
        title = _clean_html(str(entry.get("title") or ""))
        if not title:
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
                metadata={
                    "journal": journal.name,
                    "issn": journal.issn,
                    "document_type": _entry_document_type(entry),
                    "subjects": _entry_subjects(entry),
                },
            )
        )
    return candidates


def _crossref_date(item: dict[str, Any]) -> datetime | None:
    for key in ("published-online", "published-print", "published", "issued"):
        date_parts = (item.get(key) or {}).get("date-parts") or []
        if not date_parts or not date_parts[0]:
            continue
        values = [int(value) for value in date_parts[0][:3]]
        values.extend([1] * (3 - len(values)))
        try:
            return datetime(values[0], values[1], values[2], tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_crossref_works(payload: dict[str, Any], journal: JournalFeed) -> list[PaperCandidate]:
    candidates: list[PaperCandidate] = []
    for item in (payload.get("message") or {}).get("items", []):
        titles = item.get("title") or []
        title = _clean_html(str(titles[0] if titles else ""))
        doi = str(item.get("DOI") or "").strip().lower() or None
        published_at = _crossref_date(item)
        if not title or not doi or published_at is None:
            continue
        authors = []
        for author in item.get("author") or []:
            author_parts = (
                str(author.get("given") or ""),
                str(author.get("family") or ""),
            )
            name = " ".join(
                part for part in author_parts if part
            ).strip()
            if name:
                authors.append(name)
        resource_url = str(
            ((item.get("resource") or {}).get("primary") or {}).get("URL") or ""
        ).strip()
        candidates.append(
            PaperCandidate(
                source="journal",
                external_id=doi,
                title=title,
                authors=authors,
                abstract=_clean_html(str(item.get("abstract") or "")),
                published_at=published_at,
                doi=doi,
                canonical_url=resource_url or f"https://doi.org/{doi}",
                metadata={
                    "journal": journal.name,
                    "issn": journal.issn,
                    "document_type": str(item.get("subtype") or item.get("type") or ""),
                    "subjects": item.get("subject") or [],
                    "crossref_type": item.get("type"),
                },
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
        self.feeds = feeds if feeds is not None else []
        self.client = client or get_journal_network().client
        self.errors: list[str] = []

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        primary_candidates: list[PaperCandidate] = []
        enrichment_candidates: list[PaperCandidate] = []
        has_primary_feed = any(journal.kind != "crossref" for journal in self.feeds)
        for journal in self.feeds:
            if journal.kind == "crossref":
                enrichment_candidates.extend(
                    self._fetch_crossref(
                        journal,
                        since,
                        max_pages=1 if has_primary_feed else 100,
                    )
                )
                continue
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
                primary_candidates.append(candidate)
        candidates = (
            _merge_enrichment(primary_candidates, enrichment_candidates)
            if has_primary_feed
            else _deduplicate_candidates(enrichment_candidates)
        )
        if not candidates and self.errors:
            raise RuntimeError("; ".join(self.errors))
        return candidates

    def _fetch_crossref(
        self,
        journal: JournalFeed,
        since: datetime | None,
        *,
        max_pages: int = 100,
    ) -> list[PaperCandidate]:
        cursor = "*"
        results: list[PaperCandidate] = []
        for _page in range(max_pages):
            params: dict[str, str | int] = {
                "rows": 100,
                "cursor": cursor,
                "sort": "published",
                "order": "desc",
            }
            if since:
                params["filter"] = f"from-pub-date:{since.date().isoformat()}"
            try:
                response = self.client.get(
                    journal.url,
                    params=params,
                    headers={"User-Agent": "arxiv-updater/0.2 (mailto:local@localhost)"},
                )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                self.errors.append(f"{journal.name}: {type(exc).__name__}")
                break
            page = parse_crossref_works(payload, journal)
            results.extend(page)
            message = payload.get("message") or {}
            next_cursor = str(message.get("next-cursor") or "")
            if len(message.get("items") or []) < 100 or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return results


def _candidate_key(candidate: PaperCandidate) -> str:
    return (candidate.doi or candidate.external_id).strip().lower()


def _merge_candidate(primary: PaperCandidate, supplement: PaperCandidate) -> None:
    """Fill sparse official-feed metadata without changing its article universe."""

    if supplement.abstract:
        primary.abstract = supplement.abstract
    if not primary.authors and supplement.authors:
        primary.authors = supplement.authors
    if primary.published_at is None:
        primary.published_at = supplement.published_at
    if not primary.canonical_url:
        primary.canonical_url = supplement.canonical_url
    primary.categories = sorted(set(primary.categories + supplement.categories))
    primary_subjects = primary.metadata.get("subjects") or []
    supplement_subjects = supplement.metadata.get("subjects") or []
    primary.metadata["subjects"] = sorted(set(primary_subjects + supplement_subjects))
    for key, value in supplement.metadata.items():
        if key == "subjects":
            continue
        if not primary.metadata.get(key) and value:
            primary.metadata[key] = value


def _deduplicate_candidates(candidates: list[PaperCandidate]) -> list[PaperCandidate]:
    by_key: dict[str, PaperCandidate] = {}
    for candidate in candidates:
        key = _candidate_key(candidate)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = candidate
        else:
            _merge_candidate(existing, candidate)
    return list(by_key.values())


def _merge_enrichment(
    primary_candidates: list[PaperCandidate],
    enrichment_candidates: list[PaperCandidate],
) -> list[PaperCandidate]:
    """Enrich RSS entries by DOI and discard Crossref-only records."""

    primary = _deduplicate_candidates(primary_candidates)
    enrichment = {
        _candidate_key(candidate): candidate
        for candidate in _deduplicate_candidates(enrichment_candidates)
    }
    for candidate in primary:
        supplement = enrichment.get(_candidate_key(candidate))
        if supplement is not None:
            _merge_candidate(candidate, supplement)
    return primary
