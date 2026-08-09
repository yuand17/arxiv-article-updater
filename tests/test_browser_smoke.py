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


def test_local_feed_actions_and_mobile_layout():
    playwright = pytest.importorskip("playwright.sync_api")
    db_module.Base.metadata.drop_all(bind=db_module.engine)
    db_module.Base.metadata.create_all(bind=db_module.engine)
    with db_module.SessionLocal() as db:
        paper = models.Paper(
            title="Browser-tested quantum paper",
            normalized_title="browser-tested quantum paper",
            abstract="We present a browser-tested method for quantum information.",
            abstract_source="arxiv",
            abstract_status="available",
            authors_text="Alice Example",
            first_author="alice example",
            published_at=datetime.now(UTC),
            categories=["quant-ph"],
        )
        db.add(paper)
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
            page.locator(".interest-button").click()
            playwright.expect(page.locator(".abstract-panel")).to_contain_text(
                "browser-tested method"
            )
            page.locator("button[hx-post$='/save']").click()
            playwright.expect(page.locator("button[hx-post$='/save']")).to_have_class(
                re.compile(r"\bis-saved\b")
            )

            page.goto(f"http://127.0.0.1:{port}/settings?toast=sync_started")
            title = page.locator(".settings-heading-copy h1")
            toast = page.locator(".toast", has_text="更新已启动")
            playwright.expect(title).to_be_visible()
            playwright.expect(toast).to_be_visible()
            title_box = title.bounding_box()
            assert title_box is not None and title_box["height"] < 100
            toast_box = toast.bounding_box()
            assert toast_box is not None
            assert toast_box["x"] + toast_box["width"] > 1200
            assert toast_box["y"] + toast_box["height"] > 800

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
            sync_box = sync_panel.bounding_box()
            usage_box = usage_panel.bounding_box()
            assert sync_box is not None and usage_box is not None
            assert usage_box["y"] > sync_box["y"] + sync_box["height"]
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
