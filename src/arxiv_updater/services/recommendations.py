"""Three-day DeepSeek recommendation batches with a deterministic offline fallback."""

import json
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
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
    utcnow,
)
from .preferences import PreferenceUnavailableError, check_token_budget, get_preferences

RECOMMENDATION_PROMPT_VERSION = "v1"


class RecommendationUnavailableError(RuntimeError):
    """A generation error that should fall back to local deterministic ranking."""


class ModelRecommendation(BaseModel):
    paper_id: str
    preference_score: float = Field(ge=0, le=100)
    reason: str = Field(min_length=1, max_length=300)


class ModelRecommendationResponse(BaseModel):
    items: list[ModelRecommendation] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RerankResult:
    items: list[ModelRecommendation]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


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
                "papers": [
                    {
                        "paper_id": paper.id,
                        "title": paper.title,
                        "authors": paper.authors_text,
                        "abstract": paper.abstract or "unavailable",
                    }
                    for paper in papers
                ],
                "required_output": {
                    "items": [
                        {
                            "paper_id": "one supplied ID",
                            "preference_score": "0 to 100",
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
                max_tokens=max(1200, len(papers) * 45),
                extra_body=extra_body,
            )
        except Exception as exc:
            raise RecommendationUnavailableError("DeepSeek 推荐排序服务暂时不可用") from exc
        try:
            raw = response.choices[0].message.content or "{}"
            parsed = ModelRecommendationResponse.model_validate(json.loads(raw))
        except (AttributeError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise RecommendationUnavailableError("DeepSeek 返回的推荐格式无效") from exc
        usage = response.usage
        return RerankResult(
            items=parsed.items,
            model=self.settings.llm_model,
            input_tokens=int(usage.prompt_tokens if usage else 0),
            output_tokens=int(usage.completion_tokens if usage else 0),
        )


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC) - timedelta(days=365)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _source_names(paper: Paper) -> set[str]:
    return {source.source for source in paper.sources}


def _excluded_ids(db: Session, cutoff: datetime) -> set[str]:
    return set(
        db.scalars(
            select(RecommendationItem.paper_id)
            .join(RecommendationBatch)
            .where(RecommendationBatch.generated_at >= cutoff)
        ).all()
    )


def recommendation_candidates(db: Session, *, now: datetime | None = None) -> list[Paper]:
    now = now or datetime.now(UTC)
    strict_start = now - timedelta(days=7)
    broad_start = now - timedelta(days=30)
    dismissed_ids = set(
        db.scalars(
            select(Interaction.paper_id).where(Interaction.kind == InteractionKind.DISMISSED)
        ).all()
    )
    previously_recommended = _excluded_ids(db, now - timedelta(days=30))
    papers = list(
        db.scalars(
            select(Paper)
            .options(selectinload(Paper.sources))
            .where(or_(Paper.published_at >= broad_start, Paper.discovered_at >= broad_start))
            .order_by(Paper.discovered_at.desc(), Paper.id.desc())
        )
        .unique()
        .all()
    )
    fresh = [
        paper
        for paper in papers
        if paper.id not in dismissed_ids
        and paper.id not in previously_recommended
        and (
            _aware(paper.published_at) >= strict_start
            or _aware(paper.discovered_at) >= strict_start
        )
    ]
    if len(fresh) >= 50:
        return fresh
    extras = [
        paper
        for paper in papers
        if paper.id not in dismissed_ids
        and paper.id not in previously_recommended
        and paper not in fresh
    ]
    return fresh + extras


def _profile_terms(preferences: AppPreferences) -> set[str]:
    profile = preferences.profile_json or {}
    raw_values = [preferences.manual_interests, preferences.profile_summary]
    for key in ("topics", "methods", "physical_systems", "preferred_authors"):
        raw_values.extend(str(value) for value in profile.get(key, []))
    return {token for value in raw_values for token in re.findall(r"[a-z0-9-]{3,}", value.lower())}


def _fallback_score(paper: Paper, preferences: AppPreferences, now: datetime) -> tuple[float, str]:
    age_days = max(
        0.0, (now - _aware(paper.published_at or paper.discovered_at)).total_seconds() / 86400
    )
    freshness = max(0.0, 100 - min(100, age_days * 12))
    sources = _source_names(paper)
    author = 100.0 if "scholar" in sources else 0.0
    scirate = min(100.0, 20 * math.log2(1 + paper.scites_count)) if paper.scites_count else 0.0
    journal = 100.0 if "journal" in sources else 0.0
    tokens = set(re.findall(r"[a-z0-9-]{3,}", f"{paper.title} {paper.abstract}".lower()))
    terms = _profile_terms(preferences)
    match = 100.0 * len(tokens & terms) / max(1, len(terms))
    score = 0.7 * match + 0.1 * freshness + 0.08 * author + 0.07 * scirate + 0.05 * journal
    reason = "本地回退：结合近期性、来源与研究关键词"
    return score, reason


def _validated_model_scores(
    result: RerankResult, supplied_ids: set[str]
) -> dict[str, ModelRecommendation]:
    valid: dict[str, ModelRecommendation] = {}
    for item in result.items:
        if item.paper_id in supplied_ids and item.paper_id not in valid:
            valid[item.paper_id] = item
    return valid


def recommendation_is_due(db: Session, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    latest = db.scalar(
        select(RecommendationBatch)
        .where(RecommendationBatch.status == "success")
        .order_by(RecommendationBatch.generated_at.desc())
    )
    if latest is None:
        return True
    generated = _aware(latest.generated_at)
    return now - generated >= timedelta(days=3)


def generate_recommendation_batch(
    db: Session,
    *,
    provider: RecommendationProvider | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> RecommendationBatch:
    """Persist a complete 3-day batch; no model result means deterministic fallback."""

    now = now or utcnow()
    settings = settings or get_settings()
    preferences = get_preferences(db)
    candidates = recommendation_candidates(db, now=now)
    batch = RecommendationBatch(
        generated_at=now,
        window_start=now - timedelta(days=7),
        window_end=now,
        profile_generated_at=preferences.profile_generated_at,
        model=settings.llm_model if settings.deepseek_api_key else "local-fallback",
        prompt_version=RECOMMENDATION_PROMPT_VERSION,
        status="success",
    )
    db.add(batch)
    db.flush()
    fallback_used = not bool(settings.deepseek_api_key) and provider is None
    scores: dict[str, ModelRecommendation] = {}
    input_tokens = 0
    output_tokens = 0
    if settings.deepseek_api_key or provider is not None:
        try:
            check_token_budget(db, settings, now)
            active_provider = provider or DeepSeekRecommendationProvider(settings)
            for start in range(0, len(candidates), 50):
                chunk = candidates[start : start + 50]
                result = active_provider.rerank(preferences, chunk)
                scores.update(_validated_model_scores(result, {paper.id for paper in chunk}))
                batch.model = result.model
                input_tokens += result.input_tokens
                output_tokens += result.output_tokens
            if len(scores) != len(candidates):
                raise RecommendationUnavailableError("模型未返回全部候选论文")
        except (PreferenceUnavailableError, RecommendationUnavailableError) as exc:
            fallback_used = True
            batch.error = str(exc)
            scores = {}

    ordered: list[tuple[Paper, float, str, float]] = []
    for paper in candidates:
        fallback_score, fallback_reason = _fallback_score(paper, preferences, now)
        model_score = scores.get(paper.id)
        if model_score:
            sources = _source_names(paper)
            age_days = max(
                0.0,
                (now - _aware(paper.published_at or paper.discovered_at)).total_seconds() / 86400,
            )
            freshness = max(0.0, 100 - min(100, age_days * 12))
            author = 100.0 if "scholar" in sources else 0.0
            scirate = (
                min(100.0, 20 * math.log2(1 + paper.scites_count)) if paper.scites_count else 0.0
            )
            journal = 100.0 if "journal" in sources else 0.0
            final_score = (
                0.7 * model_score.preference_score
                + 0.1 * freshness
                + 0.08 * author
                + 0.07 * scirate
                + 0.05 * journal
            )
            ordered.append((paper, final_score, model_score.reason, model_score.preference_score))
        else:
            ordered.append((paper, fallback_score, fallback_reason, 0.0))
    ordered.sort(
        key=lambda item: (item[1], _aware(item[0].discovered_at), item[0].id), reverse=True
    )
    for position, (paper, final_score, reason, llm_score) in enumerate(ordered, start=1):
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
    batch.fallback_used = fallback_used
    if scores:
        db.add(
            ApiUsage(
                service="deepseek",
                operation="recommendation_rerank",
                request_count=max(1, math.ceil(len(candidates) / 50)),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                created_at=now,
            )
        )
    db.commit()
    db.refresh(batch)
    return batch


def generate_recommendation_batch_in_background() -> None:
    with SessionLocal() as db:
        generate_recommendation_batch(db)
