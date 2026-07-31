import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from ..models import AuthorFollow, Interaction, InteractionKind, Paper, User


@dataclass(slots=True)
class RankedPaper:
    paper: Paper
    score: float
    reasons: list[str] = field(default_factory=list)
    interaction_kinds: set[InteractionKind] = field(default_factory=set)


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC) - timedelta(days=365)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _source_names(paper: Paper) -> set[str]:
    return {source.source for source in paper.sources}


def _paper_text(paper: Paper) -> str:
    return f"{paper.title} {paper.abstract} {' '.join(paper.categories or [])}"


def _semantic_scores(query: str, papers: list[Paper]) -> list[float]:
    if not query.strip() or not papers:
        return [0.0] * len(papers)
    stop_words = {
        "a",
        "an",
        "and",
        "are",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "we",
        "with",
    }

    def vector(value: str) -> Counter[str]:
        return Counter(
            token
            for token in re.findall(r"[a-z0-9-]{2,}", value.lower())
            if token not in stop_words
        )

    query_vector = vector(query)
    query_norm = math.sqrt(sum(count * count for count in query_vector.values()))
    if not query_norm:
        return [0.0] * len(papers)
    scores: list[float] = []
    for paper in papers:
        paper_vector = vector(_paper_text(paper))
        paper_norm = math.sqrt(sum(count * count for count in paper_vector.values()))
        dot = sum(count * paper_vector.get(token, 0) for token, count in query_vector.items())
        scores.append(dot / (query_norm * paper_norm) if paper_norm else 0.0)
    return scores


def _behavior_query(db: Session, user_id: str, positive: bool) -> str:
    kinds = (
        [InteractionKind.INTERESTED, InteractionKind.SAVED, InteractionKind.FULLTEXT]
        if positive
        else [InteractionKind.DISMISSED]
    )
    papers = db.scalars(
        select(Paper)
        .join(Interaction)
        .where(Interaction.user_id == user_id, Interaction.kind.in_(kinds))
        .order_by(Interaction.created_at.desc())
        .limit(50)
    ).all()
    return " ".join(_paper_text(paper) for paper in papers)


def rank_papers(
    db: Session,
    user: User,
    view: str = "weekly",
    query: str = "",
    category: str = "",
    limit: int = 20,
    now: datetime | None = None,
) -> list[RankedPaper]:
    now = now or datetime.now(UTC)
    cutoff_days = 7 if view == "weekly" else 30
    cutoff = now - timedelta(days=cutoff_days)
    statement = (
        select(Paper)
        .options(selectinload(Paper.sources), selectinload(Paper.interactions))
        .where(or_(Paper.published_at >= cutoff, Paper.discovered_at >= cutoff))
        .order_by(Paper.published_at.desc(), Paper.discovered_at.desc())
        .limit(1000)
    )
    papers = list(db.scalars(statement).unique().all())

    interactions_by_paper: dict[str, set[InteractionKind]] = {}
    for interaction in db.scalars(select(Interaction).where(Interaction.user_id == user.id)):
        interactions_by_paper.setdefault(interaction.paper_id, set()).add(interaction.kind)

    followed_scholar_ids = set()
    for follow in db.scalars(
        select(AuthorFollow)
        .options(selectinload(AuthorFollow.author))
        .where(AuthorFollow.user_id == user.id)
    ):
        followed_scholar_ids.add(follow.author.scholar_author_id)

    filtered: list[Paper] = []
    for paper in papers:
        kinds = interactions_by_paper.get(paper.id, set())
        sources = _source_names(paper)
        if InteractionKind.DISMISSED in kinds:
            continue
        if view == "saved" and InteractionKind.SAVED not in kinds:
            continue
        if view == "authors":
            belongs_to_followed_author = any(
                source.metadata_json.get("tracked_author_id") in followed_scholar_ids
                for source in paper.sources
                if source.source == "scholar"
            )
            if not belongs_to_followed_author:
                continue
        if view == "scirate" and not paper.is_scirate_hot:
            continue
        if view == "arxiv" and "arxiv" not in sources:
            continue
        if view == "journals" and "journal" not in sources:
            continue
        if category and category not in (paper.categories or []):
            continue
        if query:
            haystack = f"{paper.title} {paper.authors_text}".lower()
            if query.lower() not in haystack:
                continue
        if view == "weekly" and kinds:
            continue
        filtered.append(paper)

    interests = _semantic_scores(user.interests, filtered)
    positives = _semantic_scores(_behavior_query(db, user.id, True), filtered)
    negatives = _semantic_scores(_behavior_query(db, user.id, False), filtered)
    ranked: list[RankedPaper] = []
    for index, paper in enumerate(filtered):
        reasons: list[tuple[float, str]] = []
        score = 0.0
        published = _aware(paper.published_at or paper.discovered_at)
        age_days = max(0.0, (now - published).total_seconds() / 86400)
        freshness = 20 * (2 ** (-age_days / 7))
        score += freshness
        reasons.append((freshness, "近期发布"))

        sources = _source_names(paper)
        paper_followed = any(
            source.metadata_json.get("tracked_author_id") in followed_scholar_ids
            for source in paper.sources
            if source.source == "scholar"
        )
        if paper_followed:
            score += 40
            reasons.append((40, "你关注的作者"))
        if "journal" in sources:
            score += 15
            reasons.append((15, "重点期刊"))
        if paper.scites_count:
            scirate = min(20.0, 4 * math.log2(1 + paper.scites_count))
            score += scirate
            reasons.append((scirate, "SciRate 热度较高"))
        interest_score = 25 * interests[index]
        if interest_score > 0.5:
            score += interest_score
            reasons.append((interest_score, "匹配你的研究兴趣"))
        behavior_score = 25 * (positives[index] - negatives[index])
        score += behavior_score
        if behavior_score > 0.5:
            reasons.append((behavior_score, "类似你感兴趣的论文"))
        elif behavior_score < -0.5:
            reasons.append((abs(behavior_score), "与低偏好主题相近"))
        kinds = interactions_by_paper.get(paper.id, set())
        if not kinds:
            score += 5
        ranked.append(
            RankedPaper(
                paper=paper,
                score=round(score, 2),
                reasons=[label for _, label in sorted(reasons, reverse=True)[:2]],
                interaction_kinds=kinds,
            )
        )
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked[:limit]
