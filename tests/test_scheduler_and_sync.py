from datetime import datetime

from arxiv_updater.services import sync as sync_module
from arxiv_updater.sources.scholar import ScholarAdapter


class _EmptyAdapter:
    def __init__(self) -> None:
        self.since = None

    def fetch(self, since):
        self.since = since
        return []


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
