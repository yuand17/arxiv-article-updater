from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from alembic import command
from arxiv_updater.auth import create_user
from arxiv_updater.config import get_settings


def test_initial_migration_has_no_schema_drift(tmp_path, monkeypatch):
    database_path = tmp_path / "migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    try:
        command.upgrade(config, "head")
        command.check(config)
        tables = set(inspect(create_engine(f"sqlite:///{database_path.as_posix()}")).get_table_names())
        assert {"papers", "users", "paper_summaries", "journal_subscriptions"} <= tables
        command.downgrade(config, "base")
    finally:
        get_settings.cache_clear()


def test_admin_can_add_only_https_journal_feeds(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        create_user(
            db,
            "feeds-admin@example.com",
            "a-strong-password",
            "Feed Admin",
            models.UserRole.ADMIN,
        )
    client.post(
        "/login",
        data={"email": "feeds-admin@example.com", "password": "a-strong-password"},
    )

    response = client.post(
        "/admin/journals",
        data={"name": "Unsafe", "feed_url": "http://localhost/feed", "issn": ""},
    )
    assert response.status_code == 200
    assert "必须是公开的 HTTPS URL" in response.text

    response = client.post(
        "/admin/journals",
        data={
            "name": "Example Physics",
            "feed_url": "https://journals.example.org/physics.atom",
            "issn": "1234-5678",
        },
    )
    assert response.status_code == 200
    assert "Example Physics" in response.text
    with session_factory() as db:
        feed = db.scalar(
            select(models.JournalSubscription).where(
                models.JournalSubscription.name == "Example Physics"
            )
        )
        assert feed is not None
        assert feed.issn == "1234-5678"
