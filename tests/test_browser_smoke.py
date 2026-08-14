import re
import socket
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
import uvicorn

from arxiv_updater import db as db_module
from arxiv_updater import models
from arxiv_updater import web as web_module

pytestmark = pytest.mark.browser


def _available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_local_feed_actions_and_mobile_layout(monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    all_source_calls: list[str] = []
    monkeypatch.setattr(
        "arxiv_updater.scheduler.run_all_source_updates_in_background",
        lambda: all_source_calls.append("all"),
    )
    db_module.Base.metadata.drop_all(bind=db_module.engine)
    db_module.Base.metadata.create_all(bind=db_module.engine)
    with db_module.SessionLocal() as db:
        paper = models.Paper(
            title="Browser-tested quantum paper $q_{2}$",
            normalized_title="browser-tested quantum paper q 2",
            abstract="We present a browser-tested $q_{2}$ method for quantum information.",
            abstract_source="arxiv",
            abstract_status="available",
            authors_text="Alice Example",
            first_author="alice example",
            published_at=datetime.now(UTC),
            categories=["quant-ph"],
            doi="10.1234/browser.test",
            canonical_url="https://arxiv.org/abs/2608.13521",
            pdf_url="https://arxiv.org/pdf/2608.13521",
        )
        db.add(paper)
        db.flush()
        paper_id = paper.id
        now = datetime.now(UTC)
        db.add_all(
            [
                models.SyncRun(
                    source="arxiv",
                    status=models.SyncStatus.SUCCESS,
                    started_at=now - timedelta(minutes=index),
                    finished_at=now - timedelta(minutes=index) + timedelta(seconds=1),
                    items_seen=index,
                    items_created=index,
                )
                for index in range(25)
            ]
        )
        db.add_all(
            [
                models.ApiUsage(
                    service="deepseek",
                    operation="featured_rerank",
                    request_count=1,
                    input_tokens=index,
                    output_tokens=index,
                    created_at=now - timedelta(minutes=index),
                )
                for index in range(25)
            ]
        )
        db.commit()

    port = _available_port()
    server = uvicorn.Server(
        uvicorn.Config(web_module.create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/?view=all")
            playwright.expect(
                page.locator("h2", has_text="Browser-tested quantum paper")
            ).to_be_visible()
            playwright.expect(page.locator("h2 .katex")).to_be_visible()
            search = page.get_by_label("搜索英文标题或作者", exact=True)
            search.focus()
            playwright.expect(page.locator(".search-box")).to_have_css(
                "outline-style", "solid"
            )
            playwright.expect(
                page.locator('.view-tabs a[aria-current="page"]')
            ).to_have_count(1)
            loading = page.locator(".htmx-indicator").first
            playwright.expect(loading).to_be_hidden()
            loading.evaluate("element => element.classList.add('htmx-request')")
            playwright.expect(loading).to_be_visible()
            loading.evaluate("element => element.classList.remove('htmx-request')")
            playwright.expect(loading).to_be_hidden()
            page.locator(".interest-button").click()
            playwright.expect(page.locator(".abstract-panel")).to_contain_text(
                "quantum information"
            )
            playwright.expect(page.locator(".abstract-panel .katex")).to_be_visible()
            fulltext_link = page.get_by_role("link", name="阅读原文", exact=False)
            playwright.expect(fulltext_link).to_have_count(1)
            playwright.expect(fulltext_link).to_have_attribute(
                "href", "https://doi.org/10.1234/browser.test"
            )
            page.context.route(
                "https://doi.org/**",
                lambda route: route.fulfill(
                    status=200, content_type="text/html", body="DOI destination"
                ),
            )
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and response.url.endswith(f"/papers/{paper_id}/fulltext")
            ) as signal_response, page.expect_popup() as popup_info:
                fulltext_link.click()
            assert signal_response.value.status == 204
            popup = popup_info.value
            popup.wait_for_load_state("load")
            assert popup.url == "https://doi.org/10.1234/browser.test"
            playwright.expect(popup.get_by_text("DOI destination")).to_be_visible()
            popup.close()
            page.locator("button[hx-post$='/save']").click()
            playwright.expect(page.locator("button[hx-post$='/save']")).to_have_class(
                re.compile(r"\bis-saved\b")
            )

            page.goto(f"http://127.0.0.1:{port}/settings?toast=sync_started")
            title = page.locator(".settings-heading-copy h1")
            toast = page.locator(".toast", has_text="更新已启动").first
            playwright.expect(title).to_be_visible()
            playwright.expect(toast).to_be_visible()
            all_sources_button = page.get_by_role(
                "button", name="一键更新四个来源", exact=True
            )
            playwright.expect(all_sources_button).to_be_visible()
            all_sources_button.click()
            page.wait_for_timeout(100)
            assert all_source_calls == ["all"]
            latest_toast = page.locator(".toast", has_text="更新已启动").last
            playwright.expect(latest_toast).to_be_visible()
            deepseek_toggle = page.locator('[aria-label="启用 DeepSeek"]')
            deepseek_fields = page.locator("#deepseek-key-fields")
            deepseek_track = page.locator("#service-deepseek .ios-switch-track")
            playwright.expect(deepseek_fields).to_be_hidden()
            deepseek_track.click()
            playwright.expect(deepseek_toggle).to_be_checked()
            playwright.expect(deepseek_fields).to_be_visible()
            playwright.expect(deepseek_track).to_have_css(
                "background-color", "rgb(52, 199, 89)"
            )
            title_box = title.bounding_box()
            assert title_box is not None and title_box["height"] < 100
            toast_box = latest_toast.bounding_box()
            assert toast_box is not None
            assert toast_box["x"] + toast_box["width"] > 1200
            assert toast_box["y"] + toast_box["height"] > 800
            science_toggle = page.get_by_label("订阅 Science", exact=True)
            nature_toggle = page.get_by_label("订阅 Nature", exact=True)
            playwright.expect(science_toggle).to_be_checked()
            playwright.expect(nature_toggle).to_be_checked()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and "/settings/journals/" in response.url
            ) as disable_response:
                page.locator(
                    'input[aria-label="订阅 Science"] + .ios-switch-track'
                ).click()
            assert disable_response.value.ok
            playwright.expect(science_toggle).not_to_be_checked()
            playwright.expect(nature_toggle).to_be_checked()
            playwright.expect(
                page.locator(".toast", has_text="期刊订阅已更新").last
            ).to_be_visible()
            page.reload()
            playwright.expect(science_toggle).not_to_be_checked()
            playwright.expect(nature_toggle).to_be_checked()
            with page.expect_response(
                lambda response: response.request.method == "POST"
                and "/settings/journals/" in response.url
            ) as enable_response:
                page.locator(
                    'input[aria-label="订阅 Science"] + .ios-switch-track'
                ).click()
            assert enable_response.value.ok
            playwright.expect(science_toggle).to_be_checked()
            page.reload()
            playwright.expect(science_toggle).to_be_checked()

            sync_panel = page.locator('[aria-label="最近同步历史"]')
            usage_panel = page.locator('[aria-label="API 用量明细"]')
            playwright.expect(sync_panel).to_be_visible()
            playwright.expect(usage_panel).to_be_visible()
            assert sync_panel.evaluate("element => element.scrollHeight > element.clientHeight")
            assert usage_panel.evaluate("element => element.scrollHeight > element.clientHeight")
            usage_start = usage_panel.evaluate("element => element.scrollTop")
            sync_panel.scroll_into_view_if_needed()
            sync_panel.hover()
            page.mouse.wheel(0, 2400)
            playwright.expect(sync_panel.locator("tbody tr")).to_have_count(25)
            assert usage_panel.evaluate("element => element.scrollTop") == usage_start
            usage_panel.scroll_into_view_if_needed()
            usage_panel.hover()
            page.mouse.wheel(0, 2400)
            playwright.expect(usage_panel.locator("tbody tr")).to_have_count(25)

            page.set_viewport_size({"width": 390, "height": 844})
            playwright.expect(all_sources_button).to_be_visible()
            sync_box = sync_panel.bounding_box()
            usage_box = usage_panel.bounding_box()
            assert sync_box is not None and usage_box is not None
            assert usage_box["y"] > sync_box["y"] + sync_box["height"]
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
