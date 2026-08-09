import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import func, select

from arxiv_updater.config import Settings
from arxiv_updater.services.papers import normalize_doi, normalize_title, upsert_paper
from arxiv_updater.sources.arxiv import ArxivAdapter, parse_arxiv_feed
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
