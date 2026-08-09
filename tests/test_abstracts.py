from datetime import UTC, datetime

from arxiv_updater.services.abstracts import enrich_paper_abstract


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
