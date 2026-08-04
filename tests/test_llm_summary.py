from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from arxiv_updater.auth import create_user
from arxiv_updater.config import Settings
from arxiv_updater.models import Paper
from arxiv_updater.services.llm import (
    LLMProvider,
    OpenAICompatibleProvider,
    SummaryContent,
    SummaryQuotaError,
    SummaryResult,
    SummaryUnavailableError,
    generate_summary,
)


class FakeProvider(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, paper) -> SummaryResult:
        self.calls += 1
        return SummaryResult(
            content=SummaryContent(
                tldr=f"This paper studies {paper.title}.",
                contributions=["Introduces a testable method.", "Reports a benchmark."],
                methods="The abstract describes a numerical comparison.",
                limitations="Not stated in the abstract.",
            ),
            model="fake-summary-model",
            input_tokens=120,
            output_tokens=80,
        )


class StubCompletions:
    def __init__(self, content: str, finish_reason: str = "stop") -> None:
        self.kwargs = None
        self.response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason=finish_reason,
                    message=SimpleNamespace(content=content),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _provider_with_stub(stub: StubCompletions, **settings_overrides):
    settings = Settings(
        deepseek_api_key="test-key",
        llm_model="deepseek-v4-flash",
        **settings_overrides,
    )
    provider = OpenAICompatibleProvider(settings)
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return provider


def _provider_paper() -> Paper:
    return Paper(
        title="A structured quantum summary",
        normalized_title="a structured quantum summary",
        abstract="We present and test a quantum method.",
    )


def _paper(models):
    return models.Paper(
        title="A cached quantum summary",
        normalized_title="a cached quantum summary",
        abstract="We compare two numerical methods for a quantum system and report a benchmark.",
        authors_text="Alice Example",
        first_author="alice example",
        published_at=datetime.now(UTC),
        categories=["quant-ph"],
    )


def test_summary_is_shared_cached_and_usage_is_counted_once(app_client):
    _, session_factory, models = app_client
    provider = FakeProvider()
    settings = Settings(deepseek_api_key="test-key")
    with session_factory() as db:
        first = create_user(db, "summary-one@example.com", "a-strong-password", "First")
        second = create_user(db, "summary-two@example.com", "a-strong-password", "Second")
        paper = _paper(models)
        db.add(paper)
        db.commit()

        generated = generate_summary(db, first, paper, provider=provider, settings=settings)
        cached = generate_summary(db, second, paper, provider=provider, settings=settings)

        assert cached.id == generated.id
        assert generated.contributions == [
            "Introduces a testable method.",
            "Reports a benchmark.",
        ]
        assert provider.calls == 1
        usage_count = db.scalar(select(func.count()).select_from(models.ApiUsage))
        assert usage_count == 1


def test_deepseek_v4_summary_disables_thinking_by_default():
    stub = StubCompletions(
        '{"tldr":"A result.","contributions":["One."],'
        '"methods":"A method.","limitations":"Not stated in the abstract."}'
    )
    provider = _provider_with_stub(stub)

    result = provider.summarize(_provider_paper())

    assert result.content.tldr == "A result."
    assert stub.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


def test_truncated_model_output_has_specific_retry_error():
    provider = _provider_with_stub(
        StubCompletions('{"tldr":"cut off",', finish_reason="length")
    )

    with pytest.raises(SummaryUnavailableError, match="输出被截断"):
        provider.summarize(_provider_paper())


def test_summary_weekly_user_quota_is_enforced(app_client):
    _, session_factory, models = app_client
    provider = FakeProvider()
    settings = Settings(
        deepseek_api_key="test-key",
        summary_user_weekly_limit=0,
    )
    with session_factory() as db:
        user = create_user(db, "limited@example.com", "a-strong-password", "Limited")
        paper = _paper(models)
        db.add(paper)
        db.commit()

        with pytest.raises(SummaryQuotaError, match="本周"):
            generate_summary(db, user, paper, provider=provider, settings=settings)
        assert provider.calls == 0


def test_interested_action_reports_missing_api_key_inline(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        create_user(db, "no-key@example.com", "a-strong-password", "No Key")
        paper = _paper(models)
        db.add(paper)
        db.commit()
        paper_id = paper.id

    client.post("/login", data={"email": "no-key@example.com", "password": "a-strong-password"})
    response = client.post(f"/papers/{paper_id}/interested")

    assert response.status_code == 200
    assert "管理员尚未配置 DeepSeek API key" in response.text
    assert "重试" in response.text
