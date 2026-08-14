import pytest
from sqlalchemy import select

from arxiv_updater.web import display_source_error


def test_only_trusted_local_host_headers_are_accepted(app_client):
    client, _, _ = app_client

    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "testserver"):
        response = client.get("/health", headers={"Host": host})
        assert response.status_code == 200

    response = client.get("/health", headers={"Host": "malicious.example:8000"})
    assert response.status_code == 400
    assert "Host" in response.text


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_cross_site_browser_requests_block_all_unsafe_methods(app_client, method):
    client, _, _ = app_client

    response = client.request(
        method,
        "/health",
        headers={
            "Origin": "https://malicious.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403


def test_cross_site_browser_post_cannot_change_ordinary_settings(app_client):
    client, session_factory, models = app_client

    response = client.post(
        "/settings",
        data={"interests": "cross-site-change", "featured_paper_count": "7"},
        headers={
            "Origin": "https://malicious.example",
            "Sec-Fetch-Site": "cross-site",
        },
        follow_redirects=False,
    )

    assert response.status_code == 403
    with session_factory() as db:
        preferences = db.get(models.AppPreferences, 1)
        assert preferences is None or preferences.manual_interests != "cross-site-change"


def test_same_origin_browser_post_and_local_test_client_remain_supported(app_client):
    client, session_factory, models = app_client

    same_origin = client.post(
        "/settings",
        data={"interests": "same-origin-change", "featured_paper_count": "23"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    local_client = client.post(
        "/settings",
        data={"interests": "local-client-change", "featured_paper_count": "24"},
        follow_redirects=False,
    )

    assert same_origin.status_code == 303
    assert local_client.status_code == 303
    with session_factory() as db:
        preferences = db.get(models.AppPreferences, 1)
        assert preferences is not None
        assert preferences.manual_interests == "local-client-change"
        assert preferences.featured_paper_count == 24


def test_feed_and_settings_render_accessible_control_metadata(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        paper = models.Paper(
            title="Accessible quantum paper",
            normalized_title="accessible quantum paper",
            abstract="Accessible abstract",
            authors_text="Accessible Author",
            categories=["quant-ph"],
        )
        paper.sources.append(
            models.PaperSource(source="arxiv", external_id="accessible-paper")
        )
        db.add_all(
            [
                paper,
                models.TrackedAuthor(
                    scholar_author_id="accessible-author",
                    name="Accessible Author",
                    profile_url=(
                        "https://scholar.google.com/citations?user=accessible-author"
                    ),
                ),
            ]
        )
        db.commit()
        paper_id = paper.id

    feed = client.get("/?view=all")
    settings = client.get("/settings")
    stylesheet = client.get("/static/app.css")
    vendor_assets = [
        client.get("/static/vendor/htmx-2.0.4.min.js"),
        client.get("/static/vendor/katex/katex-0.18.0.min.css"),
        client.get("/static/vendor/katex/katex-0.18.0.min.js"),
        client.get("/static/vendor/katex/auto-render-0.18.0.min.js"),
    ]

    assert feed.status_code == settings.status_code == stylesheet.status_code == 200
    assert all(response.status_code == 200 for response in vendor_assets)
    assert "cdn.jsdelivr.net" not in feed.text
    assert "unpkg.com" not in feed.text
    assert 'aria-label="搜索英文标题或作者"' in feed.text
    assert feed.text.count('aria-current="page"') == 1
    assert 'role="status" aria-live="polite" aria-atomic="true"' in feed.text
    assert f'id="save-{paper_id}"' in feed.text
    assert 'aria-pressed="false"' in feed.text
    assert 'aria-label="立即更新 arXiv"' in settings.text
    assert 'aria-label="立即更新 重点期刊"' in settings.text
    assert 'aria-label="移除重点作者 Accessible Author"' in settings.text
    assert "<label>研究兴趣<textarea" in settings.text
    assert "<label>作者主页链接<input" in settings.text
    assert settings.text.count('scope="col"') == 11
    assert ".search-box:focus-within" in stylesheet.text
    assert ".htmx-indicator { display:none; }" in stylesheet.text
    assert ".htmx-request.htmx-indicator { display:inline; }" in stylesheet.text


def test_security_headers_and_fulltext_signal_require_a_trusted_post(app_client):
    client, session_factory, models = app_client
    with session_factory() as db:
        paper = models.Paper(
            title="Trusted fulltext paper",
            normalized_title="trusted fulltext paper",
            canonical_url="https://example.test/paper",
        )
        db.add(paper)
        db.commit()
        paper_id = paper.id

    page = client.get("/")
    old_get = client.get(f"/papers/{paper_id}/fulltext", follow_redirects=False)
    cross_site = client.post(
        f"/papers/{paper_id}/fulltext",
        headers={"Origin": "https://malicious.example", "Sec-Fetch-Site": "cross-site"},
        follow_redirects=False,
    )
    trusted = client.post(f"/papers/{paper_id}/fulltext", follow_redirects=False)

    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert old_get.status_code == 405
    assert cross_site.status_code == 403
    assert trusted.status_code == 303
    assert trusted.headers["location"] == "https://example.test/paper"
    with session_factory() as db:
        interaction = db.scalar(
            select(models.Interaction).where(
                models.Interaction.paper_id == paper_id,
                models.Interaction.kind == models.InteractionKind.FULLTEXT,
            )
        )
        assert interaction is not None


@pytest.mark.parametrize(
    ("stored_error", "expected_message"),
    [
        (
            "ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING] unexpected eof",
            "网络连接暂时中断",
        ),
        (
            "Partial sync: Science: RuntimeError; Science Advances: RuntimeError",
            "部分期刊暂时未更新",
        ),
        (
            "All journal subscriptions failed: Science: HTTP 403",
            "重点期刊暂时无法连接",
        ),
    ],
)
def test_low_level_source_errors_are_classified_for_the_ui(
    app_client,
    stored_error,
    expected_message,
):
    client, session_factory, models = app_client
    with session_factory() as db:
        run = models.SyncRun(
            source="journals",
            status=models.SyncStatus.FAILED,
            error=stored_error,
        )
        db.add(run)
        db.commit()

    settings = client.get("/settings")

    assert settings.status_code == 200
    assert expected_message in settings.text
    assert stored_error not in settings.text
    assert display_source_error(stored_error).startswith(expected_message)
