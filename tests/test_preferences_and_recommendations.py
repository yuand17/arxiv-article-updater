import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from arxiv_updater.config import Settings
from arxiv_updater.services.interactions import record_interaction
from arxiv_updater.services.preferences import (
    PreferenceContent,
    PreferenceProvider,
    PreferenceResult,
    rebuild_preference_profile,
)
from arxiv_updater.services.ranking import rank_papers
from arxiv_updater.services.recommendations import (
    DeepSeekRecommendationProvider,
    ModelRecommendation,
    RecommendationOutputTruncatedError,
    RecommendationProvider,
    RerankResult,
    generate_recommendation_batch,
)


class FakePreferenceProvider(PreferenceProvider):
    def __init__(self) -> None:
        self.received: list[dict] = []

    def build_profile(self, manual_interests: str, papers: list[dict]) -> PreferenceResult:
        self.received = papers
        return PreferenceResult(
            content=PreferenceContent(
                topics=["quantum error correction"],
                methods=["tensor networks"],
                physical_systems=["many-body systems"],
                preferred_authors=["Alice Example"],
                avoid_topics=["unrelated biology"],
                summary="偏好量子纠错、张量网络与多体系统。",
            ),
            model="fake-preference-model",
            input_tokens=120,
            output_tokens=80,
        )


class FakeRecommendationProvider(RecommendationProvider):
    def __init__(self, *, include_unknown: bool = False) -> None:
        self.include_unknown = include_unknown
        self.calls: list[list[str]] = []

    def rerank(self, preferences, papers) -> RerankResult:
        self.calls.append([paper.id for paper in papers])
        items = [
            ModelRecommendation(
                paper_id=paper.id,
                preference_score=100 - index,
                reason=f"匹配 {paper.title}",
            )
            for index, paper in enumerate(papers)
        ]
        if self.include_unknown:
            items.append(
                ModelRecommendation(paper_id="not-a-paper", preference_score=100, reason="bad")
            )
        return RerankResult(items=items, model="fake-reranker", input_tokens=30, output_tokens=20)


class ConcurrentWriteProvider(FakeRecommendationProvider):
    def __init__(self, session_factory, models) -> None:
        super().__init__()
        self.session_factory = session_factory
        self.models = models

    def rerank(self, preferences, papers) -> RerankResult:
        with self.session_factory() as concurrent:
            concurrent.add(
                self.models.ApiUsage(
                    service="test",
                    operation="concurrent_during_rerank",
                )
            )
            concurrent.commit()
        return super().rerank(preferences, papers)


class LengthLimitedRecommendationProvider(RecommendationProvider):
    def __init__(self, *, maximum_batch_size: int = 25) -> None:
        self.maximum_batch_size = maximum_batch_size
        self.calls: list[list[str]] = []

    def rerank(self, preferences, papers) -> RerankResult:
        self.calls.append([paper.id for paper in papers])
        if len(papers) > self.maximum_batch_size:
            raise RecommendationOutputTruncatedError(
                "length",
                input_tokens=100,
                output_tokens=200,
            )
        return RerankResult(
            items=[
                ModelRecommendation(
                    paper_id=paper.id,
                    preference_score=100 - index,
                    reason=f"匹配 {paper.title}",
                )
                for index, paper in enumerate(papers)
            ],
            model="split-reranker",
            input_tokens=30,
            output_tokens=20,
        )


def _paper(models, index: int, *, days_old: int = 1):
    return models.Paper(
        title=f"Quantum candidate {index}",
        normalized_title=f"quantum candidate {index}",
        abstract=f"An abstract about quantum error correction and tensor networks {index}.",
        abstract_source="arxiv",
        abstract_status="available",
        authors_text="Alice Example",
        first_author="alice example",
        published_at=datetime.now(UTC) - timedelta(days=days_old),
        discovered_at=datetime.now(UTC) - timedelta(minutes=index),
        categories=["quant-ph"],
    )


def test_preference_profile_uses_title_authors_abstract_and_reading_signals(app_client):
    _, session_factory, models = app_client
    provider = FakePreferenceProvider()
    with session_factory() as db:
        paper = _paper(models, 1)
        db.add(paper)
        db.commit()
        preferences = models.AppPreferences(id=1, manual_interests="quantum information")
        db.add(preferences)
        db.commit()
        record_interaction(db, paper.id, models.InteractionKind.ABSTRACT_VIEWED)
        profile = rebuild_preference_profile(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key"),
            force=True,
        )
        assert profile.profile_json["topics"] == ["quantum error correction"]
        assert profile.profile_summary == "偏好量子纠错、张量网络与多体系统。"
        assert provider.received[0]["title"] == paper.title
        assert provider.received[0]["authors"] == "Alice Example"
        assert "tensor networks" in provider.received[0]["abstract"]
        assert provider.received[0]["signals"] == ["abstract_viewed"]
        assert db.query(models.ApiUsage).filter_by(operation="preference_profile").count() == 1


def test_recommendation_batch_reranks_shortlist_and_keeps_configured_count(app_client):
    _, session_factory, models = app_client
    provider = FakeRecommendationProvider(include_unknown=True)
    with session_factory() as db:
        db.add(
            models.AppPreferences(
                id=1, manual_interests="quantum", featured_paper_count=17
            )
        )
        papers = [_paper(models, index) for index in range(55)]
        db.add_all(papers)
        db.commit()
        batch = generate_recommendation_batch(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key"),
        )
        items = sorted(batch.items, key=lambda item: item.position)
        assert len(items) == 17
        assert len(provider.calls) == 2
        assert all(len(call) <= 50 for call in provider.calls)
        assert items[0].reason.startswith("匹配")
        assert batch.fallback_used is False
        assert len(rank_papers(db, view="featured")) == 17
        assert db.query(models.ApiUsage).filter_by(operation="featured_rerank").count() == 1


def test_length_limited_rerank_splits_chunks_without_local_fallback(app_client):
    _, session_factory, models = app_client
    provider = LengthLimitedRecommendationProvider()
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum", featured_paper_count=17))
        db.add_all([_paper(models, index) for index in range(55)])
        db.commit()

        batch = generate_recommendation_batch(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key"),
        )

        assert [len(call) for call in provider.calls] == [50, 25, 25, 5]
        assert batch.rerank_success_count == batch.shortlist_count == 55
        assert batch.rerank_fallback_count == 0
        assert batch.fallback_used is False
        assert batch.error == ""
        usage = db.query(models.ApiUsage).filter_by(operation="featured_rerank").one()
        assert usage.request_count == 4
        assert usage.input_tokens == 190
        assert usage.output_tokens == 260


def test_deepseek_rerank_uses_generous_output_budget_and_more_abstract(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    captured: dict = {}

    class CapturingCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            prompt = json.loads(kwargs["messages"][1]["content"])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content=json.dumps(
                                {
                                    "items": [
                                        {
                                            "paper_id": paper["paper_id"],
                                            "preference_score": 80,
                                            "confidence": 0.9,
                                            "reason": "匹配研究偏好",
                                        }
                                        for paper in prompt["papers"]
                                    ]
                                }
                            )
                        ),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=1_000, completion_tokens=4_000),
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=CapturingCompletions())
    )
    monkeypatch.setattr(
        "arxiv_updater.services.recommendations.OpenAI",
        lambda **_kwargs: fake_client,
    )
    with session_factory() as db:
        preferences = models.AppPreferences(id=1, manual_interests="quantum")
        papers = [_paper(models, index) for index in range(50)]
        papers[0].abstract = "q" * 5_000
        db.add(preferences)
        db.add_all(papers)
        db.commit()

        result = DeepSeekRecommendationProvider(
            Settings(deepseek_api_key="test-key")
        ).rerank(preferences, papers)

        prompt = json.loads(captured["messages"][1]["content"])
        assert captured["max_tokens"] == 10_000
        assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
        assert len(prompt["papers"][0]["abstract"]) == 4_000
        assert len(result.items) == 50


def test_deepseek_length_response_carries_usage_for_adaptive_split(
    app_client, monkeypatch
):
    _, session_factory, models = app_client
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            finish_reason="length",
                            message=SimpleNamespace(content="{}"),
                        )
                    ],
                    usage=SimpleNamespace(
                        prompt_tokens=4_168,
                        completion_tokens=10_000,
                    ),
                )
            )
        )
    )
    monkeypatch.setattr(
        "arxiv_updater.services.recommendations.OpenAI",
        lambda **_kwargs: fake_client,
    )
    with session_factory() as db:
        preferences = models.AppPreferences(id=1, manual_interests="quantum")
        paper = _paper(models, 1)
        db.add_all([preferences, paper])
        db.commit()

        with pytest.raises(RecommendationOutputTruncatedError) as captured:
            DeepSeekRecommendationProvider(
                Settings(deepseek_api_key="test-key")
            ).rerank(preferences, [paper])

        assert captured.value.input_tokens == 4_168
        assert captured.value.output_tokens == 10_000


def test_default_monthly_deepseek_budget_is_relaxed(monkeypatch):
    monkeypatch.delenv("LLM_MONTHLY_TOKEN_BUDGET", raising=False)
    assert Settings(_env_file=None).llm_monthly_token_budget == 50_000_000


def test_missing_deepseek_key_generates_deterministic_fallback_batch(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum"))
        db.add_all([_paper(models, index) for index in range(3)])
        db.commit()
        batch = generate_recommendation_batch(db, settings=Settings(deepseek_api_key=""))
        assert batch.fallback_used is True
        assert batch.model == "local-bm25"
        assert len(batch.items) == 3
        assert all("本地粗排" in item.reason for item in batch.items)


def test_rerank_does_not_hold_a_sqlite_write_lock(app_client):
    _, session_factory, models = app_client
    provider = ConcurrentWriteProvider(session_factory, models)
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum"))
        db.add(_paper(models, 1))
        db.commit()

        batch = generate_recommendation_batch(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key"),
        )

        assert batch.status == "success"
        assert (
            db.query(models.ApiUsage)
            .filter_by(operation="concurrent_during_rerank")
            .count()
            == 1
        )


def test_recommendation_rerank_respects_the_monthly_deepseek_token_budget(app_client):
    _, session_factory, models = app_client
    provider = FakeRecommendationProvider()
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum"))
        db.add(_paper(models, 1))
        db.add(
            models.ApiUsage(
                service="deepseek",
                operation="previous_request",
                input_tokens=10,
                output_tokens=0,
            )
        )
        db.commit()
        batch = generate_recommendation_batch(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key", llm_monthly_token_budget=10),
        )
        assert batch.fallback_used is True
        assert provider.calls == []
        assert batch.error
