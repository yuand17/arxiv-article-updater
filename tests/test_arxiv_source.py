import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from arxiv_updater.config import Settings
from arxiv_updater.services.papers import (
    normalize_author_names,
    normalize_authors_text,
    normalize_doi,
    normalize_title,
    upsert_paper,
)
from arxiv_updater.sources.arxiv import ArxivAdapter, parse_arxiv_feed
from arxiv_updater.sources.base import PaperCandidate
from arxiv_updater.sources.cache import DailyResponseCache

FIXTURE = Path(__file__).parent / "fixtures" / "arxiv_feed.xml"


def test_parse_arxiv_feed():
    papers = parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))
    assert len(papers) == 1
    paper = papers[0]
    assert paper.arxiv_id == "2607.12345"
    assert paper.title == "Entanglement Growth in a Noisy Quantum Chain"
    assert paper.authors == ["Alice Example", "Bob Example"]
    assert paper.categories == ["quant-ph", "cond-mat.stat-mech"]
    assert paper.doi == "10.1000/example.1"


def test_parse_respects_since():
    papers = parse_arxiv_feed(
        FIXTURE.read_text(encoding="utf-8"), datetime(2026, 7, 31, tzinfo=UTC)
    )
    assert papers == []


def test_normalization():
    assert normalize_title("An Éxample:  Quantum—Paper!") == "an example quantum paper"
    assert normalize_doi("https://doi.org/10.1000/ABC") == "10.1000/abc"
    assert normalize_author_names(["Alice Example,", " Bob Example "]) == [
        "Alice Example",
        "Bob Example",
    ]
    assert normalize_authors_text("Alice Example,, Bob Example,") == (
        "Alice Example, Bob Example"
    )


def test_upsert_is_idempotent(app_client):
    _, session_factory, models = app_client
    candidate = parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))[0]
    with session_factory() as db:
        first = upsert_paper(db, candidate)
        second = upsert_paper(db, candidate)
        db.commit()
        assert first.created is True
        assert second.created is False
        assert db.scalar(select(func.count()).select_from(models.Paper)) == 1
        assert db.scalar(select(func.count()).select_from(models.PaperSource)) == 1


def test_upsert_repairs_malformed_author_separators(app_client):
    _, session_factory, _ = app_client
    candidate = parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))[0]
    with session_factory() as db:
        first = upsert_paper(db, candidate)
        first.paper.authors_text = "Alice Example,, Bob Example,"
        db.commit()

        second = upsert_paper(db, candidate)
        db.commit()

        assert second.created is False
        assert second.paper.authors_text == "Alice Example, Bob Example"


def test_upsert_replaces_journal_feed_summary_with_crossref_abstract(app_client):
    _, session_factory, _ = app_client
    feed_candidate = PaperCandidate(
        source="journal",
        external_id="10.1103/example",
        title="A journal result",
        authors=["Alice Example"],
        abstract="Author(s): Alice Example A truncated result… [Journal 1, 1] Published today",
        doi="10.1103/example",
        metadata={"abstract_source_kind": "feed-summary"},
    )
    crossref_candidate = replace(
        feed_candidate,
        abstract="The complete publisher-deposited abstract.",
        metadata={"abstract_source_kind": "crossref"},
    )

    with session_factory() as db:
        first = upsert_paper(db, feed_candidate)
        second = upsert_paper(db, crossref_candidate)
        db.commit()

        assert first.created is True
        assert second.created is False
        assert second.paper.abstract == "The complete publisher-deposited abstract."
        assert second.paper.abstract_source == "crossref"


def test_crossref_does_not_replace_an_existing_arxiv_abstract(app_client):
    _, session_factory, _ = app_client
    arxiv_candidate = PaperCandidate(
        source="arxiv",
        external_id="2608.00001",
        arxiv_id="2608.00001",
        title="A shared result",
        authors=["Alice Example"],
        abstract="The complete arXiv abstract.",
        doi="10.1103/example",
    )
    crossref_candidate = PaperCandidate(
        source="journal",
        external_id="10.1103/example",
        title="A shared result",
        authors=["Alice Example"],
        abstract="The publisher abstract.",
        doi="10.1103/example",
        metadata={"abstract_source_kind": "crossref"},
    )

    with session_factory() as db:
        upsert_paper(db, arxiv_candidate)
        result = upsert_paper(db, crossref_candidate)
        db.commit()

        assert result.created is False
        assert result.paper.abstract == "The complete arXiv abstract."
        assert result.paper.abstract_source == "arxiv"


def test_scholar_upsert_deduplicates_the_same_paper_across_tracked_authors(app_client):
    _, session_factory, models = app_client
    first = PaperCandidate(
        source="scholar",
        external_id="author-a:paper-1",
        scholar_citation_id="author-a:paper-1",
        title="A Shared Quantum Result",
        authors=["Alice Example", "Bob Example"],
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={"tracked_author_id": "author-a"},
    )
    second = replace(
        first,
        external_id="author-b:paper-1",
        scholar_citation_id="author-b:paper-1",
        metadata={"tracked_author_id": "author-b"},
    )

    with session_factory() as db:
        first_result = upsert_paper(db, first)
        second_result = upsert_paper(db, second)
        db.commit()

        assert first_result.created is True
        assert second_result.created is False
        assert db.scalar(select(func.count()).select_from(models.Paper)) == 1
        assert db.scalar(select(func.count()).select_from(models.PaperSource)) == 2


def test_upsert_normalizes_legacy_naive_sqlite_updated_at(app_client):
    _, session_factory, models = app_client
    candidate = parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))[0]
    with session_factory() as db:
        upsert_paper(db, candidate)
        db.commit()

    with session_factory() as db:
        stored = db.scalar(select(models.Paper).where(models.Paper.arxiv_id == candidate.arxiv_id))
        assert stored is not None
        assert stored.updated_at is not None
        assert stored.updated_at.tzinfo is None
        newer = replace(candidate, updated_at=candidate.updated_at + timedelta(minutes=1))
        second = upsert_paper(db, newer)
        db.commit()

        assert second.created is False

    with session_factory() as db:
        stored = db.scalar(select(models.Paper).where(models.Paper.arxiv_id == candidate.arxiv_id))
        assert stored is not None
        assert stored.updated_at == newer.updated_at.replace(tzinfo=None)


def test_newer_arxiv_revision_refreshes_authoritative_metadata_and_keeps_interactions(
    app_client,
):
    _, session_factory, models = app_client
    original = replace(
        parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))[0],
        metadata={"revision": "v1"},
    )
    revised = replace(
        original,
        title="Revised Entanglement Growth",
        authors=["Alice Revised", "Bob Example", "Carol Example"],
        abstract="A corrected and expanded arXiv abstract.",
        published_at=original.published_at + timedelta(hours=1),
        updated_at=original.updated_at + timedelta(days=1),
        doi="10.1000/revised.2",
        categories=["quant-ph"],
        canonical_url="https://arxiv.org/abs/2607.12345v2",
        pdf_url="https://arxiv.org/pdf/2607.12345v2",
        metadata={"revision": "v2"},
    )

    with session_factory() as db:
        first = upsert_paper(db, original)
        first.paper.scites_count = 9
        db.add(
            models.Interaction(
                paper_id=first.paper.id,
                kind=models.InteractionKind.SAVED,
                weight=3.0,
            )
        )
        db.commit()

        second = upsert_paper(db, revised)
        db.commit()
        source = db.scalar(
            select(models.PaperSource).where(models.PaperSource.paper_id == second.paper.id)
        )

        assert second.created is False
        assert second.paper.title == revised.title
        assert second.paper.normalized_title == normalize_title(revised.title)
        assert second.paper.authors_text == "Alice Revised, Bob Example, Carol Example"
        assert second.paper.first_author == "alice revised"
        assert second.paper.abstract == revised.abstract
        assert second.paper.abstract_source == "arxiv"
        assert second.paper.published_at == revised.published_at
        assert second.paper.updated_at == revised.updated_at
        assert second.paper.doi == "10.1000/revised.2"
        assert second.paper.categories == ["quant-ph"]
        assert second.paper.canonical_url == revised.canonical_url
        assert second.paper.pdf_url == revised.pdf_url
        assert second.paper.scites_count == 9
        assert source is not None
        assert source.url == revised.canonical_url
        assert source.metadata_json == {"revision": "v2"}
        assert db.scalar(
            select(func.count())
            .select_from(models.Interaction)
            .where(models.Interaction.paper_id == second.paper.id)
        ) == 1


def test_older_arxiv_revision_cannot_overwrite_newer_metadata(app_client):
    _, session_factory, models = app_client
    old = replace(
        parse_arxiv_feed(FIXTURE.read_text(encoding="utf-8"))[0],
        metadata={"revision": "v1"},
    )
    current = replace(
        old,
        title="Current arXiv title",
        authors=["Current Author"],
        abstract="Current arXiv abstract.",
        updated_at=old.updated_at + timedelta(days=2),
        doi="10.1000/current.3",
        categories=["quant-ph"],
        canonical_url="https://arxiv.org/abs/2607.12345v3",
        pdf_url="https://arxiv.org/pdf/2607.12345v3",
        metadata={"revision": "v3"},
    )

    with session_factory() as db:
        upsert_paper(db, current)
        db.commit()
        result = upsert_paper(db, old)
        db.commit()
        source = db.scalar(
            select(models.PaperSource).where(models.PaperSource.paper_id == result.paper.id)
        )

        assert result.created is False
        assert result.paper.title == current.title
        assert result.paper.authors_text == "Current Author"
        assert result.paper.abstract == current.abstract
        assert result.paper.updated_at == current.updated_at.replace(tzinfo=None)
        assert result.paper.doi == "10.1000/current.3"
        assert result.paper.categories == ["quant-ph"]
        assert result.paper.canonical_url == current.canonical_url
        assert result.paper.pdf_url == current.pdf_url
        assert source is not None
        assert source.url == current.canonical_url
        assert source.metadata_json == {"revision": "v3"}


def test_arxiv_adapter_uses_daily_page_cache(tmp_path):
    cache = DailyResponseCache("arxiv-test", tmp_path)
    query = "cat:quant-ph"
    cache.put(f"{query}|0|1", FIXTURE.read_text(encoding="utf-8"))
    cache.put(
        f"{query}|1|1",
        '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        max_results=2,
        page_size=1,
        cache=cache,
    )

    papers = adapter.fetch()

    assert [paper.arxiv_id for paper in papers] == ["2607.12345"]


def test_daily_response_cache_can_expire_stale_arxiv_data(tmp_path):
    cache = DailyResponseCache("arxiv-expiry-test", tmp_path)
    cache.put("page", "old response")
    path = cache._path("page")
    os.utime(path, (0, 0))

    assert cache.get("page", max_age=timedelta(minutes=5)) is None


def test_arxiv_adapter_retries_rate_limit(tmp_path, monkeypatch):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", lambda _seconds: None)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=1,
        page_size=1,
        cache=DailyResponseCache("arxiv-retry-test", tmp_path),
    )

    papers = adapter.fetch()

    assert calls == 2
    assert [paper.arxiv_id for paper in papers] == ["2607.12345"]


def test_arxiv_adapter_retries_transport_error_and_keeps_three_second_spacing(
    tmp_path, monkeypatch
):
    calls = 0
    clock = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.monotonic", monotonic)
    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", sleep)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=1,
        page_size=1,
        cache=DailyResponseCache("arxiv-transport-retry-test", tmp_path),
    )

    papers = adapter.fetch()

    assert calls == 2
    assert sleeps == [3.0]
    assert [paper.arxiv_id for paper in papers] == ["2607.12345"]


@pytest.mark.parametrize(
    ("status_code", "retry_after", "expected_delay"),
    [(408, "7", 7.0), (503, "999", 30.0)],
)
def test_arxiv_adapter_retries_transient_status_and_bounds_retry_after(
    status_code, retry_after, expected_delay, tmp_path, monkeypatch
):
    calls = 0
    clock = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(status_code, headers={"Retry-After": retry_after})
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"))

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.monotonic", monotonic)
    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", sleep)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=1,
        page_size=1,
        cache=DailyResponseCache(f"arxiv-status-{status_code}-test", tmp_path),
    )

    papers = adapter.fetch()

    assert calls == 2
    assert sleeps == [expected_delay]
    assert [paper.arxiv_id for paper in papers] == ["2607.12345"]


def test_arxiv_adapter_does_not_retry_nontransient_403(tmp_path, monkeypatch):
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", lambda _seconds: None)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=1,
        page_size=1,
        cache=DailyResponseCache("arxiv-no-403-retry-test", tmp_path),
    )

    with pytest.raises(httpx.HTTPStatusError):
        adapter.fetch()
    assert calls == 1


def test_arxiv_adapter_stops_after_three_transport_failures(tmp_path, monkeypatch):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadError("temporary read failure", request=request)

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", lambda _seconds: None)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_results=1,
        page_size=1,
        cache=DailyResponseCache("arxiv-transport-exhaustion-test", tmp_path),
    )

    with pytest.raises(httpx.ReadError):
        adapter.fetch()
    assert calls == 3


def test_arxiv_adapter_pages_past_five_hundred_until_time_boundary(
    tmp_path, monkeypatch
):
    starts: list[int] = []

    def atom_page(start: int, *, old: bool) -> str:
        date = "2026-07-20T10:00:00Z" if old else "2026-07-30T10:00:00Z"
        entries = "".join(
            f"""<entry><id>http://arxiv.org/abs/2607.{start + index:05d}v1</id>
            <updated>{date}</updated><published>{date}</published>
            <title>Quantum paper {start + index}</title><summary>Quantum result</summary>
            <author><name>Alice Example</name></author><category term="quant-ph" />
            <link rel="alternate" href="https://arxiv.org/abs/2607.{start + index:05d}" />
            </entry>"""
            for index in range(100)
        )
        return f'<feed xmlns="http://www.w3.org/2005/Atom">{entries}</feed>'

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        starts.append(start)
        return httpx.Response(200, text=atom_page(start, old=start >= 600))

    monkeypatch.setattr("arxiv_updater.sources.arxiv.time.sleep", lambda _seconds: None)
    adapter = ArxivAdapter(
        settings=Settings(arxiv_categories=["quant-ph"]),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        page_size=100,
        max_pages=10,
        cache=DailyResponseCache("arxiv-boundary-test", tmp_path),
    )

    papers = adapter.fetch(datetime(2026, 7, 28, tzinfo=UTC))

    assert len(papers) == 600
    assert starts == [0, 100, 200, 300, 400, 500, 600]
