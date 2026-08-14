from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from arxiv_updater import scheduler as scheduler_module
from arxiv_updater.arxiv_schedule import next_arxiv_update_at
from arxiv_updater.config import Settings
from arxiv_updater.scheduler import _set_next_due, ensure_source_schedules, run_source_update
from arxiv_updater.services import sync as sync_module
from arxiv_updater.sources.base import PaperCandidate
from arxiv_updater.sources.scholar import ScholarAdapter, SerpApiAccountUsage
from arxiv_updater.sources.scirate import SciRateAdapter, SciRateRecord


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


@pytest.mark.parametrize("source", ["arxiv", "journals"])
def test_fixed_source_schedule_is_always_enabled_and_daily(app_client, source):
    _, session_factory, models = app_client
    with session_factory() as db:
        ensure_source_schedules(db)
        schedule = db.get(models.SourceSchedule, source)
        assert schedule is not None
        schedule.enabled = False
        schedule.interval_days = 7
        db.commit()
        ensure_source_schedules(db)
        db.refresh(schedule)
        assert schedule.enabled is True
        assert schedule.interval_days == 1


def test_journal_sync_skips_only_the_individually_disabled_journal(
    app_client, monkeypatch
):
    client, session_factory, models = app_client
    client.get("/settings")
    fetched: list[str] = []

    def record_fetch(adapter, since):
        fetched.append(adapter.feeds[0].name)
        return []

    monkeypatch.setattr(sync_module.JournalAdapter, "fetch", record_fetch)
    with session_factory() as db:
        science = db.scalar(
            select(models.JournalSubscription).where(
                models.JournalSubscription.name == "Science"
            )
        )
        assert science is not None
        science.is_active = False
        db.commit()
        seen, created, errors = sync_module._sync_journals(db)

    assert (seen, created, errors) == (0, 0, [])
    assert "Science" not in fetched
    assert fetched == [
        "Nature",
        "Nature Physics",
        "Nature Communications",
        "Science Advances",
        "Physical Review Letters",
        "Physical Review X",
        "PRX Quantum",
    ]


def test_scholar_sync_fails_before_partial_update_when_live_quota_is_insufficient(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    monkeypatch.setattr(
        ScholarAdapter,
        "fetch_account_usage",
        lambda self: SerpApiAccountUsage(250, 249, 1),
    )
    with session_factory() as db:
        db.add_all(
            [
                models.TrackedAuthor(
                    scholar_author_id=f"author{index:04d}",
                    name=f"Author {index}",
                    profile_url=f"https://scholar.google.com/citations?user=author{index:04d}",
                )
                for index in range(2)
            ]
        )
        db.commit()
        with pytest.raises(RuntimeError, match="月度额度不足"):
            sync_module._build_adapter(db, "scholar")


def test_scholar_sync_uses_live_quota_instead_of_inflated_legacy_usage(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    monkeypatch.setattr(
        ScholarAdapter,
        "fetch_account_usage",
        lambda self: SerpApiAccountUsage(250, 46, 204),
    )
    with session_factory() as db:
        db.add_all(
            [
                models.TrackedAuthor(
                    scholar_author_id=f"author{index:04d}",
                    name=f"Author {index}",
                    profile_url=f"https://scholar.google.com/citations?user=author{index:04d}",
                )
                for index in range(21)
            ]
        )
        db.add(
            models.ApiUsage(
                service="serpapi",
                operation="author_sync",
                request_count=224,
            )
        )
        db.commit()

        adapter = sync_module._build_adapter(db, "scholar")

        assert isinstance(adapter, ScholarAdapter)
        assert adapter.account_usage_before == SerpApiAccountUsage(250, 46, 204)


def test_scholar_sync_is_skipped_without_an_enabled_serpapi_key(app_client, monkeypatch):
    _, session_factory, models = app_client
    monkeypatch.setattr(
        sync_module,
        "get_settings",
        lambda: Settings(serpapi_api_key=""),
    )

    with session_factory() as db:
        run = sync_module.sync_sources(db, "scholar")[0]

        assert run.status == models.SyncStatus.SKIPPED
        assert run.items_seen == 0
        assert "SerpAPI 未启用" in (run.error or "")
        assert db.query(models.ApiUsage).filter_by(service="serpapi").count() == 0


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


def test_source_update_only_enables_browser_challenge_when_explicit(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    observed: list[bool] = []

    def fake_sync(db, source, *, allow_browser_challenge=False):
        observed.append(allow_browser_challenge)
        return [models.SyncRun(source=source, status=models.SyncStatus.SUCCESS)]

    monkeypatch.setattr(sync_module, "sync_sources", fake_sync)
    with session_factory() as db:
        assert run_source_update(db, "scirate", allow_browser_challenge=True)
        assert run_source_update(db, "scirate")

    assert observed == [True, False]


def test_one_click_update_runs_all_sources_and_records_aggregate(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    calls: list[tuple[str, bool]] = []

    def fake_source_update(db, source, *, now=None, allow_browser_challenge=False):
        calls.append((source, allow_browser_challenge))
        status = models.SyncStatus.SKIPPED if source == "scholar" else models.SyncStatus.SUCCESS
        run = models.SyncRun(
            source=source,
            status=status,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            items_seen=10,
            items_created=1,
            error="SerpAPI 未启用" if source == "scholar" else "",
        )
        db.add(run)
        db.commit()
        return status == models.SyncStatus.SUCCESS

    monkeypatch.setattr(scheduler_module, "SessionLocal", session_factory)
    monkeypatch.setattr(scheduler_module, "run_source_update", fake_source_update)

    scheduler_module.run_all_source_updates_in_background()

    assert calls == [
        ("arxiv", False),
        ("scirate", True),
        ("scholar", False),
        ("journals", False),
    ]
    with session_factory() as db:
        aggregate = db.scalar(
            select(models.SyncRun)
            .where(models.SyncRun.source == "all")
            .order_by(models.SyncRun.started_at.desc())
        )
        assert aggregate is not None
        assert aggregate.status == models.SyncStatus.SUCCESS
        assert (aggregate.items_seen, aggregate.items_created) == (40, 4)
        assert aggregate.error == "已跳过未启用的来源：scholar"


def test_sync_normalizes_legacy_naive_sqlite_run_timestamp(app_client, monkeypatch):
    _, session_factory, models = app_client
    adapter = _EmptyAdapter()
    monkeypatch.setattr(sync_module, "_build_adapter", lambda db, name, **kwargs: adapter)
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
    monkeypatch.setattr(sync_module, "_build_adapter", lambda db, name, **kwargs: adapter)
    monkeypatch.setattr(
        sync_module,
        "get_settings",
        lambda: Settings(serpapi_api_key="test-serpapi-key"),
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


class _SciRateImportAdapter(SciRateAdapter):
    def __init__(self, records: list[SciRateRecord]) -> None:
        self.next_records = records
        self.records: list[SciRateRecord] = []

    def fetch(self, since):
        self.records = self.next_records
        return [
            PaperCandidate(
                source="scirate",
                external_id=record.arxiv_id,
                title=record.title,
                authors=record.authors,
                abstract=record.abstract,
                published_at=record.published_at,
                updated_at=record.published_at,
                arxiv_id=record.arxiv_id,
                categories=record.categories,
                canonical_url=f"https://arxiv.org/abs/{record.arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{record.arxiv_id}",
                metadata={
                    "scites_count": record.scites_count,
                    "rank": rank,
                    "range_days": 3,
                },
            )
            for rank, record in enumerate(self.records, start=1)
        ]


def test_scirate_sync_imports_ranked_papers_and_clears_old_hot_flag(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    records = [
        SciRateRecord(
            arxiv_id="2608.00001",
            scites_count=20,
            title="Top SciRate paper",
            authors=["Alice Example"],
            abstract="Top abstract",
            published_at=datetime(2026, 8, 8, tzinfo=UTC),
            categories=["quant-ph"],
        ),
        SciRateRecord(
            arxiv_id="2608.00002",
            scites_count=0,
            title="Fiftieth SciRate paper",
            authors=["Bob Example"],
            abstract="Fiftieth abstract",
            published_at=datetime(2026, 8, 8, tzinfo=UTC),
            categories=["quant-ph"],
        ),
    ]
    adapter = _SciRateImportAdapter(records)
    monkeypatch.setattr(sync_module, "_build_adapter", lambda db, name, **kwargs: adapter)

    with session_factory() as db:
        run = sync_module.sync_sources(db, "scirate")[0]
        papers = list(db.scalars(select(models.Paper).order_by(models.Paper.scites_count.desc())))
        assert run.status == models.SyncStatus.SUCCESS
        assert (run.items_seen, run.items_created) == (2, 2)
        assert [paper.scites_count for paper in papers] == [20, 0]
        assert all(paper.is_scirate_hot for paper in papers)
        assert all(source.source == "scirate" for paper in papers for source in paper.sources)
        assert papers[0].sources[0].metadata_json["rank"] == 1

        adapter.next_records = records[:1]
        second = sync_module.sync_sources(db, "scirate")[0]
        papers = list(db.scalars(select(models.Paper).order_by(models.Paper.scites_count.desc())))
        assert (second.items_seen, second.items_created) == (1, 0)
        assert papers[0].is_scirate_hot is True
        assert papers[1].is_scirate_hot is False
