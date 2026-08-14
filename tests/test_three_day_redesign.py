from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from arxiv_updater.config import Settings
from arxiv_updater.services.article_classification import classify_journal_candidate
from arxiv_updater.services.interactions import record_interaction
from arxiv_updater.services.journal_discovery import (
    JournalDiscoveryError,
    discover_journal,
    validate_public_https,
)
from arxiv_updater.services.ranking import rank_papers
from arxiv_updater.services.recommendations import (
    generate_recommendation_batch,
    local_rank_candidates,
    recommendation_is_due,
)
from arxiv_updater.services.retention import run_retention_cleanup
from arxiv_updater.sources.base import PaperCandidate
from arxiv_updater.sources.journals import JournalAdapter, JournalFeed, parse_crossref_works


def _candidate(
    title: str,
    *,
    document_type: str = "Article",
    abstract: str = "",
) -> PaperCandidate:
    return PaperCandidate(
        source="journal",
        external_id=title,
        title=title,
        authors=["A. Researcher"],
        abstract=abstract,
        canonical_url="https://publisher.example/articles/result",
        metadata={"document_type": document_type},
    )


@pytest.mark.parametrize(
    ("journal", "scope", "candidate", "accepted"),
    [
        ("Nature", "general", _candidate("Research news", document_type="News"), False),
        (
            "Nature",
            "general",
            _candidate("Quantum entanglement transition", abstract="A many-body quantum system."),
            True,
        ),
        (
            "Nature Communications",
            "general",
            _candidate("A clinical biomarker", abstract="A cohort study of patients."),
            False,
        ),
        ("Nature Physics", "physics", _candidate("Coherent photon transport"), True),
        (
            "Nature Physics",
            "physics",
            _candidate("Review of coherent transport", document_type="Review"),
            False,
        ),
        (
            "Physical Review Letters",
            "physics",
            _candidate("A new phase transition", document_type="Letter"),
            True,
        ),
        (
            "Physical Review Letters",
            "physics",
            _candidate("Erratum: A new phase transition", document_type="Erratum"),
            False,
        ),
    ],
)
def test_journal_research_and_physics_classification(journal, scope, candidate, accepted):
    result = classify_journal_candidate(candidate, journal_name=journal, scope_kind=scope)
    assert result.accepted is accepted
    assert result.reason


def _paper(models, title: str, abstract: str, discovered_at: datetime):
    paper = models.Paper(
        title=title,
        normalized_title=title.lower(),
        abstract=abstract,
        abstract_source="arxiv",
        abstract_status="available",
        authors_text="Alice Example",
        first_author="alice example",
        published_at=discovered_at,
        discovered_at=discovered_at,
        categories=["quant-ph"],
    )
    paper.sources.append(
        models.PaperSource(source="arxiv", external_id=title, metadata_json={})
    )
    return paper


def test_bm25_is_repeatable_and_weights_title_above_abstract(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        preferences = models.AppPreferences(id=1, manual_interests="topological qubit")
        title_match = _paper(models, "Topological qubit dynamics", "generic result", now)
        abstract_match = _paper(
            models,
            "Generic dynamics",
            "topological qubit dynamics",
            now - timedelta(minutes=1),
        )
        common = _paper(models, "Study model result", "study model result " * 20, now)
        db.add_all([preferences, title_match, abstract_match, common])
        db.commit()

        first = local_rank_candidates(db, [title_match, abstract_match, common], preferences, now)
        second = local_rank_candidates(db, [title_match, abstract_match, common], preferences, now)

        assert [item.paper.id for item in first] == [item.paper.id for item in second]
        scores = {item.paper.id: item.bm25_score for item in first}
        assert scores[title_match.id] > scores[abstract_match.id] > scores[common.id]


def test_bm25_negative_feedback_penalizes_avoided_terms(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        preferences = models.AppPreferences(id=1, manual_interests="quantum")
        positive = _paper(models, "Quantum transport", "generic", now)
        neutral = _paper(models, "Neutral transport", "generic", now)
        avoided = _paper(models, "Oncology transport", "generic", now)
        db.add_all([preferences, positive, neutral, avoided])
        db.commit()
        record_interaction(db, avoided.id, models.InteractionKind.DISMISSED)

        ranked = local_rank_candidates(db, [positive, neutral, avoided], preferences, now)
        scores = {item.paper.id: item.final_score for item in ranked}

        assert scores[positive.id] > scores[neutral.id] > scores[avoided.id]


def test_three_day_batch_uses_dynamic_count_and_never_backfills_old_papers(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        db.add(
            models.AppPreferences(
                id=1, manual_interests="quantum", featured_paper_count=17
            )
        )
        fresh = [
            _paper(models, f"Fresh quantum paper {index}", "quantum result", now)
            for index in range(20)
        ]
        old = _paper(
            models,
            "Old quantum paper",
            "quantum result",
            now - timedelta(days=4),
        )
        dismissed = _paper(models, "Dismissed quantum paper", "quantum", now)
        db.add_all([*fresh, old, dismissed])
        db.commit()
        record_interaction(db, dismissed.id, models.InteractionKind.DISMISSED)

        batch = generate_recommendation_batch(
            db,
            settings=Settings(deepseek_api_key=""),
            now=now,
        )

        assert batch.requested_count == 17
        assert batch.candidate_count == 20
        assert batch.filtered_count == 1
        assert batch.selected_count == 17
        assert len(batch.items) == 17
        assert old.id not in {item.paper_id for item in batch.items}
        assert batch.window_start == (now - timedelta(days=3)).replace(tzinfo=None)


def test_legacy_batch_is_not_featured_and_does_not_delay_first_three_day_batch(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        paper = _paper(models, "Legacy weekly paper", "quantum", now)
        db.add(paper)
        db.flush()
        legacy = models.RecommendationBatch(
            generated_at=now,
            window_start=now - timedelta(days=7),
            window_end=now,
            status="success",
            ranking_version="",
        )
        db.add(legacy)
        db.flush()
        db.add(
            models.RecommendationItem(
                batch_id=legacy.id,
                paper_id=paper.id,
                position=1,
            )
        )
        db.commit()

        assert rank_papers(db, view="featured", now=now) == []
        assert recommendation_is_due(db, now=now) is True

        current = generate_recommendation_batch(
            db,
            settings=Settings(deepseek_api_key=""),
            now=now,
        )
        assert current.selected_count == 1
        assert current.items[0].paper_id == paper.id


def test_cleanup_protects_every_interaction_and_writes_seen_record(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        untouched = _paper(models, "Untouched", "old", now - timedelta(days=10))
        untouched.sources[0].source = "scholar"
        saved = _paper(models, "Saved", "old", now - timedelta(days=10))
        abstracted = _paper(models, "Abstracted", "old", now - timedelta(days=10))
        opened = _paper(models, "Opened", "old", now - timedelta(days=10))
        dismissed = _paper(models, "Dismissed", "old", now - timedelta(days=10))
        recent = _paper(models, "Recent", "new", now - timedelta(days=8))
        db.add_all([untouched, saved, abstracted, opened, dismissed, recent])
        db.commit()
        untouched_id = untouched.id
        protected_ids = [saved.id, abstracted.id, opened.id, dismissed.id, recent.id]
        for paper, kind in (
            (saved, models.InteractionKind.SAVED),
            (abstracted, models.InteractionKind.ABSTRACT_VIEWED),
            (opened, models.InteractionKind.FULLTEXT),
            (dismissed, models.InteractionKind.DISMISSED),
        ):
            record_interaction(db, paper.id, kind)

        run = run_retention_cleanup(db, now=now)

        assert run.status == "success"
        assert run.deleted_count == 1
        assert db.get(models.Paper, untouched_id) is None
        assert all(db.get(models.Paper, paper_id) is not None for paper_id in protected_ids)
        seen = db.scalar(
            select(models.SeenSourceItem).where(
                models.SeenSourceItem.external_id == "Untouched"
            )
        )
        assert seen is not None
        assert seen.outcome == "cleaned"
        assert seen.paper_id is None


def test_cleanup_protects_only_the_latest_three_successful_batches(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        papers = [
            _paper(models, f"Batch paper {index}", "old", now - timedelta(days=20))
            for index in range(4)
        ]
        db.add_all(papers)
        db.flush()
        for index, paper in enumerate(papers):
            generated_at = now - timedelta(days=4 - index)
            batch = models.RecommendationBatch(
                generated_at=generated_at,
                window_start=generated_at - timedelta(days=3),
                window_end=generated_at,
                status="success",
            )
            db.add(batch)
            db.flush()
            db.add(
                models.RecommendationItem(
                    batch_id=batch.id,
                    paper_id=paper.id,
                    position=1,
                )
            )
        db.commit()
        paper_ids = [paper.id for paper in papers]

        run = run_retention_cleanup(db, now=now)

        assert run.status == "success"
        assert db.get(models.Paper, paper_ids[0]) is None
        assert all(db.get(models.Paper, paper_id) is not None for paper_id in paper_ids[1:])


def test_cleanup_rolls_back_all_paper_changes_on_failure(app_client, monkeypatch):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        paper = _paper(models, "Rollback paper", "old", now - timedelta(days=20))
        db.add(paper)
        db.commit()
        paper_id = paper.id
        original_execute = db.execute

        def fail_paper_delete(statement, *args, **kwargs):
            table = getattr(statement, "table", None)
            if getattr(statement, "is_delete", False) and getattr(table, "name", "") == "papers":
                raise RuntimeError("simulated cleanup failure")
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db, "execute", fail_paper_delete)
        run = run_retention_cleanup(db, now=now)

        assert run.status == "failed"
        assert "simulated cleanup failure" in run.error
        assert db.get(models.Paper, paper_id) is not None
        assert db.scalar(select(models.SeenSourceItem)) is None


def test_cleanup_handles_naive_sqlite_batch_timestamps_after_reload(app_client):
    _, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        old_batch = models.RecommendationBatch(
            generated_at=now - timedelta(days=40),
            window_start=now - timedelta(days=43),
            window_end=now - timedelta(days=40),
            status="success",
        )
        current_batch = models.RecommendationBatch(
            generated_at=now,
            window_start=now - timedelta(days=3),
            window_end=now,
            status="success",
        )
        db.add_all([old_batch, current_batch])
        db.commit()
        old_id = old_batch.id
        current_id = current_batch.id

    with session_factory() as db:
        assert db.get(models.RecommendationBatch, old_id) is not None
        run = run_retention_cleanup(db, now=now)

        assert run.status == "success"
        assert db.get(models.RecommendationBatch, old_id) is None
        assert db.get(models.RecommendationBatch, current_id) is not None


def test_crossref_works_parser_keeps_structured_research_evidence():
    payload = {
        "message": {
            "items": [
                {
                    "title": ["Quantum transport across a moire interface"],
                    "DOI": "10.1234/PHYSICS.1",
                    "published-online": {"date-parts": [[2026, 8, 8]]},
                    "type": "journal-article",
                    "subtype": "article",
                    "subject": ["Condensed Matter Physics"],
                    "author": [{"given": "Ada", "family": "Lovelace"}],
                    "resource": {
                        "primary": {"URL": "https://journal.example/articles/s1"}
                    },
                }
            ]
        }
    }

    papers = parse_crossref_works(
        payload,
        JournalFeed("Example Physics", "https://api.crossref.org/works", "1234-5678"),
    )

    assert len(papers) == 1
    assert papers[0].external_id == "10.1234/physics.1"
    assert papers[0].authors == ["Ada Lovelace"]
    assert papers[0].metadata["document_type"] == "article"
    assert papers[0].metadata["subjects"] == ["Condensed Matter Physics"]


def test_official_rss_is_enriched_by_crossref_without_importing_crossref_only_items():
    rss = """<?xml version="1.0"?><rss version="2.0"
      xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Science</title>
      <item><title>Quantum collision result</title>
      <link>https://www.science.org/doi/10.1126/science.test1</link>
      <guid>10.1126/science.test1</guid><dc:type>Research Article</dc:type>
      <pubDate>Sun, 09 Aug 2026 10:00:00 GMT</pubDate>
      <description>Issue citation without an abstract.</description></item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [
                            {
                                "title": ["Quantum collision result"],
                                "DOI": "10.1126/science.test1",
                                "published-online": {"date-parts": [[2026, 8, 9]]},
                                "type": "journal-article",
                                "abstract": (
                                    "<jats:title>Abstract</jats:title>"
                                    "<jats:p>Quantum spin collision abstract.</jats:p>"
                                ),
                                "author": [{"given": "Ada", "family": "Lovelace"}],
                            },
                            {
                                "title": ["Crossref only"],
                                "DOI": "10.1126/science.unlisted",
                                "published-online": {"date-parts": [[2026, 8, 9]]},
                                "type": "journal-article",
                            },
                        ]
                    }
                },
            )
        return httpx.Response(200, text=rss)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        candidates = JournalAdapter(
            feeds=[
                JournalFeed("Science", "https://www.science.org/science.rss", "1095-9203"),
                JournalFeed(
                    "Science",
                    "https://api.crossref.org/journals/1095-9203/works",
                    "1095-9203",
                    "crossref",
                ),
            ],
            client=client,
        ).fetch()

    assert len(candidates) == 1
    assert candidates[0].external_id == "10.1126/science.test1"
    assert candidates[0].abstract == "Quantum spin collision abstract."
    assert candidates[0].metadata["abstract_source_kind"] == "crossref"
    assert candidates[0].authors == ["Ada Lovelace"]
    assert candidates[0].metadata["document_type"] == "Research Article"


def test_public_https_validation_rejects_private_resolution_and_credentials():
    private = lambda *args, **kwargs: [  # noqa: E731
        (2, 1, 6, "", ("127.0.0.1", 443))
    ]
    with pytest.raises(JournalDiscoveryError):
        validate_public_https("https://journal.example/", resolver=private)
    with pytest.raises(JournalDiscoveryError):
        validate_public_https("https://user:pass@journal.example/", resolver=private)


def test_journal_discovery_validates_a_real_feed_preview_without_saving():
    rss = """<?xml version="1.0"?><rss version="2.0"
      xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>Example Physics</title>
      <item><title>Quantum entanglement transition</title>
      <link>https://journal.example/articles/s123</link><guid>10.1234/example</guid>
      <pubDate>Sun, 09 Aug 2026 10:00:00 GMT</pubDate><dc:type>Article</dc:type>
      <description>A many-body quantum result.</description></item></channel></rss>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            return httpx.Response(
                200,
                json={
                    "message": {
                        "items": [{"title": "Example Physics", "ISSN": ["1234-5678"]}]
                    }
                },
            )
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text=rss, headers={"content-type": "application/rss+xml"})
        return httpx.Response(
            200,
            text=(
                '<html><head><link rel="canonical" href="https://journal.example/">'
                '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
                '<meta name="citation_issn" content="1234-5678"></head></html>'
            ),
            headers={"content-type": "text/html"},
        )

    public = lambda *args, **kwargs: [  # noqa: E731
        (2, 1, 6, "", ("93.184.216.34", 443))
    ]
    preview = discover_journal(
        "Example Physics",
        "https://journal.example/",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        resolver=public,
    )

    assert preview.name == "Example Physics"
    assert preview.issn_online == "1234-5678"
    assert preview.scanned_count == 1
    assert preview.papers[0].title == "Quantum entanglement transition"
    assert preview.endpoints[0].url == "https://journal.example/feed.xml"


def test_journal_discovery_rejects_private_redirect_before_requesting_it():
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(str(request.url.host))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})

    def resolver(hostname, *args, **kwargs):
        address = "127.0.0.1" if hostname == "127.0.0.1" else "93.184.216.34"
        return [(2, 1, 6, "", (address, 443))]

    with pytest.raises(JournalDiscoveryError, match="私有网络"):
        discover_journal(
            "Unsafe",
            "https://journal.example/",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            resolver=resolver,
        )
    assert requested_hosts == ["journal.example"]


def test_journal_discovery_turns_connection_failures_into_a_form_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    public = lambda *args, **kwargs: [  # noqa: E731
        (2, 1, 6, "", ("93.184.216.34", 443))
    ]
    with pytest.raises(JournalDiscoveryError, match="无法连接期刊官网"):
        discover_journal(
            "Example Physics",
            "https://journal.example/",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            resolver=public,
        )


def test_activity_panels_show_at_most_one_hundred_rows_from_the_last_seven_days(app_client):
    client, session_factory, models = app_client
    now = datetime.now(UTC)
    with session_factory() as db:
        for index in range(105):
            created_at = now - timedelta(minutes=index)
            db.add(
                models.SyncRun(
                    source="arxiv",
                    status=models.SyncStatus.SUCCESS,
                    started_at=created_at,
                    finished_at=created_at,
                )
            )
            db.add(
                models.ApiUsage(
                    service="deepseek",
                    operation=f"recent-{index}",
                    created_at=created_at,
                )
            )
        old = now - timedelta(days=8)
        db.add(
            models.SyncRun(
                source="old-sync-marker",
                status=models.SyncStatus.SUCCESS,
                started_at=old,
                finished_at=old,
            )
        )
        db.add(
            models.ApiUsage(
                service="deepseek",
                operation="old-usage-marker",
                created_at=old,
            )
        )
        db.commit()

    page = client.get("/settings")
    assert page.status_code == 200
    assert page.text.count("<tr><td>arxiv</td>") == 100
    assert "recent-99" in page.text
    assert "recent-100" not in page.text
    assert "old-sync-marker" not in page.text
    assert "old-usage-marker" not in page.text
    assert "仅显示近 7 天记录，每栏最多 100 条" in page.text
    assert "sync-runs?cursor=" not in page.text
    assert "api-usage?cursor=" not in page.text
    assert 'aria-label="最近同步历史"' in page.text
    assert 'aria-label="API 用量明细"' in page.text
