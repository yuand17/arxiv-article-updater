"""Embedded, per-source scheduler for the local single-user application."""

import threading
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session

from .arxiv_schedule import next_arxiv_update_at
from .config import get_settings
from .datetime_utils import as_utc
from .db import SessionLocal
from .models import SourceSchedule, SyncRun, SyncStatus, utcnow

DEFAULT_SOURCE_INTERVALS = {
    "arxiv": 1,
    "scirate": 3,
    "scholar": 7,
    "journals": 1,
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
    changed = False
    scholar_available = bool(get_settings().serpapi_api_key)
    for source, interval_days in DEFAULT_SOURCE_INTERVALS.items():
        schedule = db.get(SourceSchedule, source)
        if schedule is None:
            db.add(
                SourceSchedule(
                    source=source,
                    enabled=scholar_available if source == "scholar" else True,
                    interval_days=interval_days,
                    next_due_at=now if source != "scholar" or scholar_available else None,
                )
            )
            changed = True
        elif source == "scholar" and not scholar_available and schedule.enabled:
            schedule.enabled = False
            schedule.next_due_at = None
            schedule.last_error = ""
            schedule.updated_at = now
            changed = True
        elif source in {"arxiv", "journals"} and (
            not schedule.enabled or schedule.interval_days != interval_days
        ):
            schedule.enabled = True
            schedule.interval_days = interval_days
            if schedule.next_due_at is None:
                schedule.next_due_at = now
            schedule.last_error = ""
            schedule.updated_at = now
            changed = True
    if changed:
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
        if run.status == SyncStatus.SKIPPED:
            current.enabled = False
            current.next_due_at = None
            current.last_error = ""
            db.commit()
            return False
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
            batch = generate_recommendation_batch(db, now=now)
            if batch.status == "success":
                from .services.retention import run_retention_cleanup

                run_retention_cleanup(db, now=now)


def run_source_update_in_background(
    source: str, allow_browser_challenge: bool = False
) -> None:
    with SessionLocal() as db:
        run_source_update(
            db,
            source,
            allow_browser_challenge=allow_browser_challenge,
        )


def run_all_source_updates_in_background() -> None:
    """Run all four manual source updates in order and record one aggregate result."""

    with SessionLocal() as db:
        aggregate = SyncRun(source="all", status=SyncStatus.RUNNING)
        db.add(aggregate)
        db.commit()
        aggregate_id = aggregate.id

    total_seen = 0
    total_created = 0
    failed_sources: list[str] = []
    skipped_sources: list[str] = []
    for source in DEFAULT_SOURCE_INTERVALS:
        source_started_at = utcnow().replace(tzinfo=None)
        try:
            with SessionLocal() as db:
                run_source_update(
                    db,
                    source,
                    allow_browser_challenge=source == "scirate",
                )
                run = db.scalar(
                    select(SyncRun)
                    .where(
                        SyncRun.source == source,
                        SyncRun.started_at >= source_started_at,
                    )
                    .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
                )
                if run is None:
                    failed_sources.append(f"{source}（已有更新正在运行）")
                    continue
                total_seen += run.items_seen
                total_created += run.items_created
                if run.status == SyncStatus.SKIPPED:
                    skipped_sources.append(source)
                elif run.status != SyncStatus.SUCCESS:
                    failed_sources.append(f"{source}（{run.error or '同步失败'}）")
        except Exception as exc:
            failed_sources.append(f"{source}（{type(exc).__name__}）")

    with SessionLocal() as db:
        current_aggregate = db.get(SyncRun, aggregate_id)
        if current_aggregate is None:
            return
        current_aggregate.items_seen = total_seen
        current_aggregate.items_created = total_created
        current_aggregate.finished_at = utcnow()
        if failed_sources:
            current_aggregate.status = SyncStatus.FAILED
            current_aggregate.error = "部分来源更新失败：" + "；".join(failed_sources)
        else:
            current_aggregate.status = SyncStatus.SUCCESS
            current_aggregate.error = (
                "已跳过未启用的来源：" + "、".join(skipped_sources)
                if skipped_sources
                else ""
            )
        db.commit()


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
