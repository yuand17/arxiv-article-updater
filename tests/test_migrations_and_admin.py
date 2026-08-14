import json
import sqlite3
from datetime import UTC, datetime, timedelta

from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from alembic import command
from arxiv_updater.config import get_settings
from arxiv_updater.db import alembic_config_path


def test_alembic_config_is_available_to_the_runtime():
    config_path = alembic_config_path()

    assert config_path.is_file()
    assert (config_path.parent / "alembic" / "env.py").is_file()


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
                == 8
            )
            assert (
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM journal_endpoints"
                ).scalar_one()
                == 16
            )
            assert connection.exec_driver_sql(
                "SELECT group_concat(name, '|') FROM "
                "(SELECT name FROM journal_subscriptions ORDER BY name)"
            ).scalar_one() == (
                "Nature|Nature Communications|Nature Physics|PRX Quantum|"
                "Physical Review Letters|Physical Review X|Science|Science Advances"
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


def test_crossref_endpoint_migration_reuses_legacy_subscription_id(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy-journal-migration.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path.as_posix()}")
    get_settings.cache_clear()
    config = Config("alembic.ini")
    legacy_id = "legacy-nature-physics-id"
    try:
        command.upgrade(config, "0005")
        with sqlite3.connect(database_path) as conn:
            conn.execute(
                "INSERT INTO journal_subscriptions "
                "(id, name, homepage_url, canonical_domain, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    legacy_id,
                    "Nature Physics",
                    "https://legacy.example/nphys",
                    "legacy.example",
                    "2026-01-01",
                ),
            )
            conn.commit()

        command.upgrade(config, "head")

        with sqlite3.connect(database_path) as conn:
            endpoint_owner = conn.execute(
                "SELECT journal_subscription_id FROM journal_endpoints WHERE url = ?",
                ("https://api.crossref.org/journals/1745-2481/works",),
            ).fetchone()
            assert endpoint_owner == (legacy_id,)
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


def test_local_settings_seeds_fixed_journals_and_toggles_each_independently(app_client):
    client, session_factory, models = app_client
    settings_response = client.get("/settings")
    assert settings_response.status_code == 200
    assert "可选 API 服务" in settings_response.text
    assert "来源更新计划" in settings_response.text
    assert "arXiv 官方时间表" in settings_response.text
    assert "美东时间周日至周四 20:00 发布" in settings_response.text
    assert "北京时间夏令时周一至周五 08:10" in settings_response.text
    assert "成员邀请" not in settings_response.text
    assert "查找期刊" not in settings_response.text
    assert "期刊官网" not in settings_response.text
    assert settings_response.text.count('data-auto-submit aria-label="订阅 ') == 8
    assert 'onchange="this.form.requestSubmit()"' not in settings_response.text
    arxiv_card = settings_response.text.split('id="schedule-arxiv"', 1)[1].split(
        "</section>", 1
    )[0]
    journal_card = settings_response.text.split('id="schedule-journals"', 1)[1].split(
        "</section>", 1
    )[0]
    assert 'name="enabled"' not in arxiv_card
    assert "保存开关" not in arxiv_card
    assert "每天更新" not in journal_card
    names = [
        "Nature",
        "Nature Physics",
        "Nature Communications",
        "Science",
        "Science Advances",
        "Physical Review Letters",
        "Physical Review X",
        "PRX Quantum",
    ]
    for name in names:
        assert name in settings_response.text

    with session_factory() as db:
        feeds = list(db.scalars(select(models.JournalSubscription)))
        assert len(feeds) == 8
        assert all(feed.is_active for feed in feeds)
        assert all(
            {endpoint.kind for endpoint in feed.endpoints} == {"rss", "crossref"}
            for feed in feeds
        )
        science = next(feed for feed in feeds if feed.name == "Science")
        science_id = science.id

    response = client.post(
        f"/settings/journals/{science_id}/toggle",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert json.loads(response.headers["HX-Trigger"])["app:toast"]["title"] == (
        "期刊订阅已更新"
    )
    with session_factory() as db:
        science = db.get(models.JournalSubscription, science_id)
        assert science is not None and science.is_active is False
        assert all(
            feed.is_active
            for feed in db.scalars(
                select(models.JournalSubscription).where(
                    models.JournalSubscription.id != science_id
                )
            )
        )

    response = client.post(
        f"/settings/journals/{science_id}/toggle",
        data={"enabled": "on"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/settings?toast=journal_subscription_saved"
    )
    with session_factory() as db:
        science = db.get(models.JournalSubscription, science_id)
        assert science is not None and science.is_active is True


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


def test_one_click_update_starts_all_four_sources(app_client, monkeypatch):
    client, _, _ = app_client
    calls: list[str] = []
    monkeypatch.setattr(
        "arxiv_updater.scheduler.run_all_source_updates_in_background",
        lambda: calls.append("all"),
    )

    page = client.get("/settings")
    assert "一键更新四个来源" in page.text

    response = client.post("/settings/sync/all", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/settings?sync_started=all&toast=sync_started"
    assert calls == ["all"]


def test_all_source_status_uses_aggregate_run(app_client):
    client, session_factory, models = app_client
    after = datetime.now(UTC) - timedelta(seconds=1)
    with session_factory() as db:
        db.add(
            models.SyncRun(
                source="all",
                status=models.SyncStatus.SUCCESS,
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                items_seen=42,
                items_created=7,
            )
        )
        db.commit()

    response = client.get(
        "/settings/sync/all/status",
        params={"after": after.replace(tzinfo=None).isoformat()},
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "message": "",
        "items_seen": 42,
        "items_created": 7,
    }


def test_settings_hides_legacy_serpapi_usage_estimates(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        db.add_all(
            [
                models.ApiUsage(
                    service="serpapi",
                    operation="author_sync",
                    request_count=224,
                ),
                models.ApiUsage(
                    service="serpapi",
                    operation="author_sync_billed",
                    request_count=2,
                ),
            ]
        )
        db.commit()

    page = client.get("/settings")

    assert page.status_code == 200
    assert "<strong>serpapi</strong> 2 requests" in page.text
    assert "<td>author_sync</td>" not in page.text
    assert "<td>author_sync_billed</td>" in page.text


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
        "message": "来源更新失败；已保留上次成功数据，请稍后重试。",
        "items_seen": 0,
        "items_created": 0,
    }
