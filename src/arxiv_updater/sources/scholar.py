import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from ..config import Settings, get_settings
from ..security import redact_sensitive_text
from .base import PaperCandidate, SourceAdapter

AUTHOR_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
RECENT_ARTICLE_LIMIT = 10


@dataclass(frozen=True, slots=True)
class SerpApiAccountUsage:
    searches_per_month: int
    this_month_usage: int
    total_searches_left: int


def _nonnegative_account_value(payload: dict, field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError("SerpAPI 账户额度响应无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SerpAPI 账户额度响应无效") from exc
    if parsed < 0:
        raise RuntimeError("SerpAPI 账户额度响应无效")
    return parsed


def parse_scholar_author_id(value: str) -> str:
    value = value.strip()
    if AUTHOR_ID_PATTERN.fullmatch(value):
        return value
    parsed = urlparse(value)
    if parsed.netloc not in {"scholar.google.com", "scholar.google.co.uk", "scholar.google.cn"}:
        raise ValueError("请输入 Google Scholar 作者主页链接")
    author_id = parse_qs(parsed.query).get("user", [""])[0]
    if not AUTHOR_ID_PATTERN.fullmatch(author_id):
        raise ValueError("链接中没有有效的 Scholar 作者 ID")
    return author_id


def _article_year(value: object) -> datetime | None:
    try:
        year = int(str(value))
    except (TypeError, ValueError):
        return None
    if 1900 <= year <= datetime.now(UTC).year + 1:
        return datetime(year, 1, 1, tzinfo=UTC)
    return None


def parse_scholar_response(payload: dict) -> tuple[str, list[PaperCandidate]]:
    author = payload.get("author", {})
    author_name = str(author.get("name") or "Unknown author")
    candidates: list[PaperCandidate] = []
    for item in payload.get("articles", []):
        citation_id = str(item.get("citation_id") or "").strip()
        title = str(item.get("title") or "").strip()
        if not citation_id or not title:
            continue
        authors = [part.strip() for part in str(item.get("authors") or "").split(",")]
        candidates.append(
            PaperCandidate(
                source="scholar",
                external_id=citation_id,
                scholar_citation_id=citation_id,
                title=title,
                authors=[name for name in authors if name],
                published_at=_article_year(item.get("year")),
                canonical_url=item.get("link"),
                metadata={
                    "publication": item.get("publication"),
                    "cited_by": (item.get("cited_by") or {}).get("value", 0),
                    "tracked_author": author_name,
                },
            )
        )
    return author_name, candidates


def parse_scholar_citation_count(payload: dict) -> int | None:
    table = (payload.get("cited_by") or {}).get("table") or []
    if not isinstance(table, list):
        return None
    for metric in table:
        citations = metric.get("citations") if isinstance(metric, dict) else None
        if not isinstance(citations, dict):
            continue
        raw_value = citations.get("all")
        if raw_value is None or isinstance(raw_value, bool):
            return None
        try:
            return max(0, int(str(raw_value).replace(",", "").strip()))
        except ValueError:
            return None
    return None


class ScholarAdapter(SourceAdapter):
    name = "scholar"

    def __init__(
        self,
        author_ids: list[str],
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.author_ids = author_ids
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.author_names: dict[str, str] = {}
        self.author_citation_counts: dict[str, int] = {}
        self.account_usage_before: SerpApiAccountUsage | None = None

    def fetch_account_usage(self) -> SerpApiAccountUsage:
        """Read the provider's authoritative quota counters without spending a search."""

        if not self.settings.serpapi_api_key:
            raise RuntimeError("SERPAPI_API_KEY is not configured")
        try:
            response = self.client.get(
                "https://serpapi.com/account.json",
                params={"api_key": self.settings.serpapi_api_key},
            )
        except httpx.HTTPError as exc:
            raise RuntimeError("无法读取 SerpAPI 账户额度") from exc
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"SerpAPI 账户接口返回 HTTP {response.status_code}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("SerpAPI 账户额度响应无效") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("SerpAPI 账户额度响应无效")
        return SerpApiAccountUsage(
            searches_per_month=_nonnegative_account_value(payload, "searches_per_month"),
            this_month_usage=_nonnegative_account_value(payload, "this_month_usage"),
            total_searches_left=_nonnegative_account_value(payload, "total_searches_left"),
        )

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        if not self.settings.serpapi_api_key:
            raise RuntimeError("SERPAPI_API_KEY is not configured")
        results: list[PaperCandidate] = []
        for author_id in self.author_ids:
            response = self.client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google_scholar_author",
                    "author_id": author_id,
                    "sort": "pubdate",
                    "num": RECENT_ARTICLE_LIMIT,
                    "api_key": self.settings.serpapi_api_key,
                },
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(f"SerpAPI 返回 HTTP {response.status_code}") from exc
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(
                    redact_sensitive_text(
                        payload["error"],
                        (self.settings.serpapi_api_key,),
                    )
                )
            name, candidates = parse_scholar_response(payload)
            candidates = candidates[:RECENT_ARTICLE_LIMIT]
            self.author_names[author_id] = name
            citation_count = parse_scholar_citation_count(payload)
            if citation_count is not None:
                self.author_citation_counts[author_id] = citation_count
            for candidate in candidates:
                candidate.metadata["tracked_author_id"] = author_id
            results.extend(candidates)
        return results
