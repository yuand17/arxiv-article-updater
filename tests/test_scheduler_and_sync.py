from datetime import UTC, datetime

from sqlalchemy import select

from arxiv_updater.arxiv_schedule import next_arxiv_update_at
from arxiv_updater.scheduler import _set_next_due, run_source_update
from arxiv_updater.services import sync as sync_module
from arxiv_updater.sources.base import PaperCandidate
from arxiv_updater.sources.scholar import ScholarAdapter
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
