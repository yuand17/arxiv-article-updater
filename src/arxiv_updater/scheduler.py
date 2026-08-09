"""Embedded, per-source scheduler for the local single-user application."""

import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from .arxiv_schedule import next_arxiv_update_at
from .datetime_utils import as_utc
from .db import SessionLocal
from .models import SourceSchedule, SyncStatus, utcnow

DEFAULT_SOURCE_INTERVALS = {
    "arxiv": 1,
    "scirate": 3,
    "scholar": 7,
    "journals": 7,
}
RETRY_DELAY = timedelta(hours=6)
RATE_LIMIT_RETRY_DELAY = timedelta(minutes=30)
CHECK_INTERVAL_MINUTES = 5
_source_locks = {name: threading.Lock() for name in DEFAULT_SOURCE_INTERVALS}


def _aware(value: datetime | None) -> datetime | None:
    return as_utc(value)


def ensure_source_schedules(db: Session, *, now: datetime | None = None) -> None:
    now = as_utc(now or utcnow())
    assert now is not None
    created = False
    for source, interval_days in DEFAULT_SOURCE_INTERVALS.items():
        if db.get(SourceSchedule, source) is None:
            db.add(
                SourceSchedule(
                    source=source,
                    enabled=True,
                    interval_days=interval_days,
                    next_due_at=now,
                )
            )
            created = True
    if created:
        db.commit()


def _set_next_due(
    schedule: SourceSchedule,
    *,
    now: datetime,
    succeeded: bool,
    error: str = "",
) -> None:
    aware_now = as_utc(now)
    assert aware_now is not None
    if succeeded:
        schedule.last_success_at = aware_now
        schedule.next_due_at = (
            next_arxiv_update_at(aware_now)
            if schedule.source == "arxiv"
            else aware_now + timedelta(days=schedule.interval_days)
        )
        schedule.last_error = ""
    else:
        schedule.next_due_at = aware_now + (
            RATE_LIMIT_RETRY_DELAY if "429" in error else RETRY_DELAY
        )


def run_source_update(
    db: Session,
    source: str,
    *,
    now: datetime | None = None,
    allow_browser_challenge: bool = False,
) -> bool:
    """Run one source under a process lock and write its independent schedule state."""

    if source not in DEFAULT_SOURCE_INTERVALS:
        raise ValueError(f"Unknown source: {source}")
    ensure_source_schedules(db)
    lock = _source_locks[source]
    if not lock.acquire(blocking=False):
        return False
    try:
        schedule = db.get(SourceSchedule, source)
        if schedule is None:
            return False
        now = as_utc(now or utcnow())
        assert now is not None
        schedule.last_attempt_at = now
        db.commit()
        from .services.sync import sync_sources

        run = sync_sources(
            db,
            source,
            allow_browser_challenge=allow_browser_challenge and source == "scirate",
        )[0]
        succeeded = run.status == SyncStatus.SUCCESS
        current = db.get(SourceSchedule, source) or schedule
        _set_next_due(current, now=utcnow(), succeeded=succeeded, error=run.error or "")
        if not succeeded:
            current.last_error = run.error or "同步失败"
        db.commit()
        return succeeded
    finally:
        lock.release()


def run_due_jobs() -> None:
    """Run overdue sources, then regenerate due profiles and recommendation batches."""

    with SessionLocal() as db:
        now = utcnow()
        ensure_source_schedules(db, now=now)
        schedules = db.scalars(
            select(SourceSchedule)
            .where(SourceSchedule.enabled.is_(True))
            .order_by(SourceSchedule.next_due_at.asc().nullsfirst())
        ).all()
        for schedule in schedules:
            next_due = _aware(schedule.next_due_at)
            if next_due is None or next_due <= now:
                run_source_update(db, schedule.source, now=now)

        from .services.preferences import (
            PreferenceUnavailableError,
            get_preferences,
            profile_is_due,
            rebuild_preference_profile,
        )
        from .services.recommendations import generate_recommendation_batch, recommendation_is_due

        preferences = get_preferences(db)
        if profile_is_due(preferences, now=now):
            try:
                rebuild_preference_profile(db, now=now)
            except PreferenceUnavailableError:
                db.rollback()
        if recommendation_is_due(db, now=now):
            generate_recommendation_batch(db, now=now)


def run_source_update_in_background(
    source: str, allow_browser_challenge: bool = False
) -> None:
    with SessionLocal() as db:
        run_source_update(
            db,
            source,
            allow_browser_challenge=allow_browser_challenge,
        )


def configure_scheduler(scheduler: BaseScheduler) -> None:
    scheduler.add_job(
        run_due_jobs,
        "interval",
        minutes=CHECK_INTERVAL_MINUTES,
        id="due-source-and-recommendation-check",
        replace_existing=True,
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
    )


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    configure_scheduler(scheduler)
    scheduler.start()
    return scheduler
