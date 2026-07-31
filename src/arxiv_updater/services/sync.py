from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import SyncRun, SyncStatus, utcnow
from ..sources.arxiv import ArxivAdapter
from .papers import upsert_paper


def sync_sources(db: Session, source: str = "all") -> list[SyncRun]:
    sources = ["arxiv", "scholar", "scirate", "journals"] if source == "all" else [source]
    runs: list[SyncRun] = []
    for name in sources:
        run = SyncRun(source=name, status=SyncStatus.RUNNING)
        db.add(run)
        db.commit()
        if name != "arxiv":
            run.status = SyncStatus.SKIPPED
            run.error = "adapter not installed yet"
            run.finished_at = utcnow()
            db.commit()
            runs.append(run)
            continue
        previous = db.scalar(
            select(SyncRun)
            .where(SyncRun.source == name, SyncRun.status == SyncStatus.SUCCESS)
            .order_by(SyncRun.finished_at.desc())
        )
        since = (
            previous.finished_at - timedelta(days=1)
            if previous and previous.finished_at
            else datetime.now(UTC) - timedelta(days=14)
        )
        try:
            candidates = ArxivAdapter().fetch(since)
            created = 0
            for candidate in candidates:
                created += int(upsert_paper(db, candidate).created)
            run.items_seen = len(candidates)
            run.items_created = created
            run.status = SyncStatus.SUCCESS
        except Exception as exc:
            db.rollback()
            run = db.get(SyncRun, run.id) or run
            run.status = SyncStatus.FAILED
            run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.finished_at = utcnow()
        db.add(run)
        db.commit()
        runs.append(run)
    return runs


def scheduled_sync(source: str = "all") -> None:
    with SessionLocal() as db:
        sync_sources(db, source)
