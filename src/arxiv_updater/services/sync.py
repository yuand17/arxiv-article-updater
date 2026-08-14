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
    SeenSourceItem,
    SyncRun,
    SyncStatus,
    TrackedAuthor,
    utcnow,
)
from ..security import redact_sensitive_text
from ..sources.arxiv import ArxivAdapter
from ..sources.base import PaperCandidate
from ..sources.journals import JournalAdapter, JournalFeed
from ..sources.scholar import ScholarAdapter, SerpApiAccountUsage
from ..sources.scirate import SciRateAdapter
from .article_classification import classify_journal_candidate
from .journal_catalog import CATALOG_VERSION, ensure_builtin_journals
from .papers import upsert_paper


def _month_start() -> datetime:
    now = datetime.now(UTC)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


SERPAPI_SINGLE_USER_MONTHLY_LIMIT = 250
SERPAPI_BILLED_OPERATION = "author_sync_billed"


def _serpapi_billed_queries_this_month(db: Session) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(ApiUsage.request_count), 0)).where(
                ApiUsage.service == "serpapi",
                ApiUsage.operation == SERPAPI_BILLED_OPERATION,
                ApiUsage.created_at >= _month_start(),
            )
        )
        or 0
    )


def _build_adapter(db: Session, name: str, *, allow_browser_challenge: bool = False):
    if name == "arxiv":
        return ArxivAdapter()
    if name == "journals":
        raise ValueError("Journal subscriptions are synchronized independently")
    if name == "scirate":
        return SciRateAdapter(allow_browser_challenge=allow_browser_challenge)
    if name == "scholar":
        authors = db.scalars(
            select(TrackedAuthor)
            .order_by(TrackedAuthor.last_synced_at.asc().nullsfirst())
        ).all()
        if not authors:
            raise RuntimeError("No tracked authors")
        adapter = ScholarAdapter([author.scholar_author_id for author in authors])
        try:
            account_usage = adapter.fetch_account_usage()
        except RuntimeError:
            billed = _serpapi_billed_queries_this_month(db)
            remaining = max(0, SERPAPI_SINGLE_USER_MONTHLY_LIMIT - billed)
        else:
            adapter.account_usage_before = account_usage
            remaining = account_usage.total_searches_left
        if remaining < len(authors):
            raise RuntimeError(
                f"SerpAPI 月度额度不足：当前剩余 {remaining} 次，"
                f"同步 {len(authors)} 位重点作者最多需要 {len(authors)} 次"
            )
        return adapter
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


def _record_seen(
    db: Session,
    candidate: PaperCandidate,
    *,
    outcome: str,
    reason: str,
    version: str,
    paper_id: str | None = None,
) -> SeenSourceItem:
    seen = db.scalar(
        select(SeenSourceItem).where(
            SeenSourceItem.source == candidate.source,
            SeenSourceItem.external_id == candidate.external_id,
        )
    )
    if seen is None:
        seen = SeenSourceItem(
            source=candidate.source,
            external_id=candidate.external_id,
            doi=candidate.doi,
            outcome=outcome,
        )
        db.add(seen)
    seen.last_seen_at = utcnow()
    seen.doi = candidate.doi or seen.doi
    seen.outcome = outcome
    seen.reason = reason[:1000]
    seen.classification_version = version
    seen.paper_id = paper_id
    return seen


def _seen_item_blocks(db: Session, candidate: PaperCandidate) -> bool:
    if candidate.source not in {"scholar", "journal"}:
        return False
    seen = db.scalar(
        select(SeenSourceItem).where(
            SeenSourceItem.source == candidate.source,
            SeenSourceItem.external_id == candidate.external_id,
        )
    )
    if seen and seen.paper_id is None and seen.outcome in {
        "cleaned",
        "nonresearch",
        "nonphysics",
    }:
        seen.last_seen_at = utcnow()
        return True
    return False


def _sync_journals(db: Session) -> tuple[int, int, list[str]]:
    subscriptions = [
        subscription
        for subscription in ensure_builtin_journals(db)
        if subscription.is_active
    ]
    total_seen = total_created = 0
    errors: list[str] = []
    for subscription in subscriptions:
        subscription.last_attempt_at = utcnow()
        db.commit()
        last_success = as_utc(subscription.last_success_at)
        since = (
            last_success - timedelta(days=1)
            if last_success is not None
            else datetime.now(UTC) - timedelta(days=14)
        )
        feeds = [
            JournalFeed(
                subscription.name,
                endpoint.url,
                subscription.issn_online or subscription.issn_print,
                endpoint.kind,
            )
            for endpoint in sorted(subscription.endpoints, key=lambda item: item.priority)
            if endpoint.kind in {"rss", "atom", "crossref"}
        ]
        try:
            adapter = JournalAdapter(feeds=feeds)
            candidates = adapter.fetch(since)
            scanned = imported = nonresearch = nonphysics = 0
            for candidate in candidates:
                scanned += 1
                candidate.metadata["journal_subscription_id"] = subscription.id
                result = classify_journal_candidate(
                    candidate,
                    journal_name=subscription.name,
                    scope_kind=subscription.scope_kind,
                )
                classification_version = f"{result.version}+{CATALOG_VERSION}"
                existing_seen = db.scalar(
                    select(SeenSourceItem).where(
                        SeenSourceItem.source == candidate.source,
                        SeenSourceItem.external_id == candidate.external_id,
                    )
                )
                if (
                    existing_seen
                    and existing_seen.paper_id is None
                    and existing_seen.outcome in {"cleaned", "nonresearch", "nonphysics"}
                    and existing_seen.classification_version == classification_version
                ):
                    existing_seen.last_seen_at = utcnow()
                    continue
                if not result.accepted:
                    nonresearch += int(not result.is_original_research)
                    nonphysics += int(result.is_original_research and not result.is_physics)
                    _record_seen(
                        db,
                        candidate,
                        outcome=result.outcome,
                        reason=result.reason,
                        version=classification_version,
                    )
                    continue
                upserted = upsert_paper(db, candidate)
                paper = upserted.paper
                paper.document_type = result.document_type
                paper.is_original_research = result.is_original_research
                paper.is_physics = result.is_physics
                paper.physics_confidence = result.physics_confidence
                paper.classification_reason = result.reason
                paper.classification_source = result.source
                paper.classification_version = classification_version
                paper.classified_at = result.classified_at
                imported += int(upserted.created)
                _record_seen(
                    db,
                    candidate,
                    outcome="imported" if upserted.created else "duplicate",
                    reason=result.reason,
                    version=classification_version,
                    paper_id=paper.id,
                )
            subscription.last_success_at = utcnow()
            subscription.last_error = "; ".join(adapter.errors)[:2000]
            subscription.last_items_seen = scanned
            subscription.last_items_imported = imported
            subscription.last_nonresearch_filtered = nonresearch
            subscription.last_nonphysics_filtered = nonphysics
            db.commit()
            total_seen += scanned
            total_created += imported
        except Exception as exc:
            db.rollback()
            current = db.get(JournalSubscription, subscription.id)
            if current:
                current.last_error = f"{type(exc).__name__}: {exc}"[:2000]
                current.last_attempt_at = utcnow()
                db.commit()
            errors.append(f"{subscription.name}: {type(exc).__name__}")
    return total_seen, total_created, errors


def sync_sources(
    db: Session, source: str = "all", *, allow_browser_challenge: bool = False
) -> list[SyncRun]:
    sources = ["arxiv", "scholar", "scirate", "journals"] if source == "all" else [source]
    runs: list[SyncRun] = []
    for name in sources:
        if name == "scholar" and not get_settings().serpapi_api_key:
            run = SyncRun(
                source=name,
                status=SyncStatus.SKIPPED,
                started_at=utcnow(),
                finished_at=utcnow(),
                error="SerpAPI 未启用，已跳过 Google Scholar 更新。",
            )
            db.add(run)
            db.commit()
            runs.append(run)
            continue
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
            if name == "journals":
                run.items_seen, run.items_created, journal_errors = _sync_journals(db)
                if journal_errors:
                    run.error = "Partial sync: " + "; ".join(journal_errors)
                adapter = None
                candidates = []
            else:
                adapter = _build_adapter(
                    db,
                    name,
                    allow_browser_challenge=allow_browser_challenge and name == "scirate",
                )
                candidates = adapter.fetch(since)
            if isinstance(adapter, SciRateAdapter):
                run.items_seen, run.items_created = _apply_scirate(db, adapter, candidates)
            elif adapter is not None:
                created = 0
                for candidate in candidates:
                    if _seen_item_blocks(db, candidate):
                        continue
                    upserted = upsert_paper(db, candidate)
                    created += int(upserted.created)
                    if candidate.source == "scholar":
                        _record_seen(
                            db,
                            candidate,
                            outcome="imported" if upserted.created else "duplicate",
                            reason="tracked_author_result",
                            version="source-sync-v1",
                            paper_id=upserted.paper.id,
                        )
                run.items_seen = len(candidates)
                run.items_created = created
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
                billed_requests = _serpapi_billed_requests(adapter)
                if billed_requests:
                    db.add(
                        ApiUsage(
                            service="serpapi",
                            operation=SERPAPI_BILLED_OPERATION,
                            request_count=billed_requests,
                        )
                    )
            run.status = SyncStatus.SUCCESS
        except Exception as exc:
            db.rollback()
            run = db.get(SyncRun, run.id) or run
            run.status = SyncStatus.FAILED
            settings = get_settings()
            run.error = redact_sensitive_text(
                f"{type(exc).__name__}: {exc}",
                (settings.serpapi_api_key, settings.deepseek_api_key),
            )[:2000]
        run.finished_at = utcnow()
        db.add(run)
        db.commit()
        runs.append(run)
    return runs


def _serpapi_billed_requests(adapter: ScholarAdapter) -> int:
    before = getattr(adapter, "account_usage_before", None)
    if not isinstance(before, SerpApiAccountUsage):
        return 0
    try:
        after = adapter.fetch_account_usage()
    except RuntimeError:
        return 0
    usage_delta = max(0, after.this_month_usage - before.this_month_usage)
    remaining_delta = max(0, before.total_searches_left - after.total_searches_left)
    return max(usage_delta, remaining_delta)


def scheduled_sync(source: str = "all") -> None:
    with SessionLocal() as db:
        sync_sources(db, source)
