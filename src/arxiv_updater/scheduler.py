from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from .config import get_settings


def configure_scheduler(scheduler: BaseScheduler) -> None:
    from .services.sync import scheduled_sync

    timezone = get_settings().timezone
    scheduler.add_job(
        scheduled_sync,
        "cron",
        hour=8,
        minute=0,
        timezone=timezone,
        id="daily-sources",
        replace_existing=True,
    )
    scheduler.add_job(
        scheduled_sync,
        "cron",
        day_of_week="mon",
        hour=6,
        minute=0,
        timezone=timezone,
        id="weekly-scholar",
        kwargs={"source": "scholar"},
        replace_existing=True,
    )


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    configure_scheduler(scheduler)
    scheduler.start()
    return scheduler


def run_worker() -> None:
    scheduler = BlockingScheduler()
    configure_scheduler(scheduler)
    scheduler.start()
