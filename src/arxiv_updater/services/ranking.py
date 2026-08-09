from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    Interaction,
    InteractionKind,
    Paper,
    RecommendationBatch,
    RecommendationItem,
)
from .recommendations import RANKING_VERSION
from .retention import UNINTERACTED_RETENTION_DAYS


@dataclass(slots=True)
class RankedPaper:
    paper: Paper
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)
    interaction_kinds: set[InteractionKind] = field(default_factory=set)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC) - timedelta(days=365)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _source_names(paper: Paper) -> set[str]:
    return {source.source for source in paper.sources}


def _interaction_map(db: Session) -> dict[str, set[InteractionKind]]:
    result: dict[str, set[InteractionKind]] = {}
    for interaction in db.scalars(select(Interaction)):
        result.setdefault(interaction.paper_id, set()).add(interaction.kind)
    return result


def _matches_view(paper: Paper, view: str, kinds: set[InteractionKind]) -> bool:
    if InteractionKind.DISMISSED in kinds:
        return False
    sources = _source_names(paper)
    if view == "saved":
        return InteractionKind.SAVED in kinds
    if view == "authors":
        return "scholar" in sources
    if view == "scirate":
        return paper.is_scirate_hot
    if view == "arxiv":
        return "arxiv" in sources
    if view == "journals":
        return "journal" in sources
    return True


def _filter_papers(
    papers: list[Paper],
    *,
    view: str,
    query: str,
    category: str,
    interactions: dict[str, set[InteractionKind]],
) -> list[Paper]:
    filtered: list[Paper] = []
    for paper in papers:
        kinds = interactions.get(paper.id, set())
        if not _matches_view(paper, view, kinds):
            continue
        if category and category not in (paper.categories or []):
            continue
        if query and query.lower() not in f"{paper.title} {paper.authors_text}".lower():
            continue
        filtered.append(paper)
    return filtered


def _latest_batch_items(db: Session) -> list[RecommendationItem]:
    batch = db.scalar(
        select(RecommendationBatch)
        .where(
            RecommendationBatch.status == "success",
            RecommendationBatch.ranking_version == RANKING_VERSION,
        )
        .order_by(RecommendationBatch.generated_at.desc())
    )
    if not batch:
        return []
    return list(
        db.scalars(
            select(RecommendationItem)
            .where(RecommendationItem.batch_id == batch.id)
            .options(
                selectinload(RecommendationItem.paper).selectinload(Paper.sources),
                selectinload(RecommendationItem.paper).selectinload(Paper.interactions),
            )
            .order_by(RecommendationItem.position)
        ).all()
    )


def rank_papers(
    db: Session,
    *,
    view: str = "featured",
    query: str = "",
    category: str = "",
    limit: int = 100,
    offset: int = 0,
    now: datetime | None = None,
) -> list[RankedPaper]:
    """Return a local-reader view; only ``featured`` uses recommendation order."""

    now = now or datetime.now(UTC)
    interactions = _interaction_map(db)
    if view == "featured":
        batch_items = _latest_batch_items(db)
        if batch_items:
            results = [
                RankedPaper(
                    paper=item.paper,
                    score=item.final_score,
                    reasons=[item.reason] if item.reason else [],
                    interaction_kinds=interactions.get(item.paper_id, set()),
                )
                for item in batch_items
                if _matches_view(item.paper, "featured", interactions.get(item.paper_id, set()))
                and (not category or category in (item.paper.categories or []))
                and (
                    not query
                    or query.lower() in f"{item.paper.title} {item.paper.authors_text}".lower()
                )
            ]
            return results[offset : offset + limit]
        return []

    query_statement = select(Paper).options(
        selectinload(Paper.sources), selectinload(Paper.interactions)
    )
    if view != "saved":
        query_statement = query_statement.where(
            Paper.discovered_at >= now - timedelta(days=UNINTERACTED_RETENTION_DAYS)
        )
    papers = list(
        db.scalars(query_statement.order_by(Paper.discovered_at.desc(), Paper.id.desc()))
        .unique()
        .all()
    )
    filtered = _filter_papers(
        papers,
        view=view,
        query=query,
        category=category,
        interactions=interactions,
    )
    if view == "scirate":
        filtered.sort(
            key=lambda paper: (
                paper.scites_count,
                _aware(paper.published_at),
                _aware(paper.discovered_at),
                paper.id,
            ),
            reverse=True,
        )
    return [
        RankedPaper(paper=paper, interaction_kinds=interactions.get(paper.id, set()))
        for paper in filtered[offset : offset + limit]
    ]


def available_categories(
    db: Session, *, within_days: int = 30, now: datetime | None = None
) -> list[str]:
    now = now or datetime.now(UTC)
    papers = db.scalars(
        select(Paper.categories).where(Paper.discovered_at >= now - timedelta(days=within_days))
    ).all()
    return sorted({category for categories in papers for category in (categories or [])})
