from pathlib import Path

import httpx
import pytest

from arxiv_updater.sources.cache import DailyResponseCache
from arxiv_updater.sources.journals import JournalFeed, parse_journal_feed
from arxiv_updater.sources.scholar import (
    parse_scholar_author_id,
    parse_scholar_citation_count,
    parse_scholar_response,
)
from arxiv_updater.sources.scirate import SciRateAdapter, parse_scirate_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_scholar_author_id():
    assert (
        parse_scholar_author_id("https://scholar.google.com/citations?user=Qexu0QwAAAAJ&hl=en")
        == "Qexu0QwAAAAJ"
    )
    with pytest.raises(ValueError):
        parse_scholar_author_id("https://example.com/citations?user=Qexu0QwAAAAJ")


def test_parse_scholar_response():
    name, papers = parse_scholar_response(
        {
            "author": {"name": "Ada Researcher"},
            "articles": [
                {
                    "citation_id": "abc:123",
                    "title": "A new quantum result",
                    "authors": "A Researcher, B Scientist",
                    "year": "2026",
                    "link": "https://scholar.google.com/example",
                    "cited_by": {"value": 4},
                }
            ],
        }
    )
    assert name == "Ada Researcher"
    assert papers[0].authors == ["A Researcher", "B Scientist"]
    assert papers[0].metadata["cited_by"] == 4


def test_parse_scholar_citation_count():
    payload = {
        "cited_by": {
            "table": [
                {"citations": {"all": "12,345", "since_2021": 10000}},
                {"h_index": {"all": 52, "since_2021": 45}},
            ]
        }
    }
    assert parse_scholar_citation_count(payload) == 12345
    assert parse_scholar_citation_count({}) is None


def test_parse_scirate_page():
    records = parse_scirate_page((FIXTURES / "scirate.html").read_text(encoding="utf-8"))
    assert [(record.arxiv_id, record.scites_count) for record in records] == [
        ("2607.12345", 12),
        ("2607.55555", 0),
    ]
    assert records[0].title == "A paper"
    assert records[0].authors == ["Alice Example", "Bob Example"]
    assert records[0].categories == ["quant-ph"]
    assert records[0].abstract == "A useful quantum result."


def test_scirate_fetch_stops_at_vote_sorted_first_fifty(tmp_path):
    items = "".join(
        f"""
        <li class="paper">
          <div class="title"><a>Paper {index}</a></div>
          <div class="authors"><a>Author {index}</a></div>
          <div class="uid">Aug 8 2026 <a href="/arxiv/quant-ph">quant-ph</a>
            arXiv:2608.{index:05d}v1</div>
          <div class="scites-count"><button class="count">{index}</button></div>
          <div class="abstract">Abstract {index}</div>
        </li>
        """
        for index in range(55)
    )
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=items))
    )
    adapter = SciRateAdapter(client=client, cache=DailyResponseCache("scirate", tmp_path))

    candidates = adapter.fetch()

    assert len(candidates) == 50
    assert candidates[0].arxiv_id == "2608.00054"
    assert candidates[-1].arxiv_id == "2608.00005"
    assert candidates[0].metadata == {"scites_count": 54, "rank": 1, "range_days": 3}


def test_scirate_fetch_reports_cloudflare_block_without_retries(tmp_path):
    requests = 0

    def blocked(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(403, text="Cloudflare security verification")

    client = httpx.Client(transport=httpx.MockTransport(blocked))
    adapter = SciRateAdapter(client=client, cache=DailyResponseCache("scirate", tmp_path))

    with pytest.raises(RuntimeError, match="HTTP 403.*Cloudflare"):
        adapter.fetch()
    assert requests == 1


def test_scirate_manual_fetch_uses_human_chrome_after_cloudflare(tmp_path):
    html = (FIXTURES / "scirate.html").read_text(encoding="utf-8")
    calls: list[tuple[str, Path, float]] = []

    def browser_fetcher(url: str, profile: Path, timeout: float) -> str:
        calls.append((url, profile, timeout))
        return html

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                headers={"server": "cloudflare", "cf-mitigated": "challenge"},
                text="Security verification",
            )
        )
    )
    profile = tmp_path / "chrome-profile"
    adapter = SciRateAdapter(
        client=client,
        cache=DailyResponseCache("scirate", tmp_path),
        allow_browser_challenge=True,
        browser_fetcher=browser_fetcher,
        browser_profile_directory=profile,
        browser_timeout_seconds=42,
    )

    candidates = adapter.fetch()

    assert len(candidates) == 2
    assert calls == [("https://scirate.com/?range=3", profile, 42)]


def test_parse_journal_feed_filters_corrections():
    feed = JournalFeed("Example Journal", "https://example.com/rss", "0000-0000")
    papers = parse_journal_feed((FIXTURES / "journal.rss").read_text(encoding="utf-8"), feed)
    assert len(papers) == 1
    assert papers[0].doi == "10.1103/example.42"
    assert papers[0].authors == ["Alice Physicist"]
    assert papers[0].abstract == "We report a controlled observation."
