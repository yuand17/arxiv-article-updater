"""Visible, human-assisted Chrome session for sites that require a browser challenge."""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx


def is_cloudflare_challenge(response: httpx.Response) -> bool:
    """Recognize a Cloudflare browser challenge without exposing its response body."""

    body = response.text.casefold()
    challenge_header = response.headers.get("cf-mitigated", "").casefold() == "challenge"
    if challenge_header:
        return True
    if response.status_code not in {403, 503}:
        return False
    return any(
        marker in body
        for marker in (
            "cloudflare",
            "security verification",
            "安全验证",
            "just a moment",
            "cf-chl-",
        )
    ) or "cloudflare" in response.headers.get("server", "").casefold()


def find_chrome_executable() -> Path:
    candidates: list[Path] = []
    command = shutil.which("chrome") or shutil.which("chrome.exe")
    if command:
        candidates.append(Path(command))
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("未找到 Google Chrome，请先安装 Chrome 后再重试")


def _available_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_debugger(
    process: subprocess.Popen[bytes], port: int, *, timeout_seconds: float = 15
) -> None:
    deadline = time.monotonic() + timeout_seconds
    url = f"http://127.0.0.1:{port}/json/version"
    with httpx.Client(timeout=0.5, trust_env=False) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Chrome 验证窗口未能启动")
            try:
                if client.get(url).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    raise RuntimeError("连接 Chrome 验证窗口超时")


def _close_chrome(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def fetch_page_with_human_chrome(
    url: str,
    profile_directory: Path,
    timeout_seconds: float,
    *,
    ready_selector: str = "li.paper",
) -> str:
    """Open a dedicated visible Chrome window and wait for human-cleared content."""

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，无法启动 Chrome 真人验证") from exc

    chrome = find_chrome_executable()
    target_hostname = urlparse(url).hostname
    profile_directory = profile_directory.resolve()
    profile_directory.mkdir(parents=True, exist_ok=True)
    port = _available_loopback_port()
    command = [
        str(chrome),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_directory}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
        "--new-window",
        url,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    runtime = None
    browser = None
    try:
        _wait_for_debugger(process, port)
        runtime = sync_playwright().start()
        browser = runtime.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("您关闭了 Chrome 验证窗口，人工验证未完成")
            try:
                pages = [page for context in browser.contexts for page in context.pages]
                for page in pages:
                    if urlparse(page.url).hostname != target_hostname:
                        continue
                    if page.locator(ready_selector).count() > 0:
                        return page.content()
            except PlaywrightError as exc:
                if process.poll() is not None:
                    raise RuntimeError("您关闭了 Chrome 验证窗口，人工验证未完成") from exc
                raise RuntimeError("Chrome 验证会话意外中断") from exc
            time.sleep(0.5)
        minutes = max(1, round(timeout_seconds / 60))
        raise RuntimeError(f"等待 Cloudflare 真人验证超时（{minutes} 分钟）")
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        if runtime is not None:
            runtime.stop()
        _close_chrome(process)
