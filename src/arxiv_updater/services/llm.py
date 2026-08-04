import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import ApiUsage, Paper, PaperSummary, User

PROMPT_VERSION = "v4"


class SummaryUnavailableError(RuntimeError):
    """A user-facing, retryable reason why a summary could not be generated."""


class SummaryQuotaError(SummaryUnavailableError):
    """Raised when the user or group summary budget is exhausted."""


class SummaryContent(BaseModel):
    tldr: str = Field(min_length=1)
    contributions: list[str] = Field(default_factory=list, max_length=3)
    methods: str = ""


@dataclass(frozen=True)
class SummaryResult:
    content: SummaryContent
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMProvider(ABC):
    @abstractmethod
    def summarize(self, paper: Paper) -> SummaryResult:
        """Summarize only the title and abstract of a paper."""


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = OpenAI(
            api_key=self.settings.deepseek_api_key,
            base_url=self.settings.llm_base_url,
            timeout=30.0,
            max_retries=1,
        )

    def summarize(self, paper: Paper) -> SummaryResult:
        prompt = (
            "Summarize the research paper below using only facts explicitly visible in its "
            "title and abstract. Return valid JSON with exactly these fields: tldr (one English "
            "sentence), contributions (an array of at most three concise English strings), and "
            "methods (a concise English overview). Do not infer missing results or methods.\n\n"
            f"Title: {paper.title}\n\nAbstract: {paper.abstract}"
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
                            "You are a careful scientific editor. Output JSON only and never "
                            "add information not present in the supplied abstract."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                max_tokens=700,
                extra_body=extra_body,
            )
        except Exception as exc:
            raise SummaryUnavailableError("AI 总结服务暂时不可用，请稍后重试。") from exc

        try:
            choice = response.choices[0]
            if choice.finish_reason == "length":
                raise SummaryUnavailableError("模型输出被截断，请重试或联系管理员。")
            raw = choice.message.content or "{}"
            content = SummaryContent.model_validate(json.loads(raw))
        except SummaryUnavailableError:
            raise
        except (json.JSONDecodeError, ValidationError, IndexError, AttributeError) as exc:
            raise SummaryUnavailableError("模型返回的总结格式无效，请稍后重试。") from exc

        usage = response.usage
        return SummaryResult(
            content=content,
            model=self.settings.llm_model,
            input_tokens=int(usage.prompt_tokens if usage else 0),
            output_tokens=int(usage.completion_tokens if usage else 0),
        )


def _week_start(now: datetime) -> datetime:
    start = now - timedelta(days=now.weekday())
    return start.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _check_quota(db: Session, user: User, settings: Settings, now: datetime) -> None:
    user_requests = db.scalar(
        select(func.coalesce(func.sum(ApiUsage.request_count), 0)).where(
            ApiUsage.service == "deepseek",
            ApiUsage.user_id == user.id,
            ApiUsage.operation == "summary",
            ApiUsage.created_at >= _week_start(now),
        )
    )
    if int(user_requests or 0) >= settings.summary_user_weekly_limit:
        raise SummaryQuotaError("你本周的 AI 总结额度已用完，请下周再试。")

    group_tokens = db.scalar(
        select(
            func.coalesce(func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens), 0)
        ).where(
            ApiUsage.service == "deepseek",
            ApiUsage.created_at >= _month_start(now),
        )
    )
    if int(group_tokens or 0) >= settings.llm_monthly_token_budget:
        raise SummaryQuotaError("本月全组 AI token 额度已用完，请联系管理员。")


def generate_summary(
    db: Session,
    user: User,
    paper: Paper,
    *,
    provider: LLMProvider | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> PaperSummary:
    cached = db.scalar(select(PaperSummary).where(PaperSummary.paper_id == paper.id))
    if cached:
        return cached
    if not paper.abstract.strip():
        raise SummaryUnavailableError("这篇论文没有 abstract，无法生成摘要级总结。")

    settings = settings or get_settings()
    if provider is None and not settings.deepseek_api_key:
        raise SummaryUnavailableError("管理员尚未配置 DeepSeek API key。")
    now = now or datetime.now(UTC)
    _check_quota(db, user, settings, now)

    provider = provider or OpenAICompatibleProvider(settings)
    result = provider.summarize(paper)
    summary = PaperSummary(
        paper_id=paper.id,
        tldr=result.content.tldr,
        contributions=result.content.contributions[:3],
        methods=result.content.methods,
        model=result.model,
        prompt_version=PROMPT_VERSION,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    db.add(summary)
    db.add(
        ApiUsage(
            service="deepseek",
            user_id=user.id,
            operation="summary",
            request_count=1,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            created_at=now,
        )
    )
    db.commit()
    db.refresh(summary)
    return summary
