from datetime import UTC, datetime, timedelta

from arxiv_updater.auth import create_user
from arxiv_updater.services.interactions import record_interaction
from arxiv_updater.services.ranking import rank_papers


def _paper(models, title: str, days_old: int, source: str, scites: int = 0):
    paper = models.Paper(
        title=title,
        normalized_title=title.lower(),
        abstract=f"Abstract about {title} and quantum information.",
        authors_text="Alice Example, Bob Example",
        first_author="alice example",
        published_at=datetime.now(UTC) - timedelta(days=days_old),
        categories=["quant-ph"],
        scites_count=scites,
        is_scirate_hot=scites >= 5,
    )
    paper.sources.append(
        models.PaperSource(source=source, external_id=f"{source}-{title}", metadata_json={})
    )
    return paper


def test_ranking_rewards_journal_and_scirate(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        user = create_user(db, "rank@example.com", "a-strong-password", "Ranker")
        plain = _paper(models, "Plain preprint", 1, "arxiv")
        hot = _paper(models, "Hot journal quantum result", 1, "journal", scites=12)
        db.add_all([plain, hot])
        db.commit()
        ranked = rank_papers(db, user, view="all", now=datetime.now(UTC))
        assert ranked[0].paper.id == hot.id
        assert "重点期刊" in ranked[0].reasons


def test_dismissal_hides_only_for_current_user(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        first = create_user(db, "first@example.com", "a-strong-password", "First")
        second = create_user(db, "second@example.com", "a-strong-password", "Second")
        paper = _paper(models, "Personalized visibility", 1, "arxiv")
        db.add(paper)
        db.commit()
        record_interaction(db, first.id, paper.id, models.InteractionKind.DISMISSED)
        assert rank_papers(db, first, view="all") == []
        assert rank_papers(db, second, view="all")[0].paper.id == paper.id


def test_authenticated_feed_and_actions(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        create_user(db, "ui@example.com", "a-strong-password", "UI Reader")
        paper = _paper(models, "A polished quantum interface", 1, "arxiv")
        db.add(paper)
        db.commit()
        paper_id = paper.id
    client.post("/login", data={"email": "ui@example.com", "password": "a-strong-password"})
    response = client.get("/?view=all")
    assert response.status_code == 200
    assert "A polished quantum interface" in response.text
    assert "感兴趣 · 查看总结" in response.text

    response = client.post(f"/papers/{paper_id}/save")
    assert response.status_code == 200
    assert "已收藏" in response.text
    response = client.post(f"/papers/{paper_id}/dismiss")
    assert response.text == ""

