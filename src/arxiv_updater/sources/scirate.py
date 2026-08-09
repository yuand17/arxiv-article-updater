import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .base import PaperCandidate, SourceAdapter
from .cache import DailyResponseCache
from .human_browser import fetch_page_with_human_chrome

SCIRATE_URL = "https://scirate.com/?range=3"
SCIRATE_PAGE_LIMIT = 50
SCIRATE_REQUEST_ATTEMPTS = 3
BrowserFetcher = Callable[[str, Path, float], str]


@dataclass(slots=True)
class SciRateRecord:
    arxiv_id: str
    scites_count: int
    title: str = ""
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published_at: datetime | None = None
    categories: list[str] = field(default_factory=list)
    pdf_url: str | None = None

    def as_candidate(self, *, rank: int) -> PaperCandidate | None:
        if not self.title:
            return None
        return PaperCandidate(
            source="scirate",
            external_id=self.arxiv_id,
            title=self.title,
            authors=self.authors,
            abstract=self.abstract,
            published_at=self.published_at,
            updated_at=self.published_at,
            arxiv_id=self.arxiv_id,
            categories=self.categories,
            canonical_url=f"https://arxiv.org/abs/{self.arxiv_id}",
            pdf_url=self.pdf_url or f"https://arxiv.org/pdf/{self.arxiv_id}",
            metadata={"scites_count": self.scites_count, "rank": rank, "range_days": 3},
        )


def _published_at(uid_text: str) -> datetime | None:
    match = re.search(r"\b([A-Z][a-z]{2}\s+\d{1,2}\s+\d{4})\b", uid_text)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%b %d %Y").replace(tzinfo=UTC)
    except ValueError:
        return None


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
        title_node = item.select_one(".title a")
        author_nodes = item.select(".authors a")
        abstract_node = item.select_one(".abstract")
        pdf_node = item.select_one("a.paper-download")
        category_nodes = uid_text.select('a[href^="/arxiv/"]')
        categories = [
            node.get_text(" ", strip=True)
            for node in category_nodes
            if node.get_text(" ", strip=True)
        ]
        pdf_href = pdf_node.get("href") if pdf_node else None
        records.append(
            SciRateRecord(
                arxiv_id=match.group(1),
                scites_count=count,
                title=title_node.get_text(" ", strip=True) if title_node else "",
                authors=[
                    author
                    for node in author_nodes
                    if (author := node.get_text(" ", strip=True))
                    and not author.lower().startswith("et al")
                ],
                abstract=abstract_node.get_text(" ", strip=True) if abstract_node else "",
                published_at=_published_at(uid_text.get_text(" ", strip=True)),
                categories=categories,
                pdf_url=urljoin("https://scirate.com", str(pdf_href)) if pdf_href else None,
            )
        )
    return records


class SciRateAdapter(SourceAdapter):
    name = "scirate"

    def __init__(
        self,
        client: httpx.Client | None = None,
        cache: DailyResponseCache | None = None,
        *,
        allow_browser_challenge: bool = False,
        browser_fetcher: BrowserFetcher = fetch_page_with_human_chrome,
        browser_profile_directory: Path | None = None,
        browser_timeout_seconds: float | None = None,
    ) -> None:
        from ..config import get_settings

        settings = get_settings()
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.cache = cache or DailyResponseCache("scirate")
        self.allow_browser_challenge = allow_browser_challenge
        self.browser_fetcher = browser_fetcher
        self.browser_profile_directory = browser_profile_directory or Path(
            settings.scirate_browser_profile_dir
        )
        self.browser_timeout_seconds = (
            browser_timeout_seconds
            if browser_timeout_seconds is not None
            else float(settings.scirate_browser_timeout_seconds)
        )
        self.records: list[SciRateRecord] = []

    @staticmethod
    def _is_cloudflare_challenge(response: httpx.Response) -> bool:
        body = response.text.lower()
        return response.status_code == 403 and (
            response.headers.get("cf-mitigated", "").lower() == "challenge"
            or "cloudflare" in response.headers.get("server", "").lower()
            or "cloudflare" in body
            or "security verification" in body
            or "安全验证" in body
        )

    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        # SciRate's first three-day page is the site's own vote-sorted top-50 view.
        content = self.cache.get(SCIRATE_URL)
        if content is None:
            last_error: httpx.HTTPError | None = None
            for attempt in range(SCIRATE_REQUEST_ATTEMPTS):
                try:
                    response = self.client.get(
                        SCIRATE_URL,
                        headers={
                            "User-Agent": "arxiv-article-updater/0.1 (low-frequency research feed)"
                        },
                    )
                    if self._is_cloudflare_challenge(response):
                        if self.allow_browser_challenge:
                            try:
                                content = self.browser_fetcher(
                                    SCIRATE_URL,
                                    self.browser_profile_directory,
                                    self.browser_timeout_seconds,
                                )
                            except RuntimeError as exc:
                                raise RuntimeError(
                                    f"SciRate Chrome 真人验证未完成：{exc}；已保留上次成功数据"
                                ) from exc
                            self.cache.put(SCIRATE_URL, content)
                            break
                        raise RuntimeError(
                            "SciRate 返回 HTTP 403（Cloudflare 安全验证）；当前网络无法自动读取"
                            "过去三天榜单；请在设置页点击“立即更新”并完成 Chrome 真人验证，"
                            "已保留上次成功数据"
                        )
                    response.raise_for_status()
                    content = response.text
                    self.cache.put(SCIRATE_URL, content)
                    break
                except httpx.HTTPError as exc:
                    last_error = exc
                    status = (
                        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else 0
                    )
                    retryable = not status or status == 429 or status >= 500
                    if not retryable:
                        break
                    if attempt < SCIRATE_REQUEST_ATTEMPTS - 1:
                        time.sleep(2**attempt)
            if content is None:
                failure_status = (
                    last_error.response.status_code
                    if isinstance(last_error, httpx.HTTPStatusError)
                    else None
                )
                detail = (
                    f"HTTP {failure_status}"
                    if failure_status
                    else type(last_error).__name__
                )
                raise RuntimeError(
                    f"SciRate 请求失败（{detail}），已保留上次成功数据"
                ) from last_error

        records = sorted(
            parse_scirate_page(content), key=lambda item: item.scites_count, reverse=True
        )[:SCIRATE_PAGE_LIMIT]
        if not records:
            raise RuntimeError("SciRate 页面结构已变化或没有返回论文，已保留上次成功数据")
        candidates: list[PaperCandidate] = []
        for rank, record in enumerate(records, start=1):
            candidate = record.as_candidate(rank=rank)
            if candidate is None:
                raise RuntimeError(
                    f"SciRate 第 {rank} 篇论文缺少标题，页面结构可能已变化；已保留上次成功数据"
                )
            candidates.append(candidate)
        self.records = records
        return candidates
