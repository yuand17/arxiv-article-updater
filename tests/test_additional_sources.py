from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from arxiv_updater.config import Settings
from arxiv_updater.services.article_classification import classify_journal_candidate
from arxiv_updater.sources import human_browser
from arxiv_updater.sources.cache import DailyResponseCache
from arxiv_updater.sources.journals import JournalAdapter, JournalFeed, parse_journal_feed
from arxiv_updater.sources.scholar import (
    ScholarAdapter,
    SerpApiAccountUsage,
    parse_scholar_author_id,
    parse_scholar_citation_count,
    parse_scholar_response,
)
from arxiv_updater.sources.scirate import SciRateAdapter, parse_scirate_page

FIXTURES = Path(__file__).parent / "fixtures"


def test_chrome_discovery_supports_standard_macos_user_install(tmp_path, monkeypatch):
    chrome = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    chrome.parent.mkdir(parents=True)
    chrome.touch()
    monkeypatch.setattr(human_browser.shutil, "which", lambda _name: None)
    monkeypatch.setattr(human_browser.sys, "platform", "darwin")
    monkeypatch.setattr(human_browser.Path, "home", lambda: tmp_path)
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)

    assert human_browser.find_chrome_executable() == chrome.resolve()


def test_parse_scholar_author_id():
    assert (
        parse_scholar_author_id("https://scholar.google.com/citations?user=Qexu0QwAAAAJ&hl=en")
        == "Qexu0QwAAAAJ"
    )
    assert (
        parse_scholar_author_id(
            "https://scholar.google.com/citations?user=Qexu0QwAAAAJ"
            "&view_op=list_works&sortby=citedby&cstart=0&pagesize=100"
        )
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


def test_scholar_http_errors_never_include_the_serpapi_key():
    secret = "serpapi-secret-value"

    def unauthorized(request: httpx.Request) -> httpx.Response:
        assert request.url.params["api_key"] == secret
        return httpx.Response(401, request=request)

    adapter = ScholarAdapter(
        ["Qexu0QwAAAAJ"],
        settings=Settings(serpapi_api_key=secret),
        client=httpx.Client(transport=httpx.MockTransport(unauthorized)),
    )

    with pytest.raises(RuntimeError, match="SerpAPI 返回 HTTP 401") as caught:
        adapter.fetch()
    assert secret not in str(caught.value)


def test_scholar_account_usage_reads_authoritative_quota_without_exposing_key():
    secret = "serpapi-secret-value"

    def account(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account.json"
        assert request.url.params["api_key"] == secret
        return httpx.Response(
            200,
            request=request,
            json={
                "api_key": secret,
                "searches_per_month": 250,
                "this_month_usage": 46,
                "total_searches_left": 204,
            },
        )

    adapter = ScholarAdapter(
        ["Qexu0QwAAAAJ"],
        settings=Settings(serpapi_api_key=secret),
        client=httpx.Client(transport=httpx.MockTransport(account)),
    )

    usage = adapter.fetch_account_usage()

    assert usage == SerpApiAccountUsage(250, 46, 204)
    assert secret not in repr(usage)


def test_scholar_fetch_uses_date_sort_and_keeps_only_latest_ten():
    observed_params: dict[str, str] = {}

    def latest_articles(request: httpx.Request) -> httpx.Response:
        observed_params.update(request.url.params)
        return httpx.Response(
            200,
            request=request,
            json={
                "author": {"name": "Recent Researcher"},
                "articles": [
                    {
                        "citation_id": f"author:paper-{index}",
                        "title": f"Date-sorted paper {index}",
                        "authors": "Recent Researcher",
                        "year": "2026",
                    }
                    for index in range(12)
                ],
            },
        )

    adapter = ScholarAdapter(
        ["Qexu0QwAAAAJ"],
        settings=Settings(serpapi_api_key="test-key"),
        client=httpx.Client(transport=httpx.MockTransport(latest_articles)),
    )

    papers = adapter.fetch()

    assert observed_params["sort"] == "pubdate"
    assert observed_params["num"] == "10"
    assert adapter.search_requests_sent == 1
    assert [paper.title for paper in papers] == [
        f"Date-sorted paper {index}" for index in range(10)
    ]
    assert all(
        paper.metadata["tracked_author_id"] == "Qexu0QwAAAAJ" for paper in papers
    )


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


def test_journal_classification_filters_corrections_after_parsing():
    feed = JournalFeed("Physical Review Letters", "https://example.com/rss", "0000-0000")
    papers = parse_journal_feed((FIXTURES / "journal.rss").read_text(encoding="utf-8"), feed)
    assert len(papers) == 2
    assert papers[0].doi == "10.1103/example.42"
    assert papers[0].authors == ["Alice Physicist"]
    assert papers[0].abstract == "We report a controlled observation."
    results = [
        classify_journal_candidate(
            paper,
            journal_name="Physical Review Letters",
            scope_kind="physics",
        )
        for paper in papers
    ]
    assert results[0].accepted is True
    assert results[1].is_original_research is False


def test_journal_adapter_selects_network_from_primary_feed_hostname(monkeypatch):
    observed: list[str] = []
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<rss/>"))
    )
    monkeypatch.setattr(
        "arxiv_updater.sources.journals.get_journal_network",
        lambda hostname: observed.append(hostname) or SimpleNamespace(client=client),
    )

    JournalAdapter(
        feeds=[
            JournalFeed(
                "Science",
                "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
                "1095-9203",
            ),
            JournalFeed(
                "Science",
                "https://api.crossref.org/journals/1095-9203/works",
                "1095-9203",
                "crossref",
            ),
        ]
    )

    assert observed == ["www.science.org"]


def test_journal_adapter_retries_a_transport_failure(monkeypatch):
    calls = 0
    rss = (FIXTURES / "journal.rss").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary TLS EOF", request=request)
        return httpx.Response(200, text=rss)

    monkeypatch.setattr("arxiv_updater.sources.journals.time.sleep", lambda _seconds: None)
    adapter = JournalAdapter(
        feeds=[JournalFeed("Science", "https://www.science.org/feed", "1095-9203")],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert len(adapter.fetch()) == 2
    assert calls == 2
    assert adapter.errors == []


def test_journal_manual_fetch_uses_human_chrome_after_cloudflare(tmp_path):
    rss = (FIXTURES / "journal.rss").read_text(encoding="utf-8")
    calls: list[tuple[str, Path, float]] = []

    def browser_fetcher(url: str, profile: Path, timeout: float) -> str:
        calls.append((url, profile, timeout))
        return rss

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                headers={"server": "cloudflare", "cf-mitigated": "challenge"},
                text="Security verification",
            )
        )
    )
    profile = tmp_path / "journal-chrome-profile"
    adapter = JournalAdapter(
        feeds=[JournalFeed("Science", "https://www.science.org/feed", "1095-9203")],
        client=client,
        allow_browser_challenge=True,
        browser_fetcher=browser_fetcher,
        browser_profile_directory=profile,
        browser_timeout_seconds=45,
    )

    candidates = adapter.fetch()

    assert len(candidates) == 2
    assert calls == [("https://www.science.org/feed", profile, 45)]
    assert adapter.errors == []


@pytest.mark.parametrize("status_code", [408, 429, 503])
def test_journal_adapter_retries_only_transient_http_statuses(
    status_code, monkeypatch
):
    calls = 0
    rss = (FIXTURES / "journal.rss").read_text(encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, headers={"Retry-After": "0"})
        return httpx.Response(200, text=rss)

    monkeypatch.setattr("arxiv_updater.sources.journals.time.sleep", lambda _seconds: None)
    adapter = JournalAdapter(
        feeds=[JournalFeed("Science", "https://www.science.org/feed", "1095-9203")],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert len(adapter.fetch()) == 2
    assert calls == 2


def test_journal_adapter_does_not_retry_a_cloudflare_403(monkeypatch):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            403,
            headers={"server": "cloudflare", "cf-mitigated": "challenge"},
            text="sensitive challenge body",
        )

    monkeypatch.setattr("arxiv_updater.sources.journals.time.sleep", lambda _seconds: None)
    adapter = JournalAdapter(
        feeds=[JournalFeed("Science", "https://www.science.org/feed", "1095-9203")],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(
        RuntimeError,
        match=r"Science rss: HTTP 403 \(Cloudflare security verification\)",
    ) as caught:
        adapter.fetch()
    assert calls == 1
    assert "sensitive challenge body" not in str(caught.value)


def test_journal_adapter_reports_only_the_safe_transport_type(monkeypatch):
    calls = 0
    unsafe_detail = "private upstream detail"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(unsafe_detail, request=request)

    monkeypatch.setattr("arxiv_updater.sources.journals.time.sleep", lambda _seconds: None)
    adapter = JournalAdapter(
        feeds=[JournalFeed("Science", "https://www.science.org/feed", "1095-9203")],
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match=r"Science rss: ConnectError") as caught:
        adapter.fetch()
    assert calls == 3
    assert unsafe_detail not in str(caught.value)
