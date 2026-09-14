from datetime import UTC, datetime

import feedparser
import httpx
import pytest
from feedparser.exceptions import CharacterEncodingOverride, UndeclaredNamespace

from arxiv_updater.datetime_utils import as_utc
from arxiv_updater.services import sync as sync_module
from arxiv_updater.services.journal_catalog import ensure_builtin_journals
from arxiv_updater.sources.journals import (
    JOURNAL_ENRICHMENT_WARNING_PREFIX,
    JournalAdapter,
    JournalFeed,
)

SINCE = datetime(2026, 9, 13, tzinfo=UTC)
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Example Physics</title>
<item><title>Quantum collision result</title><guid>10.1234/quantum</guid>
<link>https://journal.example/articles/quantum</link>
<pubDate>Mon, 14 Sep 2026 01:00:00 GMT</pubDate>
<description>Quantum many-body physics.</description></item></channel></rss>"""
EMPTY_RSS = '<rss version="2.0"><channel><title>Example Physics</title></channel></rss>'
TRUNCATED_RSS = RSS.removesuffix("</channel></rss>")
FEEDS = [
    JournalFeed("Example Physics", "https://journal.example/feed", "1234-5678"),
    JournalFeed(
        "Example Physics",
        "https://api.crossref.org/journals/1234-5678/works",
        "1234-5678",
        "crossref",
    ),
]


def _work(doi="10.1234/quantum"):
    return {
        "DOI": doi,
        "title": ["Quantum collision result"],
        "published-online": {"date-parts": [[2026, 9, 14]]},
        "abstract": "<jats:p>Complete quantum collision abstract.</jats:p>",
    }


def _works_response(*, items=None, cursor=""):
    return httpx.Response(
        200,
        json={
            "message": {"items": items if items is not None else [_work()], "next-cursor": cursor}
        },
    )


def test_crossref_enrichment_avoids_the_rejected_cursor_publication_sort_pair():
    requests = []

    def handler(request):
        if request.url.host != "api.crossref.org":
            return httpx.Response(200, text=RSS)
        requests.append(request)
        if "cursor" in request.url.params and request.url.params.get("sort") == "published":
            return httpx.Response(400, json={"status": "failed"})
        return _works_response()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=FEEDS, client=client)
        candidates = adapter.fetch(SINCE)

    assert len(requests) == 1
    assert requests[0].url.params["sort"] == "published"
    assert requests[0].url.params["filter"] == "from-pub-date:2026-09-13"
    assert len(candidates) == 1
    assert candidates[0].abstract == "Complete quantum collision abstract."
    assert adapter.errors == adapter.warnings == []


@pytest.mark.parametrize("rss", [RSS, EMPTY_RSS])
@pytest.mark.parametrize("since", [SINCE, datetime(2026, 9, 15, tzinfo=UTC)])
def test_optional_crossref_failure_keeps_a_valid_official_feed_successful(rss, since):
    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(400)
        return httpx.Response(200, text=rss)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=FEEDS, client=client)
        candidates = adapter.fetch(since)

    assert len(candidates) == int(rss == RSS and since == SINCE)
    assert adapter.errors == []
    assert adapter.warnings == ["Example Physics crossref: HTTP 400"]


@pytest.mark.parametrize(
    "response", [httpx.Response(403), httpx.Response(200, text="<html>Login</html>")]
)
def test_crossref_cannot_rescue_an_unavailable_or_unrecognizable_official_feed(response):
    def handler(request):
        return _works_response() if request.url.host == "api.crossref.org" else response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=FEEDS, client=client)
        with pytest.raises(RuntimeError, match="Example Physics rss:"):
            adapter.fetch(SINCE)

    assert len(adapter.errors) == 1


def test_reusing_a_journal_adapter_clears_recovered_errors_and_warnings():
    recovered = False

    def handler(request):
        if request.url.host == "api.crossref.org":
            return _works_response() if recovered else httpx.Response(400)
        return httpx.Response(200, text=RSS) if recovered else httpx.Response(403)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=FEEDS, client=client)
        with pytest.raises(RuntimeError):
            adapter.fetch(SINCE)
        assert adapter.errors and adapter.warnings
        recovered = True
        assert len(adapter.fetch(SINCE)) == 1
        assert adapter.errors == adapter.warnings == []


@pytest.mark.parametrize("warning", [CharacterEncodingOverride, UndeclaredNamespace])
def test_recoverable_feed_warnings_do_not_reject_complete_entries(monkeypatch, warning):
    parsed = feedparser.parse(RSS)
    parsed["bozo"] = True
    parsed["bozo_exception"] = warning("Recoverable publisher metadata warning")
    monkeypatch.setattr("arxiv_updater.sources.journals.feedparser.parse", lambda _content: parsed)

    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, text=RSS))
    ) as client:
        adapter = JournalAdapter(feeds=[FEEDS[0]], client=client)
        assert len(adapter.fetch(SINCE)) == 1
        assert adapter.errors == []


def test_standalone_crossref_paginates_with_compatible_sorting_and_repeated_filters():
    requests = []

    def handler(request):
        requests.append(request)
        assert "sort" not in request.url.params
        assert request.url.params["filter"] == "from-pub-date:2026-09-13"
        if len(requests) == 1:
            assert request.url.params["cursor"] == "*"
            return _works_response(
                items=[_work(f"10.1234/{i}") for i in range(100)], cursor="next-page"
            )
        assert request.url.params["cursor"] == "next-page"
        return _works_response(items=[_work("10.1234/last")])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=[FEEDS[1]], client=client)
        candidates = adapter.fetch(SINCE)

    assert len(candidates) == 101
    assert len(requests) == 2
    assert adapter.errors == adapter.warnings == []


def test_standalone_crossref_does_not_report_partial_pagination_as_success():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _works_response(
                items=[_work(f"10.1234/{i}") for i in range(100)], cursor="next-page"
            )
        return httpx.Response(400)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=[FEEDS[1]], client=client)
        with pytest.raises(RuntimeError, match="Example Physics crossref: HTTP 400"):
            adapter.fetch(SINCE)

    assert calls == 2
    assert adapter.warnings == []


@pytest.mark.parametrize("payload", [{"message": []}, {"message": {"items": [None]}}])
def test_invalid_crossref_metadata_is_an_optional_warning_when_rss_succeeds(payload):
    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json=payload)
        return httpx.Response(200, text=RSS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        adapter = JournalAdapter(feeds=FEEDS, client=client)
        assert len(adapter.fetch(SINCE)) == 1
        assert adapter.warnings
        assert adapter.errors == []


def test_sync_persists_optional_warning_then_clears_it_after_recovery(app_client, monkeypatch):
    _, session_factory, models = app_client
    crossref_recovered = False

    def handler(request):
        if request.url.host == "api.crossref.org":
            return _works_response(items=[]) if crossref_recovered else httpx.Response(400)
        return httpx.Response(200, text=EMPTY_RSS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            sync_module, "JournalAdapter", lambda **kwargs: JournalAdapter(client=client, **kwargs)
        )
        with session_factory() as db:
            subscriptions = ensure_builtin_journals(db)
            subscription = subscriptions[0]
            for other in subscriptions[1:]:
                other.is_active = False
            subscription.last_error = "RuntimeError: Nature crossref: HTTP 400"
            subscription.last_success_at = SINCE
            db.commit()

            run = sync_module.sync_sources(db, "journals")[0]

            assert run.status == models.SyncStatus.SUCCESS
            assert run.error is None
            assert subscription.last_error == (
                JOURNAL_ENRICHMENT_WARNING_PREFIX + "Nature crossref: HTTP 400"
            )
            assert as_utc(subscription.last_success_at) > SINCE
            assert subscription.last_items_seen == 0

            crossref_recovered = True
            run = sync_module.sync_sources(db, "journals")[0]
            assert run.status == models.SyncStatus.SUCCESS
            assert subscription.last_error == ""


@pytest.mark.parametrize(
    ("primary_response", "crossref_response", "expected_error"),
    [
        (httpx.Response(403), _works_response(), "Nature rss: HTTP 403"),
        (
            httpx.Response(200, text=TRUNCATED_RSS),
            httpx.Response(400),
            "Nature rss: Invalid feed response",
        ),
    ],
)
def test_failed_primary_sync_preserves_the_previous_success_and_statistics(
    app_client, monkeypatch, primary_response, crossref_response, expected_error
):
    _, session_factory, models = app_client

    def handler(request):
        return crossref_response if request.url.host == "api.crossref.org" else primary_response

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            sync_module, "JournalAdapter", lambda **kwargs: JournalAdapter(client=client, **kwargs)
        )
        with session_factory() as db:
            subscriptions = ensure_builtin_journals(db)
            subscription = subscriptions[0]
            for other in subscriptions[1:]:
                other.is_active = False
            subscription.last_success_at = SINCE
            subscription.last_items_seen = 7
            subscription.last_items_imported = 3
            db.commit()

            run = sync_module.sync_sources(db, "journals")[0]

            assert run.status == models.SyncStatus.FAILED
            assert as_utc(subscription.last_success_at) == SINCE
            assert subscription.last_items_seen == 7
            assert subscription.last_items_imported == 3
            assert expected_error in subscription.last_error
            assert not subscription.last_error.startswith(JOURNAL_ENRICHMENT_WARNING_PREFIX)
