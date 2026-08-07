from datetime import UTC, datetime, timedelta

from arxiv_updater.services.interactions import record_interaction
from arxiv_updater.services.ranking import rank_papers


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


def test_weekly_fallback_can_use_source_signals_but_all_view_cannot(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        plain = _paper(models, "Plain preprint", 1, "arxiv")
        hot = _paper(models, "Hot journal quantum result", 1, "journal", scites=12)
        db.add_all([plain, hot])
        db.commit()
        weekly = rank_papers(db, view="weekly", now=datetime.now(UTC))
        assert weekly[0].paper.id == hot.id
        assert "重点期刊" in weekly[0].reasons


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
