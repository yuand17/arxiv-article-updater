import re
import socket
import threading
import time
from datetime import UTC, datetime

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

            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
