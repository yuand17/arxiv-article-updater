from datetime import UTC, datetime

from arxiv_updater.arxiv_schedule import next_arxiv_update_at
from arxiv_updater.scheduler import _set_next_due
from arxiv_updater.services import sync as sync_module
from arxiv_updater.sources.scholar import ScholarAdapter


class _EmptyAdapter:
    def __init__(self) -> None:
        self.since = None

    def fetch(self, since):
        self.since = since
        return []


def test_next_arxiv_update_tracks_official_weekdays_and_daylight_saving():
    summer = next_arxiv_update_at(datetime(2026, 8, 9, 23, 0, tzinfo=UTC))
    weekend = next_arxiv_update_at(datetime(2026, 8, 14, 0, 11, tzinfo=UTC))
    winter = next_arxiv_update_at(datetime(2026, 1, 11, 23, 0, tzinfo=UTC))

    assert summer == datetime(2026, 8, 10, 0, 10, tzinfo=UTC)
    assert weekend == datetime(2026, 8, 17, 0, 10, tzinfo=UTC)
    assert winter == datetime(2026, 1, 12, 1, 10, tzinfo=UTC)


def test_successful_arxiv_update_uses_announcement_schedule(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        schedule = models.SourceSchedule(source="arxiv", enabled=True, interval_days=1)
        db.add(schedule)
        _set_next_due(schedule, now=datetime(2026, 8, 14, 0, 11), succeeded=True)
        db.commit()

    with session_factory() as db:
        schedule = db.get(models.SourceSchedule, "arxiv")
        assert schedule is not None
        assert schedule.next_due_at == datetime(2026, 8, 17, 0, 10)


def test_arxiv_rate_limit_retries_in_thirty_minutes(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        schedule = models.SourceSchedule(source="arxiv", enabled=True, interval_days=1)
        db.add(schedule)
        _set_next_due(
            schedule,
            now=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
            succeeded=False,
            error="HTTPStatusError: 429",
        )
        db.commit()

    with session_factory() as db:
        schedule = db.get(models.SourceSchedule, "arxiv")
        assert schedule is not None
        assert schedule.next_due_at == datetime(2026, 8, 9, 10, 30)


def test_sync_normalizes_legacy_naive_sqlite_run_timestamp(app_client, monkeypatch):
    _, session_factory, models = app_client
    adapter = _EmptyAdapter()
    monkeypatch.setattr(sync_module, "_build_adapter", lambda db, name: adapter)
    with session_factory() as db:
        db.add(
            models.SyncRun(
                source="arxiv",
                status=models.SyncStatus.SUCCESS,
                finished_at=datetime(2026, 8, 1, 12, 0, 0),
            )
        )
        db.commit()
        run = sync_module.sync_sources(db, "arxiv")[0]
        assert run.status == models.SyncStatus.SUCCESS
        assert adapter.since is not None
        assert adapter.since.tzinfo is not None


class _ScholarCitationAdapter(ScholarAdapter):
    def __init__(self) -> None:
        self.author_ids = ["author1234"]
        self.author_names = {"author1234": "Cited Researcher"}
        self.author_citation_counts = {"author1234": 9876}

    def fetch(self, since):
        return []


def test_scholar_sync_updates_author_citation_count(app_client, monkeypatch):
    _, session_factory, models = app_client
    adapter = _ScholarCitationAdapter()
    monkeypatch.setattr(sync_module, "_build_adapter", lambda db, name: adapter)
    monkeypatch.setattr(
        "arxiv_updater.services.abstracts.enrich_missing_scholar_abstracts",
        lambda db: None,
    )
    with session_factory() as db:
        author = models.TrackedAuthor(
            scholar_author_id="author1234",
            name="Old name",
            profile_url="https://scholar.google.com/citations?user=author1234",
        )
        db.add(author)
        db.commit()

        run = sync_module.sync_sources(db, "scholar")[0]
        db.refresh(author)

        assert run.status == models.SyncStatus.SUCCESS
        assert author.name == "Cited Researcher"
        assert author.citation_count == 9876
        assert author.citation_count_updated_at is not None
