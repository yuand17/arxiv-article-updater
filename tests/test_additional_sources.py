from pathlib import Path

import pytest

from arxiv_updater.sources.journals import JournalFeed, parse_journal_feed
from arxiv_updater.sources.scholar import parse_scholar_author_id, parse_scholar_response
from arxiv_updater.sources.scirate import parse_scirate_page

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


def test_parse_scirate_page():
    records = parse_scirate_page((FIXTURES / "scirate.html").read_text(encoding="utf-8"))
    assert [(record.arxiv_id, record.scites_count) for record in records] == [
        ("2607.12345", 12),
        ("2607.55555", 0),
    ]


def test_parse_journal_feed_filters_corrections():
    feed = JournalFeed("Example Journal", "https://example.com/rss", "0000-0000")
    papers = parse_journal_feed((FIXTURES / "journal.rss").read_text(encoding="utf-8"), feed)
    assert len(papers) == 1
    assert papers[0].doi == "10.1103/example.42"
    assert papers[0].authors == ["Alice Physicist"]
    assert papers[0].abstract == "We report a controlled observation."
