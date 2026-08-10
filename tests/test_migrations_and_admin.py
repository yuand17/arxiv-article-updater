import json
import sqlite3
from datetime import UTC, datetime, timedelta

from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from alembic import command
from arxiv_updater.config import get_settings


def test_single_user_migration_preserves_library_and_removes_account_tables(tmp_path, monkeypatch):
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "0002")
        with sqlite3.connect(database_path) as conn:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, display_name, role, interests, "
                "is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "user-1",
                    "local@example.com",
                    "hash",
                    "Local",
                    "ADMIN",
                    "many body",
                    1,
                    "2026-01-01",
                ),
            )
            conn.execute(
                "INSERT INTO papers (id, title, normalized_title, abstract, authors_text, "
                "first_author, discovered_at, categories, scites_count, is_scirate_hot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "paper-1",
                    "Migration paper",
                    "migration paper",
                    "Original abstract",
                    "A Author",
                    "a author",
                    "2026-01-01",
                    "[]",
                    0,
                    0,
                ),
            )
            conn.executemany(
                "INSERT INTO interactions (id, user_id, paper_id, kind, weight, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    ("old-interest", "user-1", "paper-1", "INTERESTED", 3, "2026-01-01"),
                    ("keep-fulltext", "user-1", "paper-1", "FULLTEXT", 1, "2026-01-02"),
                    ("keep-dismissed", "user-1", "paper-1", "DISMISSED", -5, "2026-01-03"),
                ],
            )
            conn.execute(
                "INSERT INTO paper_summaries (id, paper_id, tldr, contributions, methods, model, "
                "prompt_version, input_tokens, output_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("summary-1", "paper-1", "old", "[]", "old", "old", "v4", 0, 0, "2026-01-01"),
            )
            conn.commit()
        command.upgrade(config, "head")
        engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        tables = set(inspect(engine).get_table_names())
        assert {"papers", "app_preferences", "source_schedules", "recommendation_batches"} <= tables
        assert not {"users", "invites", "author_follows", "paper_summaries"} & tables
        author_columns = {
            column["name"] for column in inspect(engine).get_columns("tracked_authors")
        }
        assert {"citation_count", "citation_count_updated_at"} <= author_columns
        paper_columns = {column["name"] for column in inspect(engine).get_columns("papers")}
        assert "semantic_scholar_id" not in paper_columns
        assert {"document_type", "is_original_research", "is_physics"} <= paper_columns
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM papers").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM interactions").scalar_one() == 2
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM journal_subscriptions"
                ).scalar_one()
                == 0
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM interactions WHERE kind = 'INTERESTED'"
                ).scalar_one()
                == 0
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT manual_interests FROM app_preferences"
                ).scalar_one()
                == "many body"
            )
            assert (
                connection.exec_driver_sql("SELECT COUNT(*) FROM source_schedules").scalar_one()
                == 4
            )
        command.downgrade(config, "0004")
        downgraded_tables = set(inspect(engine).get_table_names())
        downgraded_columns = {
            column["name"] for column in inspect(engine).get_columns("papers")
        }
        assert "seen_source_items" not in downgraded_tables
        assert "semantic_scholar_id" in downgraded_columns
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM papers").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM interactions").scalar_one() == 2
    finally:
        get_settings.cache_clear()


def test_settings_sorts_tracked_authors_by_citation_count(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        db.add_all(
            [
                models.TrackedAuthor(
                    scholar_author_id="lowcount1",
                    name="Low Count",
                    profile_url="https://scholar.google.com/citations?user=lowcount1",
                    citation_count=12,
                    citation_count_updated_at=models.utcnow(),
                ),
                models.TrackedAuthor(
                    scholar_author_id="highcount",
                    name="High Count",
                    profile_url="https://scholar.google.com/citations?user=highcount",
                    citation_count=340,
                    citation_count_updated_at=models.utcnow(),
                ),
            ]
        )
        db.commit()

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.index("High Count") < response.text.index("Low Count")
    assert "总引用 340" in response.text
    assert 'class="follow-list author-list"' in response.text


def test_local_settings_discovers_then_confirms_a_journal(app_client, monkeypatch):
    client, session_factory, models = app_client
    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "更新与外部服务" in settings_response.text
    assert "arXiv 官方时间表" in settings_response.text
    assert "美东时间周日至周四 20:00 发布" in settings_response.text
    assert "北京时间夏令时周一至周五 08:10" in settings_response.text
    assert "成员邀请" not in settings_response.text

    from arxiv_updater.services.journal_discovery import (
        DiscoveredEndpoint,
        JournalDiscoveryPreview,
        PreviewPaper,
    )

    preview = JournalDiscoveryPreview(
        token="preview-token",
        name="Example Physics",
        homepage_url="https://journals.example.org/physics",
        canonical_domain="journals.example.org",
        issn_online="1234-5678",
        issn_print="",
        scope_kind="physics",
        endpoints=[DiscoveredEndpoint("rss", "https://journals.example.org/rss", 10)],
        scanned_count=4,
        nonresearch_filtered=1,
        nonphysics_filtered=0,
        papers=[PreviewPaper("Quantum result", "A. Author", "2026-08-09")],
    )
    monkeypatch.setattr("arxiv_updater.web.discover_journal", lambda name, url: preview)
    monkeypatch.setattr(
        "arxiv_updater.scheduler.run_source_update_in_background", lambda source: None
    )

    response = client.post(
        "/settings/journals/discover",
        data={
            "name": "Example Physics",
            "homepage_url": "https://journals.example.org/physics",
        },
    )
    assert response.status_code == 200
    assert "Quantum result" in response.text
    response = client.post(
        "/settings/journals/confirm",
        data={"token": "preview-token"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as db:
        feed = db.scalar(
            select(models.JournalSubscription).where(
                models.JournalSubscription.name == "Example Physics"
            )
        )
        assert feed is not None
        assert feed.issn_online == "1234-5678"
        assert len(feed.endpoints) == 1

    second_preview = JournalDiscoveryPreview(
        token="second-preview-token",
        name="Example Quantum",
        homepage_url="https://journals.example.org/quantum",
        canonical_domain="journals.example.org",
        issn_online="2345-6789",
        issn_print="",
        scope_kind="physics",
        endpoints=[DiscoveredEndpoint("rss", "https://journals.example.org/quantum.rss", 10)],
        scanned_count=3,
        nonresearch_filtered=0,
        nonphysics_filtered=0,
        papers=[PreviewPaper("Second quantum result", "B. Author", "2026-08-10")],
    )
    monkeypatch.setattr(
        "arxiv_updater.web.discover_journal", lambda name, url: second_preview
    )
    response = client.post(
        "/settings/journals/discover",
        data={
            "name": "Example Quantum",
            "homepage_url": "https://journals.example.org/quantum",
        },
    )
    assert response.status_code == 200
    assert "Second quantum result" in response.text


def test_manual_scirate_sync_enables_human_chrome_assistance(app_client, monkeypatch):
    client, _, _ = app_client
    calls: list[tuple[str, bool]] = []

    def record_update(source: str, allow_browser_challenge: bool = False) -> None:
        calls.append((source, allow_browser_challenge))

    monkeypatch.setattr(
        "arxiv_updater.scheduler.run_source_update_in_background",
        record_update,
    )

    response = client.post("/settings/sync/scirate", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?sync_started=scirate&toast=sync_started"
    assert calls == [("scirate", True)]
    page = client.get(response.headers["location"])
    assert 'id="toast-region"' in page.text
    assert "app.js" in page.text


def test_htmx_manual_sync_exposes_completion_poll_and_failed_status(app_client, monkeypatch):
    client, session_factory, models = app_client
    monkeypatch.setattr(
        "arxiv_updater.scheduler.run_source_update_in_background",
        lambda source, allow_browser_challenge=False: None,
    )

    response = client.post("/settings/sync/arxiv", headers={"HX-Request": "true"})

    assert response.status_code == 204
    events = json.loads(response.headers["HX-Trigger"])
    assert events["app:sync-started"]["source"] == "arxiv"
    assert events["app:toast"]["level"] == "info"

    after = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory() as db:
        db.add(
            models.SyncRun(
                source="arxiv",
                status=models.SyncStatus.FAILED,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                error="simulated source failure",
            )
        )
        db.commit()
    status = client.get(
        "/settings/sync/arxiv/status",
        params={"after": after.replace(tzinfo=None).isoformat()},
    )

    assert status.status_code == 200
    assert status.json() == {
        "status": "failed",
        "message": "simulated source failure",
        "items_seen": 0,
        "items_created": 0,
    }
