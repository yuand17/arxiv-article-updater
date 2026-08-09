from datetime import UTC, datetime, timedelta

import pytest

from arxiv_updater.config import Settings
from arxiv_updater.services.interactions import record_interaction
from arxiv_updater.services.ranking import rank_papers
from arxiv_updater.services.recommendations import generate_recommendation_batch


def _paper(
    models,
    title: str,
    days_old: int,
    source: str,
    scites: int = 0,
    *,
    discovered_offset: int = 0,
):
    paper = models.Paper(
        title=title,
        normalized_title=title.lower(),
        abstract=f"Abstract about {title} and quantum information.",
        abstract_source=source,
        abstract_status="available",
        authors_text="Alice Example, Bob Example",
        first_author="alice example",
        published_at=datetime.now(UTC) - timedelta(days=days_old),
        discovered_at=datetime.now(UTC) - timedelta(days=days_old, seconds=discovered_offset),
        categories=["quant-ph"],
        scites_count=scites,
        is_scirate_hot=scites >= 5,
    )
    paper.sources.append(
        models.PaperSource(source=source, external_id=f"{source}-{title}", metadata_json={})
    )
    return paper


def test_all_updates_are_strictly_discovered_time_descending(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        earlier = _paper(models, "Earlier discovery", 2, "arxiv")
        later = _paper(models, "Later discovery", 2, "journal", discovered_offset=-60)
        db.add_all([earlier, later])
        db.commit()
        ranked = rank_papers(db, view="all", now=datetime.now(UTC))
        assert [item.paper.id for item in ranked] == [later.id, earlier.id]
        assert all(not item.reasons for item in ranked)


def test_featured_view_uses_persisted_batch_but_all_view_has_no_reasons(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        plain = _paper(models, "Plain preprint", 1, "arxiv")
        hot = _paper(models, "Hot journal quantum result", 1, "journal", scites=12)
        db.add_all([plain, hot])
        db.add(models.AppPreferences(id=1, manual_interests="quantum", featured_paper_count=2))
        db.commit()
        generate_recommendation_batch(db, settings=Settings(deepseek_api_key=""))
        featured = rank_papers(db, view="featured", now=datetime.now(UTC))
        all_items = rank_papers(db, view="all", now=datetime.now(UTC))
        assert featured
        assert all(featured_item.reasons for featured_item in featured)
        assert all(not item.reasons for item in all_items)


def test_scirate_view_is_sorted_by_vote_count(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        lower = _paper(models, "Lower SciRate paper", 1, "scirate", scites=9)
        higher = _paper(models, "Higher SciRate paper", 2, "scirate", scites=20)
        db.add_all([lower, higher])
        db.commit()

        ranked = rank_papers(db, view="scirate", now=datetime.now(UTC))

        assert [item.paper.id for item in ranked] == [higher.id, lower.id]


def test_single_reader_dismissal_hides_and_save_is_idempotent(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        paper = _paper(models, "Personalized visibility", 1, "arxiv")
        db.add(paper)
        db.commit()
        record_interaction(db, paper.id, models.InteractionKind.SAVED)
        record_interaction(db, paper.id, models.InteractionKind.SAVED)
        record_interaction(db, paper.id, models.InteractionKind.DISMISSED)
        assert rank_papers(db, view="all") == []
        assert len(paper.interactions) == 2


def test_interacted_old_paper_leaves_active_views_but_remains_in_saved_history(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        paper = _paper(models, "Old saved history", 12, "arxiv")
        db.add(paper)
        db.commit()
        record_interaction(db, paper.id, models.InteractionKind.SAVED)

        assert rank_papers(db, view="all", now=now) == []
        assert rank_papers(db, view="arxiv", now=now) == []
        assert [item.paper.id for item in rank_papers(db, view="saved", now=now)] == [
            paper.id
        ]


def test_feed_actions_need_no_login_and_show_raw_abstract(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        paper = _paper(models, "A polished quantum interface", 1, "arxiv")
        db.add(paper)
        db.commit()
        paper_id = paper.id
    response = client.get("/?view=all")
    assert response.status_code == 200
    assert "查看 Abstract" in response.text
    assert "感兴趣 · 查看总结" not in response.text

    response = client.post(f"/papers/{paper_id}/abstract")
    assert response.status_code == 200
    assert "Abstract about A polished quantum interface" in response.text
    response = client.post(f"/papers/{paper_id}/save")
    assert response.status_code == 200
    assert "已收藏" in response.text
    response = client.post(f"/papers/{paper_id}/dismiss")
    assert response.text == ""


def test_feed_groups_tracked_author_sources_into_one_counted_badge(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        paper = _paper(models, "A paper from two tracked authors", 1, "scholar")
        paper.sources[0].metadata_json = {"tracked_author_id": "author-one"}
        paper.sources.append(
            models.PaperSource(
                source="scholar",
                external_id="scholar-second-author",
                metadata_json={"tracked_author_id": "author-two"},
            )
        )
        db.add(paper)
        db.commit()

    response = client.get("/?view=all")
    assert response.status_code == 200
    assert response.text.count(">重点作者 2</span>") == 1
    assert response.text.count(">重点作者</span>") == 0


def test_all_updates_loads_more_than_three_hundred_in_pages_of_one_hundred(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        db.add_all([_paper(models, f"Library paper {index}", 1, "arxiv") for index in range(301)])
        db.commit()

    first = client.get("/?view=all")
    second = client.get("/papers?view=all&offset=100")
    third = client.get("/papers?view=all&offset=200")
    assert first.status_code == second.status_code == third.status_code == 200
    assert first.text.count('class="paper-card"') == 100
    assert second.text.count('class="paper-card"') == 100
    assert third.text.count('class="paper-card"') == 100
    assert "hx-swap-oob" in second.text


@pytest.mark.parametrize(
    ("view", "source"),
    [("authors", "scholar"), ("arxiv", "arxiv"), ("journals", "journal")],
)
def test_each_source_view_has_stable_hundred_item_pagination(app_client, view, source):
    client, session_factory, models = app_client
    with session_factory() as db:
        db.add_all(
            [_paper(models, f"{view} paper {index}", 1, source) for index in range(101)]
        )
        db.commit()

    first = client.get(f"/?view={view}")
    second = client.get(f"/papers?view={view}&offset=100")

    assert first.status_code == second.status_code == 200
    assert first.text.count('class="paper-card"') == 100
    assert second.text.count('class="paper-card"') == 1
