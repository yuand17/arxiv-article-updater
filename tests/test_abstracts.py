from datetime import UTC, datetime

import httpx

from arxiv_updater.services.abstracts import (
    abstract_needs_enrichment,
    enrich_paper_abstract,
)


def _paper(models, title: str, abstract: str = ""):
    return models.Paper(
        title=title,
        normalized_title=title.lower(),
        abstract=abstract,
        abstract_source="arxiv" if abstract else "",
        abstract_status="available" if abstract else "missing",
        authors_text="Alice Example",
        first_author="alice example",
        published_at=datetime.now(UTC),
        categories=["quant-ph"],
    )


def test_missing_abstract_does_not_use_fuzzy_title_match(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        known = _paper(models, "Trusted title", "A trusted original abstract.")
        missing = _paper(models, "Trusted title")
        db.add_all([known, missing])
        db.commit()
        enriched = enrich_paper_abstract(db, missing.id)
        assert enriched is not None
        assert enriched.abstract == ""
        assert enriched.abstract_source == ""
        assert enriched.abstract_status == "missing"


def test_abstract_endpoint_records_interest_even_before_enrichment(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        paper = _paper(models, "Missing abstract")
        db.add(paper)
        db.commit()
        paper_id = paper.id
    response = client.post(f"/papers/{paper_id}/abstract")
    assert response.status_code == 200
    assert "暂无摘要" in response.text
    assert "不会模糊搜索或编造内容" in response.text
    with session_factory() as db:
        interaction = (
            db.query(models.Interaction)
            .filter_by(paper_id=paper_id, kind=models.InteractionKind.ABSTRACT_VIEWED)
            .one()
        )
        assert interaction.weight > 0


def test_journal_feed_teasers_are_not_treated_as_complete_abstracts(app_client):
    _, _, models = app_client
    paper = _paper(
        models,
        "Feed title",
        "Author(s): Alice Example A truncated result… [Journal 1, 1] Published today",
    )
    paper.abstract_source = "journal"
    assert abstract_needs_enrichment(paper) is True

    paper.abstract = "A complete abstract without publisher feed boilerplate."
    assert abstract_needs_enrichment(paper) is False


def test_incomplete_journal_abstract_is_replaced_from_crossref(app_client):
    _, session_factory, models = app_client

    def crossref(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.crossref.org"
        assert request.url.path == "/works/10.1234/example"
        return httpx.Response(
            200,
            request=request,
            json={
                "message": {
                    "abstract": (
                        "<jats:title>Abstract</jats:title>"
                        "<jats:p>The complete abstract.</jats:p>"
                    )
                }
            },
        )

    with session_factory() as db, httpx.Client(
        transport=httpx.MockTransport(crossref)
    ) as client:
        paper = _paper(
            models,
            "Crossref title",
            "Nature Physics, Published online: today; doi:10.1234/example A short teaser.",
        )
        paper.abstract_source = "journal"
        paper.doi = "10.1234/example"
        db.add(paper)
        db.commit()

        enriched = enrich_paper_abstract(db, paper.id, http_client=client)

        assert enriched is not None
        assert enriched.abstract == "The complete abstract."
        assert enriched.abstract_source == "crossref"
        assert enriched.abstract_status == "available"


def test_publisher_dc_description_is_used_when_crossref_has_no_abstract(app_client):
    _, session_factory, models = app_client

    def publisher(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(404, request=request)
        return httpx.Response(
            200,
            request=request,
            text='<html><meta name="dc.description" content="The publisher abstract."></html>',
        )

    with session_factory() as db, httpx.Client(
        transport=httpx.MockTransport(publisher), follow_redirects=True
    ) as client:
        paper = _paper(models, "Publisher title")
        paper.abstract_source = "journal"
        paper.doi = "10.1234/example"
        paper.canonical_url = "https://publisher.example/article"
        db.add(paper)
        db.commit()

        enriched = enrich_paper_abstract(db, paper.id, http_client=client)

        assert enriched is not None
        assert enriched.abstract == "The publisher abstract."
        assert enriched.abstract_source == "citation-meta"
