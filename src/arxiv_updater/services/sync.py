from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..datetime_utils import as_utc
from ..db import SessionLocal
from ..models import (
    ApiUsage,
    JournalSubscription,
    Paper,
    PaperSource,
    SyncRun,
    SyncStatus,
    TrackedAuthor,
    utcnow,
)
from ..sources.arxiv import ArxivAdapter
from ..sources.base import PaperCandidate
from ..sources.journals import DEFAULT_JOURNAL_FEEDS, JournalAdapter, JournalFeed
from ..sources.scholar import ScholarAdapter
from ..sources.scirate import SciRateAdapter
from .papers import upsert_paper


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _serpapi_queries_this_month(db: Session) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(ApiUsage.request_count), 0)).where(
                ApiUsage.service == "serpapi", ApiUsage.created_at >= _month_start()
            )
        )
        or 0
    )


def _build_adapter(db: Session, name: str, *, allow_browser_challenge: bool = False):
    if name == "arxiv":
        return ArxivAdapter()
    if name == "journals":
        custom_feeds = db.scalars(
            select(JournalSubscription).where(JournalSubscription.is_active.is_(True))
        ).all()
        feeds = [
            *DEFAULT_JOURNAL_FEEDS,
            *[
                JournalFeed(feed.name, feed.feed_url, feed.issn)
                for feed in custom_feeds
                if feed.feed_url not in {default.url for default in DEFAULT_JOURNAL_FEEDS}
            ],
        ]
        return JournalAdapter(feeds=feeds)
    if name == "scirate":
        return SciRateAdapter(allow_browser_challenge=allow_browser_challenge)
    if name == "scholar":
        settings = get_settings()
        used = _serpapi_queries_this_month(db)
        remaining = max(0, settings.serpapi_monthly_query_budget - used)
        authors = db.scalars(
            select(TrackedAuthor)
            .order_by(TrackedAuthor.last_synced_at.asc().nullsfirst())
            .limit(remaining)
        ).all()
        if not authors:
            raise RuntimeError("No tracked authors or the SerpAPI monthly budget is exhausted")
        return ScholarAdapter([author.scholar_author_id for author in authors])
    raise ValueError(f"Unknown source: {name}")


def _apply_scirate(
    db: Session, adapter: SciRateAdapter, candidates: list[PaperCandidate]
) -> tuple[int, int]:
    sorted_records = sorted(adapter.records, key=lambda item: item.scites_count, reverse=True)[:50]
    candidate_by_id = {
        candidate.arxiv_id: candidate for candidate in candidates if candidate.arxiv_id is not None
    }
    missing_ids = [
        record.arxiv_id for record in sorted_records if record.arxiv_id not in candidate_by_id
    ]
    if missing_ids:
        raise RuntimeError("SciRate metadata missing for: " + ", ".join(missing_ids))

    # SciRate is a rolling three-day list.  A paper must stop being marked hot once it leaves it.
    db.execute(update(Paper).where(Paper.is_scirate_hot.is_(True)).values(is_scirate_hot=False))
    created = 0
    for record in sorted_records:
        result = upsert_paper(db, candidate_by_id[record.arxiv_id])
        paper = result.paper
        created += int(result.created)
        paper.scites_count = record.scites_count
        paper.is_scirate_hot = True
        source = db.scalar(
            select(PaperSource).where(
                PaperSource.source == "scirate", PaperSource.external_id == record.arxiv_id
            )
        )
        if not source:
            db.add(
                PaperSource(
                    paper_id=paper.id,
                    source="scirate",
                    external_id=record.arxiv_id,
                    url=f"https://scirate.com/arxiv/{record.arxiv_id}",
                    metadata_json={"scites_count": record.scites_count},
                )
            )
        else:
            source.url = f"https://scirate.com/arxiv/{record.arxiv_id}"
            source.metadata_json = candidate_by_id[record.arxiv_id].metadata
            source.last_seen_at = utcnow()
    return len(sorted_records), created


def sync_sources(
    db: Session, source: str = "all", *, allow_browser_challenge: bool = False
) -> list[SyncRun]:
    sources = ["arxiv", "scholar", "scirate", "journals"] if source == "all" else [source]
    runs: list[SyncRun] = []
    for name in sources:
        run = SyncRun(source=name, status=SyncStatus.RUNNING)
        db.add(run)
        db.commit()
        previous = db.scalar(
            select(SyncRun)
            .where(SyncRun.source == name, SyncRun.status == SyncStatus.SUCCESS)
            .order_by(SyncRun.finished_at.desc())
        )
        previous_finished_at = as_utc(previous.finished_at) if previous else None
        since = (
            previous_finished_at - timedelta(days=1)
            if previous_finished_at
            else datetime.now(UTC) - timedelta(days=14)
        )
        try:
            adapter = _build_adapter(
                db,
                name,
                allow_browser_challenge=allow_browser_challenge and name == "scirate",
            )
            candidates = adapter.fetch(since)
            if isinstance(adapter, SciRateAdapter):
                run.items_seen, run.items_created = _apply_scirate(db, adapter, candidates)
            else:
                created = 0
                for candidate in candidates:
                    created += int(upsert_paper(db, candidate).created)
                run.items_seen = len(candidates)
                run.items_created = created
            if isinstance(adapter, JournalAdapter) and adapter.errors:
                run.error = "Partial sync: " + "; ".join(adapter.errors)
            if isinstance(adapter, ScholarAdapter):
                author_map = {
                    author.scholar_author_id: author
                    for author in db.scalars(select(TrackedAuthor)).all()
                }
                synced_at = utcnow()
                for author_id, name_value in adapter.author_names.items():
                    author = author_map.get(author_id)
                    if author:
                        author.name = name_value
                        author.last_synced_at = synced_at
                        if author_id in adapter.author_citation_counts:
                            author.citation_count = adapter.author_citation_counts[author_id]
                            author.citation_count_updated_at = synced_at
                db.add(
                    ApiUsage(
                        service="serpapi",
                        operation="author_sync",
                        request_count=len(adapter.author_ids),
                    )
                )
                from .abstracts import enrich_missing_scholar_abstracts

                enrich_missing_scholar_abstracts(db)
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
