from datetime import datetime

from arxiv_updater.services import sync as sync_module


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
