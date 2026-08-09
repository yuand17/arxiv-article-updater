"""arXiv announcement-aware scheduling rules."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .datetime_utils import as_utc

ARXIV_TIMEZONE = ZoneInfo("America/New_York")
ARXIV_ANNOUNCEMENT_WEEKDAYS = {0, 1, 2, 3, 6}  # Monday-Thursday and Sunday
ARXIV_ANNOUNCEMENT_TIME = time(hour=20)
ARXIV_FETCH_DELAY = timedelta(minutes=10)


def next_arxiv_update_at(after: datetime | None = None) -> datetime:
    """Return the next post-announcement fetch time as an aware UTC datetime."""

    current_utc = as_utc(after or datetime.now(UTC))
    assert current_utc is not None
    current_eastern = current_utc.astimezone(ARXIV_TIMEZONE)

    for day_offset in range(8):
        announcement_date = current_eastern.date() + timedelta(days=day_offset)
        if announcement_date.weekday() not in ARXIV_ANNOUNCEMENT_WEEKDAYS:
            continue
        announcement = datetime.combine(
            announcement_date,
            ARXIV_ANNOUNCEMENT_TIME,
            tzinfo=ARXIV_TIMEZONE,
        )
        fetch_at = announcement + ARXIV_FETCH_DELAY
        if fetch_at > current_eastern:
            return fetch_at.astimezone(UTC)

    raise RuntimeError("Could not calculate the next arXiv announcement")
