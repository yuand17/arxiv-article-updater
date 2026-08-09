"""Deterministic paper retention and lightweight seen-item preservation."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..db import backup_sqlite_database, sqlite_database_path
from ..models import (
    CleanupRun,
    Interaction,
    Paper,
    PaperSource,
    RecommendationBatch,
    RecommendationItem,
    SeenSourceItem,
    SyncRun,
    utcnow,
)

UNINTERACTED_RETENTION_DAYS = 9
PROTECTED_BATCH_COUNT = 3
FULL_BATCH_RETENTION_DAYS = 30
SYNC_LOG_RETENTION_DAYS = 180
logger = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ensure_recent_backup(now: datetime) -> Path | None:
    database_path = sqlite_database_path()
    if database_path is None or not database_path.is_file():
        return None
    backup_dir = database_path.parent / "backups"
    recent_cutoff = now.timestamp() - 3 * 86400
    if backup_dir.is_dir() and any(
        path.is_file() and path.stat().st_mtime >= recent_cutoff
        for path in backup_dir.glob(f"{database_path.stem}.*.bak")
    ):
        return None
    return backup_sqlite_database(label="pre-cleanup")


def _protected_paper_ids(db: Session) -> set[str]:
    protected = set(db.scalars(select(Interaction.paper_id)).all())
    batch_ids = db.scalars(
        select(RecommendationBatch.id)
        .where(RecommendationBatch.status == "success")
        .order_by(RecommendationBatch.generated_at.desc())
        .limit(PROTECTED_BATCH_COUNT)
    ).all()
    if batch_ids:
        protected.update(
            db.scalars(
                select(RecommendationItem.paper_id).where(
                    RecommendationItem.batch_id.in_(batch_ids)
                )
            ).all()
        )
    return protected


def run_retention_cleanup(
    db: Session,
    *,
    now: datetime | None = None,
) -> CleanupRun:
    """Delete only old, uninteracted papers while preserving source identities."""

    now = now or utcnow()
    _ensure_recent_backup(now)
    run = CleanupRun(started_at=now, status="running")
    db.add(run)
    db.commit()
    try:
        before = int(db.scalar(select(func.count()).select_from(Paper)) or 0)
        protected = _protected_paper_ids(db)
        cutoff = now - timedelta(days=UNINTERACTED_RETENTION_DAYS)
        candidates = db.scalars(
            select(Paper).where(Paper.discovered_at < cutoff).order_by(Paper.discovered_at)
        ).all()
        delete_ids = [paper.id for paper in candidates if paper.id not in protected]
        sources = (
            db.scalars(select(PaperSource).where(PaperSource.paper_id.in_(delete_ids))).all()
            if delete_ids
            else []
        )
        paper_dois = {paper.id: paper.doi for paper in candidates if paper.id in delete_ids}
        for source in sources:
            seen = db.scalar(
                select(SeenSourceItem).where(
                    SeenSourceItem.source == source.source,
                    SeenSourceItem.external_id == source.external_id,
                )
            )
            if seen is None:
                seen = SeenSourceItem(
                    source=source.source,
                    external_id=source.external_id,
                    doi=paper_dois.get(source.paper_id),
                    first_seen_at=source.first_seen_at,
                    outcome="cleaned",
                )
                db.add(seen)
            seen.last_seen_at = now
            seen.outcome = "cleaned"
            seen.reason = "expired_uninteracted_paper"
            seen.paper_id = None
        if delete_ids:
            db.execute(
                delete(Paper)
                .where(Paper.id.in_(delete_ids))
                .execution_options(synchronize_session=False)
            )
        db.execute(
            delete(RecommendationBatch)
            .where(
                RecommendationBatch.generated_at
                < now - timedelta(days=FULL_BATCH_RETENTION_DAYS)
            )
            .execution_options(synchronize_session=False)
        )
        db.execute(
            delete(SyncRun)
            .where(SyncRun.started_at < now - timedelta(days=SYNC_LOG_RETENTION_DAYS))
            .execution_options(synchronize_session=False)
        )
        after = int(db.scalar(select(func.count()).select_from(Paper)) or 0)
        current = db.get(CleanupRun, run.id) or run
        current.finished_at = utcnow()
        current.status = "success"
        current.scanned_count = len(candidates)
        current.deleted_count = len(delete_ids)
        current.protected_count = len(candidates) - len(delete_ids)
        current.papers_before = before
        current.papers_after = after
        db.commit()
        current_id = current.id
        db.expire_all()
        return db.get(CleanupRun, current_id) or current
    except Exception as exc:
        logger.exception("Retention cleanup failed")
        db.rollback()
        current = db.get(CleanupRun, run.id) or run
        current.finished_at = utcnow()
        current.status = "failed"
        current.error = f"{type(exc).__name__}: {exc}"[:2000]
        db.add(current)
        db.commit()
        return current
