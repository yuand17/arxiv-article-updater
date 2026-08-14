"""Deterministic three-day BM25 shortlist followed by optional DeepSeek reranking."""

import json
import math
import re
import threading
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings, get_settings
from ..db import SessionLocal
from ..models import (
    ApiUsage,
    AppPreferences,
    Interaction,
    InteractionKind,
    Paper,
    RecommendationBatch,
    RecommendationItem,
    SourceSchedule,
    utcnow,
)
from .preferences import PreferenceUnavailableError, check_token_budget, get_preferences

RECOMMENDATION_PROMPT_VERSION = "featured-v2"
RANKING_VERSION = "bm25-v1"
RERANK_CHUNK_SIZE = 50
RERANK_MIN_OUTPUT_TOKENS = 4_000
RERANK_OUTPUT_TOKENS_PER_PAPER = 200
RERANK_ABSTRACT_CHAR_LIMIT = 4_000
TITLE_WEIGHT = 3.0
ABSTRACT_WEIGHT = 1.0
BM25_K1 = 1.5
BM25_B = 0.75
EXPLORATION_FRACTION = 0.10
STOPWORDS = {
    "about",
    "after",
    "also",
    "among",
    "analysis",
    "approach",
    "based",
    "between",
    "from",
    "have",
    "into",
    "model",
    "paper",
    "result",
    "results",
    "show",
    "study",
    "system",
    "that",
    "their",
    "these",
    "this",
    "using",
    "with",
}
_generation_lock = threading.Lock()


class RecommendationUnavailableError(RuntimeError):
    """A generation error that should fall back to local deterministic ranking."""

    def __init__(
        self,
        message: str,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> None:
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class RecommendationOutputTruncatedError(RecommendationUnavailableError):
    """A length-limited response that should be retried with smaller chunks."""


class ModelRecommendation(BaseModel):
    paper_id: str
    preference_score: float = Field(ge=0, le=100)
    confidence: float = Field(default=1.0, ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)


class ModelRecommendationResponse(BaseModel):
    items: list[ModelRecommendation] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RerankResult:
    items: list[ModelRecommendation]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class _AdaptiveRerankOutcome:
    scores: dict[str, ModelRecommendation]
    model: str
    successful: int
    failed: int
    requests: int
    input_tokens: int
    output_tokens: int
    errors: list[str]


@dataclass(frozen=True, slots=True)
class LocalRank:
    paper: Paper
    bm25_score: float
    structured_score: float
    final_score: float


class RecommendationProvider(ABC):
    @abstractmethod
    def rerank(self, preferences: AppPreferences, papers: list[Paper]) -> RerankResult:
        """Score every supplied paper and return only their supplied IDs."""


class DeepSeekRecommendationProvider(RecommendationProvider):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.llm_base_url,
            timeout=60.0,
            max_retries=1,
        )

    def rerank(self, preferences: AppPreferences, papers: list[Paper]) -> RerankResult:
        profile = preferences.profile_json or {}
        prompt = json.dumps(
            {
                "manual_interests": preferences.manual_interests,
                "preference_profile": profile,
                "absolute_scale": {
                    "0": "unrelated",
                    "50": "possibly useful",
                    "100": "directly aligned with the reader's research",
                },
                "papers": [
                    {
                        "paper_id": paper.id,
                        "title": paper.title,
                        "authors": paper.authors_text,
                        "sources": sorted(_source_names(paper)),
                        "abstract": (paper.abstract or "unavailable")[
                            :RERANK_ABSTRACT_CHAR_LIMIT
                        ],
                    }
                    for paper in papers
                ],
                "required_output": {
                    "items": [
                        {
                            "paper_id": "one supplied ID",
                            "preference_score": "0 to 100",
                            "confidence": "0 to 1",
                            "reason": "short Chinese reason",
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )
        extra_body = None
        if self.settings.llm_model.startswith("deepseek-v4"):
            extra_body = {
                "thinking": {
                    "type": "enabled" if self.settings.llm_thinking_enabled else "disabled"
                }
            }
        response = None
        try:
            response = self.client.chat.completions.create(
                model=self.settings.llm_model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You rank scientific papers for one reader. Use only the supplied "
                            "profile and paper metadata. Return JSON only. Include exactly one "
                            "result for every supplied paper ID, never invent an ID."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=max(
                    RERANK_MIN_OUTPUT_TOKENS,
                    len(papers) * RERANK_OUTPUT_TOKENS_PER_PAPER,
                ),
                extra_body=extra_body,
            )
            choice = response.choices[0]
            usage = response.usage
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            if choice.finish_reason == "length":
                raise RecommendationOutputTruncatedError(
                    "DeepSeek 推荐输出达到长度上限，将自动缩小分组重试",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            if choice.finish_reason not in {"stop", None}:
                raise RecommendationUnavailableError(
                    f"DeepSeek 推荐输出未完成：{choice.finish_reason}",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            raw = choice.message.content or "{}"
            parsed = ModelRecommendationResponse.model_validate(json.loads(raw))
        except RecommendationUnavailableError:
            raise
        except (AttributeError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            usage = getattr(response, "usage", None)
            raise RecommendationUnavailableError(
                "DeepSeek 返回的推荐格式无效",
                input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            ) from exc
        except Exception as exc:
            raise RecommendationUnavailableError("DeepSeek 推荐排序服务暂时不可用") from exc
        return RerankResult(
            items=parsed.items,
            model=self.settings.llm_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC) - timedelta(days=365)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _source_names(paper: Paper) -> set[str]:
    return {source.source for source in paper.sources}


def _recently_recommended_without_update(db: Session, cutoff: datetime) -> set[str]:
    rows = db.execute(
        select(Paper, RecommendationBatch.generated_at)
        .join(RecommendationItem, RecommendationItem.paper_id == Paper.id)
        .join(RecommendationBatch, RecommendationBatch.id == RecommendationItem.batch_id)
        .where(
            RecommendationBatch.generated_at >= cutoff,
            RecommendationBatch.ranking_version == RANKING_VERSION,
        )
    ).all()
    excluded: set[str] = set()
    for paper, generated_at in rows:
        if paper.updated_at is None or _aware(paper.updated_at) <= _aware(generated_at):
            excluded.add(paper.id)
    return excluded


def recommendation_candidates(db: Session, *, now: datetime | None = None) -> list[Paper]:
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=3)
    dismissed_ids = set(
        db.scalars(
            select(Interaction.paper_id).where(Interaction.kind == InteractionKind.DISMISSED)
        ).all()
    )
    previously_recommended = _recently_recommended_without_update(
        db, now - timedelta(days=30)
    )
    papers = list(
        db.scalars(
            select(Paper)
            .options(selectinload(Paper.sources))
            .where(or_(Paper.published_at >= window_start, Paper.discovered_at >= window_start))
            .order_by(Paper.discovered_at.desc(), Paper.id.desc())
        )
        .unique()
        .all()
    )
    return [
        paper
        for paper in papers
        if paper.id not in dismissed_ids
        and paper.id not in previously_recommended
        and not (
            "journal" in _source_names(paper)
            and (paper.is_original_research is False or paper.is_physics is False)
        )
    ]


def _tokens(value: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[a-z][a-z0-9-]{2,}", value.lower())
        if token not in STOPWORDS
    ]


def _profile_term_weights(db: Session, preferences: AppPreferences) -> dict[str, float]:
    profile = preferences.profile_json or {}
    positive_values = [preferences.manual_interests, preferences.profile_summary]
    for key in ("topics", "methods", "physical_systems", "preferred_authors"):
        positive_values.extend(str(value) for value in profile.get(key, []))
    weights: dict[str, float] = {}
    for value in positive_values:
        for token in _tokens(value):
            weights[token] = weights.get(token, 0.0) + 1.0

    interactions = db.scalars(
        select(Interaction)
        .options(selectinload(Interaction.paper))
        .where(
            Interaction.kind.in_(
                (
                    InteractionKind.SAVED,
                    InteractionKind.ABSTRACT_VIEWED,
                    InteractionKind.FULLTEXT,
                    InteractionKind.DISMISSED,
                )
            )
        )
    ).all()
    signal_weight = {
        InteractionKind.SAVED: 2.0,
        InteractionKind.ABSTRACT_VIEWED: 1.0,
        InteractionKind.FULLTEXT: 1.5,
        InteractionKind.DISMISSED: -1.5,
    }
    for interaction in interactions:
        for token in set(_tokens(f"{interaction.paper.title} {interaction.paper.abstract}")):
            weights[token] = weights.get(token, 0.0) + signal_weight[interaction.kind]
    return {term: weight for term, weight in weights.items() if weight != 0}


def _bm25_scores(
    papers: list[Paper], term_weights: dict[str, float]
) -> dict[str, float]:
    if not papers or not term_weights:
        return {paper.id: 0.0 for paper in papers}
    documents: dict[str, Counter[str]] = {}
    lengths: dict[str, float] = {}
    document_frequency: Counter[str] = Counter()
    for paper in papers:
        title_counts = Counter(_tokens(paper.title))
        abstract_counts = Counter(_tokens(paper.abstract))
        weighted = Counter(
            {
                term: TITLE_WEIGHT * title_counts[term] + ABSTRACT_WEIGHT * abstract_counts[term]
                for term in title_counts.keys() | abstract_counts.keys()
            }
        )
        documents[paper.id] = weighted
        lengths[paper.id] = sum(weighted.values()) or 1.0
        document_frequency.update(weighted.keys())
    average_length = sum(lengths.values()) / len(lengths)
    total = len(papers)
    scores: dict[str, float] = {}
    for paper in papers:
        score = 0.0
        for term, query_weight in term_weights.items():
            frequency = documents[paper.id].get(term, 0.0)
            if frequency <= 0:
                continue
            frequency_docs = document_frequency[term]
            inverse_frequency = math.log(
                1 + (total - frequency_docs + 0.5) / (frequency_docs + 0.5)
            )
            denominator = frequency + BM25_K1 * (
                1 - BM25_B + BM25_B * lengths[paper.id] / average_length
            )
            score += query_weight * inverse_frequency * frequency * (BM25_K1 + 1) / denominator
        scores[paper.id] = score
    return scores


def _structured_score(paper: Paper, now: datetime) -> float:
    age_days = max(
        0.0,
        (now - _aware(paper.published_at or paper.discovered_at)).total_seconds() / 86400,
    )
    freshness = max(0.0, 100 - min(100, age_days * 25))
    sources = _source_names(paper)
    author = 100.0 if "scholar" in sources else 0.0
    scirate = min(100.0, 20 * math.log2(1 + paper.scites_count)) if paper.scites_count else 0.0
    journal = 100.0 if "journal" in sources else 0.0
    diversity = min(100.0, len(sources) * 35.0)
    return 0.45 * freshness + 0.20 * author + 0.15 * scirate + 0.10 * journal + 0.10 * diversity


def local_rank_candidates(
    db: Session,
    papers: list[Paper],
    preferences: AppPreferences,
    now: datetime,
) -> list[LocalRank]:
    bm25 = _bm25_scores(papers, _profile_term_weights(db, preferences))
    positive_maximum = max(0.0, max(bm25.values(), default=0.0))
    negative_maximum = abs(min(0.0, min(bm25.values(), default=0.0)))
    ranked = []
    for paper in papers:
        raw_bm25 = bm25[paper.id]
        if raw_bm25 > 0 and positive_maximum:
            normalized_bm25 = 100.0 * raw_bm25 / positive_maximum
        elif raw_bm25 < 0 and negative_maximum:
            normalized_bm25 = 100.0 * raw_bm25 / negative_maximum
        else:
            normalized_bm25 = 0.0
        structured = _structured_score(paper, now)
        ranked.append(
            LocalRank(
                paper=paper,
                bm25_score=normalized_bm25,
                structured_score=structured,
                final_score=0.75 * normalized_bm25 + 0.25 * structured,
            )
        )
    return sorted(
        ranked,
        key=lambda item: (item.final_score, _aware(item.paper.discovered_at), item.paper.id),
        reverse=True,
    )


def build_shortlist(local_ranks: list[LocalRank], requested_count: int) -> list[LocalRank]:
    candidate_count = len(local_ranks)
    target = min(candidate_count, max(3 * requested_count, 100), 300)
    if target >= candidate_count:
        return local_ranks
    explore_count = max(1, round(target * EXPLORATION_FRACTION))
    core = local_ranks[: target - explore_count]
    core_ids = {item.paper.id for item in core}
    exploration = sorted(
        (item for item in local_ranks if item.paper.id not in core_ids),
        key=lambda item: (
            "scholar" in _source_names(item.paper),
            item.paper.is_scirate_hot,
            len(_source_names(item.paper)),
            _aware(item.paper.discovered_at),
            item.paper.id,
        ),
        reverse=True,
    )[:explore_count]
    return core + exploration


def _validated_model_scores(
    result: RerankResult, supplied_ids: set[str]
) -> dict[str, ModelRecommendation]:
    valid: dict[str, ModelRecommendation] = {}
    for item in result.items:
        if item.paper_id in supplied_ids and item.paper_id not in valid:
            valid[item.paper_id] = item
    if set(valid) != supplied_ids:
        raise RecommendationUnavailableError("模型未返回分组中的全部候选论文")
    return valid


def _rerank_chunk_adaptively(
    provider: RecommendationProvider,
    preferences: AppPreferences,
    papers: list[Paper],
) -> _AdaptiveRerankOutcome:
    """Rerank one chunk, splitting length-limited responses instead of dropping the chunk."""

    requests = input_tokens = output_tokens = 0
    last_error = ""
    for _attempt in range(2):
        requests += 1
        try:
            result = provider.rerank(preferences, papers)
            input_tokens += result.input_tokens
            output_tokens += result.output_tokens
            scores = _validated_model_scores(result, {paper.id for paper in papers})
            return _AdaptiveRerankOutcome(
                scores=scores,
                model=result.model,
                successful=len(scores),
                failed=0,
                requests=requests,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                errors=[],
            )
        except RecommendationOutputTruncatedError as exc:
            input_tokens += exc.input_tokens
            output_tokens += exc.output_tokens
            last_error = str(exc)
            if len(papers) > 1:
                midpoint = len(papers) // 2
                left = _rerank_chunk_adaptively(provider, preferences, papers[:midpoint])
                right = _rerank_chunk_adaptively(provider, preferences, papers[midpoint:])
                return _AdaptiveRerankOutcome(
                    scores={**left.scores, **right.scores},
                    model=right.model or left.model,
                    successful=left.successful + right.successful,
                    failed=left.failed + right.failed,
                    requests=requests + left.requests + right.requests,
                    input_tokens=input_tokens + left.input_tokens + right.input_tokens,
                    output_tokens=output_tokens + left.output_tokens + right.output_tokens,
                    errors=list(dict.fromkeys([*left.errors, *right.errors])),
                )
            break
        except RecommendationUnavailableError as exc:
            input_tokens += exc.input_tokens
            output_tokens += exc.output_tokens
            last_error = str(exc)
    return _AdaptiveRerankOutcome(
        scores={},
        model="",
        successful=0,
        failed=len(papers),
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        errors=[last_error or "DeepSeek 分组失败"],
    )


def recommendation_is_due(db: Session, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    latest = db.scalar(
        select(RecommendationBatch)
        .where(
            RecommendationBatch.status == "success",
            RecommendationBatch.ranking_version == RANKING_VERSION,
        )
        .order_by(RecommendationBatch.generated_at.desc())
    )
    if latest is None:
        return True
    return now - _aware(latest.generated_at) >= timedelta(days=3)


def _stale_sources(db: Session, now: datetime) -> list[str]:
    stale: list[str] = []
    for schedule in db.scalars(select(SourceSchedule).where(SourceSchedule.enabled.is_(True))):
        if schedule.last_success_at is None or now - _aware(schedule.last_success_at) > timedelta(
            days=max(1, schedule.interval_days) * 2
        ):
            stale.append(schedule.source)
    return sorted(stale)


def generate_recommendation_batch(
    db: Session,
    *,
    provider: RecommendationProvider | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RecommendationBatch:
    """Persist at most the configured N papers from the strict three-day window."""

    if not _generation_lock.acquire(blocking=False):
        raise RecommendationUnavailableError("三天精选任务已经在运行")
    try:
        now = now or utcnow()
        settings = settings or get_settings()
        preferences = get_preferences(db)
        requested_count = preferences.featured_paper_count
        window_start = now - timedelta(days=3)
        raw_candidate_count = int(
            db.scalar(
                select(func.count())
                .select_from(Paper)
                .where(
                    or_(
                        Paper.published_at >= window_start,
                        Paper.discovered_at >= window_start,
                    )
                )
            )
            or 0
        )
        candidates = recommendation_candidates(db, now=now)
        local_ranks = local_rank_candidates(db, candidates, preferences, now)
        shortlist = build_shortlist(local_ranks, requested_count)
        source_counts = Counter(
            source for item in candidates for source in _source_names(item)
        )
        batch = RecommendationBatch(
            generated_at=now,
            window_start=window_start,
            window_end=now,
            profile_generated_at=preferences.profile_generated_at,
            model=settings.llm_model if settings.deepseek_api_key else "local-bm25",
            prompt_version=RECOMMENDATION_PROMPT_VERSION,
            status="success",
            requested_count=requested_count,
            candidate_count=len(candidates),
            shortlist_count=len(shortlist),
            filtered_count=max(0, raw_candidate_count - len(candidates)),
            source_stats_json=dict(source_counts),
            stale_sources_json=_stale_sources(db, now),
            ranking_version=RANKING_VERSION,
        )
        # Do not hold a SQLite write transaction while waiting on an external model.
        # All required ORM objects use the app's expire_on_commit=False session.
        db.commit()

        scores: dict[str, ModelRecommendation] = {}
        input_tokens = output_tokens = successful = failed = requests = 0
        errors: list[str] = []
        model_enabled = bool(settings.deepseek_api_key) or provider is not None
        if model_enabled and shortlist:
            try:
                check_token_budget(db, settings, now)
                db.commit()
                active_provider = provider or DeepSeekRecommendationProvider(settings)
                for start in range(0, len(shortlist), RERANK_CHUNK_SIZE):
                    chunk = shortlist[start : start + RERANK_CHUNK_SIZE]
                    papers = [item.paper for item in chunk]
                    outcome = _rerank_chunk_adaptively(
                        active_provider,
                        preferences,
                        papers,
                    )
                    scores.update(outcome.scores)
                    successful += outcome.successful
                    failed += outcome.failed
                    requests += outcome.requests
                    input_tokens += outcome.input_tokens
                    output_tokens += outcome.output_tokens
                    errors.extend(outcome.errors)
                    if outcome.model:
                        batch.model = outcome.model
            except (PreferenceUnavailableError, RecommendationUnavailableError) as exc:
                failed = len(shortlist)
                errors.append(str(exc))
        elif shortlist:
            failed = len(shortlist)

        local_by_id = {item.paper.id: item for item in shortlist}
        ordered: list[tuple[Paper, float, str, float]] = []
        for item in shortlist:
            model_score = scores.get(item.paper.id)
            if model_score:
                final_score = 0.70 * model_score.preference_score + 0.30 * item.final_score
                ordered.append(
                    (item.paper, final_score, model_score.reason, model_score.preference_score)
                )
            else:
                reason = "本地粗排：研究词项、近期性与来源信号"
                ordered.append((item.paper, item.final_score, reason, 0.0))
        ordered.sort(
            key=lambda item: (
                item[1],
                local_by_id[item[0].id].bm25_score,
                _aware(item[0].discovered_at),
                item[0].id,
            ),
            reverse=True,
        )
        selected = ordered[:requested_count]
        db.add(batch)
        db.flush()
        for position, (paper, final_score, reason, llm_score) in enumerate(selected, start=1):
            db.add(
                RecommendationItem(
                    batch_id=batch.id,
                    paper_id=paper.id,
                    position=position,
                    llm_score=llm_score,
                    final_score=final_score,
                    reason=reason,
                )
            )
        batch.rerank_success_count = successful
        batch.rerank_fallback_count = failed
        batch.selected_count = len(selected)
        batch.fallback_used = failed > 0
        batch.error = "; ".join(dict.fromkeys(errors))[:2000]
        if requests:
            db.add(
                ApiUsage(
                    service="deepseek",
                    operation="featured_rerank",
                    request_count=requests,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    created_at=now,
                )
            )
        db.commit()
        db.refresh(batch)
        return batch
    finally:
        _generation_lock.release()


def generate_recommendation_batch_in_background() -> None:
    with SessionLocal() as db:
        generate_recommendation_batch(db)
