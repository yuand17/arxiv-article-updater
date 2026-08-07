from datetime import UTC, datetime, timedelta

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
    ModelRecommendation,
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


def test_recommendation_batch_scores_all_candidates_in_chunks_and_keeps_fifty(app_client):
    _, session_factory, models = app_client
    provider = FakeRecommendationProvider(include_unknown=True)
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum"))
        papers = [_paper(models, index) for index in range(55)]
        db.add_all(papers)
        db.commit()
        batch = generate_recommendation_batch(
            db,
            provider=provider,
            settings=Settings(deepseek_api_key="test-key"),
        )
        items = sorted(batch.items, key=lambda item: item.position)
        assert len(items) == 55
        assert len(provider.calls) == 2
        assert all(len(call) <= 50 for call in provider.calls)
        assert items[0].reason.startswith("匹配")
        assert batch.fallback_used is False
        assert len(rank_papers(db, view="weekly")) >= 50
        assert db.query(models.ApiUsage).filter_by(operation="recommendation_rerank").count() == 1


def test_missing_deepseek_key_generates_deterministic_fallback_batch(app_client):
    _, session_factory, models = app_client
    with session_factory() as db:
        db.add(models.AppPreferences(id=1, manual_interests="quantum"))
        db.add_all([_paper(models, index) for index in range(3)])
        db.commit()
        batch = generate_recommendation_batch(db, settings=Settings(deepseek_api_key=""))
        assert batch.fallback_used is True
        assert batch.model == "local-fallback"
        assert len(batch.items) == 3
        assert all("本地回退" in item.reason for item in batch.items)


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
