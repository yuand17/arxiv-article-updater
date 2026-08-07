"""Weekly, structured DeepSeek preference profiles for the one local reader."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import SessionLocal
from ..models import ApiUsage, AppPreferences, Interaction, InteractionKind, Paper, utcnow

PROFILE_PROMPT_VERSION = "v1"
DEFAULT_PROFILE = {
    "topics": [],
    "methods": [],
    "physical_systems": [],
    "preferred_authors": [],
    "avoid_topics": [],
    "summary": "",
}


class PreferenceUnavailableError(RuntimeError):
    """The preference profile is retained when the external model is unavailable."""


class PreferenceContent(BaseModel):
    topics: list[str] = Field(default_factory=list, max_length=12)
    methods: list[str] = Field(default_factory=list, max_length=12)
    physical_systems: list[str] = Field(default_factory=list, max_length=12)
    preferred_authors: list[str] = Field(default_factory=list, max_length=12)
    avoid_topics: list[str] = Field(default_factory=list, max_length=12)
    summary: str = Field(default="", max_length=1200)


@dataclass(frozen=True, slots=True)
class PreferenceResult:
    content: PreferenceContent
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class PreferenceProvider(ABC):
    @abstractmethod
    def build_profile(self, manual_interests: str, papers: list[dict]) -> PreferenceResult:
        """Return a conservative structured reading-preference profile."""


class DeepSeekPreferenceProvider(PreferenceProvider):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.llm_base_url,
            timeout=45.0,
            max_retries=1,
        )

    def build_profile(self, manual_interests: str, papers: list[dict]) -> PreferenceResult:
        prompt = json.dumps(
            {
                "manual_interests": manual_interests,
                "interaction_papers": papers,
                "required_json_fields": list(DEFAULT_PROFILE),
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
                            "You infer a research reader's preferences only from the supplied "
                            "paper titles, authors, abstracts, interaction signals, and manual "
                            "interests. Return JSON only. Do not invent facts or personal data. "
                            "Use concise Chinese in summary and short English technical labels "
                            "in lists."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=1200,
                extra_body=extra_body,
            )
        except Exception as exc:
            raise PreferenceUnavailableError("DeepSeek 偏好画像服务暂时不可用") from exc
        try:
            raw = response.choices[0].message.content or "{}"
            content = PreferenceContent.model_validate(json.loads(raw))
        except (AttributeError, IndexError, json.JSONDecodeError, ValidationError) as exc:
            raise PreferenceUnavailableError("DeepSeek 返回的偏好画像格式无效") from exc
        usage = response.usage
        return PreferenceResult(
            content=content,
            model=self.settings.llm_model,
            input_tokens=int(usage.prompt_tokens if usage else 0),
            output_tokens=int(usage.completion_tokens if usage else 0),
        )


def get_preferences(db: Session) -> AppPreferences:
    preferences = db.get(AppPreferences, 1)
    if preferences is None:
        preferences = AppPreferences(id=1, profile_json=DEFAULT_PROFILE.copy())
        db.add(preferences)
        db.flush()
    return preferences


def mark_preferences_dirty(db: Session, *, now: datetime | None = None) -> None:
    preferences = get_preferences(db)
    preferences.profile_dirty_since = now or utcnow()


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def check_token_budget(db: Session, settings: Settings, now: datetime) -> None:
    tokens = db.scalar(
        select(func.coalesce(func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens), 0)).where(
            ApiUsage.service == "deepseek", ApiUsage.created_at >= _month_start(now)
        )
    )
    if int(tokens or 0) >= settings.llm_monthly_token_budget:
        raise PreferenceUnavailableError("本月 DeepSeek token 预算已用完")


def _signal_priority(kind: InteractionKind) -> int:
    return {
        InteractionKind.SAVED: 4,
        InteractionKind.FULLTEXT: 3,
        InteractionKind.ABSTRACT_VIEWED: 2,
        InteractionKind.DISMISSED: 1,
    }[kind]


def _profile_input(db: Session) -> list[dict]:
    rows = db.execute(
        select(Interaction, Paper)
        .join(Paper, Interaction.paper_id == Paper.id)
        .order_by(Interaction.created_at.desc())
        .limit(800)
    ).all()
    by_paper: dict[str, tuple[Interaction, Paper, list[str]]] = {}
    for interaction, paper in rows:
        existing = by_paper.get(paper.id)
        if existing is None:
            by_paper[paper.id] = (interaction, paper, [interaction.kind.value])
            continue
        strongest, existing_paper, signals = existing
        signals.append(interaction.kind.value)
        if _signal_priority(interaction.kind) > _signal_priority(strongest.kind):
            by_paper[paper.id] = (interaction, existing_paper, signals)
    selected = sorted(
        by_paper.values(),
        key=lambda item: (_signal_priority(item[0].kind), item[0].created_at),
        reverse=True,
    )[:500]
    return [
        {
            "paper_id": paper.id,
            "title": paper.title,
            "authors": paper.authors_text,
            "abstract": paper.abstract or "unavailable",
            "signals": sorted(set(signals)),
            "latest_interaction_at": interaction.created_at.isoformat(),
        }
        for interaction, paper, signals in selected
    ]


def profile_is_due(preferences: AppPreferences, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if preferences.profile_generated_at is None:
        return True
    generated = preferences.profile_generated_at
    generated = generated if generated.tzinfo else generated.replace(tzinfo=UTC)
    dirty = preferences.profile_dirty_since
    if dirty is None:
        return False
    dirty = dirty if dirty.tzinfo else dirty.replace(tzinfo=UTC)
    return dirty >= generated and now - generated >= timedelta(days=7)


def rebuild_preference_profile(
    db: Session,
    *,
    provider: PreferenceProvider | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> AppPreferences:
    """Persist a new profile only after strict JSON validation succeeds."""

    now = now or datetime.now(UTC)
    settings = settings or get_settings()
    preferences = get_preferences(db)
    papers = _profile_input(db)
    if not force and not profile_is_due(preferences, now=now):
        return preferences
    if not settings.deepseek_api_key and provider is None:
        raise PreferenceUnavailableError("尚未配置 DeepSeek API key")
    check_token_budget(db, settings, now)
    result = (provider or DeepSeekPreferenceProvider(settings)).build_profile(
        preferences.manual_interests,
        papers,
    )
    profile_json = result.content.model_dump()
    preferences.profile_summary = result.content.summary
    preferences.profile_json = {**DEFAULT_PROFILE, **profile_json}
    preferences.profile_model = result.model
    preferences.profile_prompt_version = PROFILE_PROMPT_VERSION
    preferences.profile_generated_at = now
    preferences.profile_interaction_count = len(papers)
    preferences.profile_dirty_since = None
    db.add(
        ApiUsage(
            service="deepseek",
            operation="preference_profile",
            request_count=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            created_at=now,
        )
    )
    db.commit()
    db.refresh(preferences)
    return preferences


def rebuild_preference_profile_in_background(*, force: bool = False) -> None:
    with SessionLocal() as db:
        rebuild_preference_profile(db, force=force)
