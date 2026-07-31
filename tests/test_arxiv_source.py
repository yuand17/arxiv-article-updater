from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from arxiv_updater.services.papers import normalize_doi, normalize_title, upsert_paper
from arxiv_updater.sources.arxiv import parse_arxiv_feed

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
