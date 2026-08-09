import sqlite3

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
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM papers").scalar_one() == 1
            assert connection.exec_driver_sql("SELECT COUNT(*) FROM interactions").scalar_one() == 2
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


def test_local_settings_can_add_only_public_https_journal_feeds(app_client):
    client, session_factory, models = app_client
    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "更新与外部服务" in settings_response.text
    assert "成员邀请" not in settings_response.text

    response = client.post(
        "/settings/journals",
        data={"name": "Unsafe", "feed_url": "http://localhost/feed", "issn": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "journal_error=https" in response.headers["location"]

    response = client.post(
        "/settings/journals",
        data={
            "name": "Example Physics",
            "feed_url": "https://journals.example.org/physics.atom",
            "issn": "1234-5678",
        },
    )
    assert response.status_code == 200
    with session_factory() as db:
        feed = db.scalar(
            select(models.JournalSubscription).where(
                models.JournalSubscription.name == "Example Physics"
            )
        )
        assert feed is not None
        assert feed.issn == "1234-5678"
