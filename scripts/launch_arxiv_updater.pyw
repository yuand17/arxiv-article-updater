"""Silent Windows launcher for arXiv Updater.

Desktop mode wakes or starts the loopback service and opens the page.  The Startup
shortcut passes ``--background`` so Windows login starts updates without opening a browser.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000/"
HEALTH_URL = f"{URL}health"
MUTEX_NAME = "Local\\arXivUpdaterLauncher"
ERROR_ALREADY_EXISTS = 183


def configure_logging() -> None:
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "launcher.log",
        encoding="utf-8",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def is_healthy() -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=1.5) as response:  # noqa: S310 - fixed loopback URL
            return response.status == 200
    except (OSError, URLError):
        return False


def acquire_start_lock() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def start_service() -> None:
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    if not pythonw.is_file():
        raise RuntimeError(f"找不到项目 Python 运行环境：{pythonw}")
    log_dir = ROOT / "data" / "logs"
    stdout = (log_dir / "server.stdout.log").open("a", encoding="utf-8")
    stderr = (log_dir / "server.stderr.log").open("a", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    subprocess.Popen(  # noqa: S603 - fixed executable and local project arguments
        [str(pythonw), "-m", "arxiv_updater", "serve"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    logging.info("Started local service process")


def wait_for_health(timeout_seconds: float = 25) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_healthy():
            return True
        time.sleep(0.5)
    return False


def show_error(message: str) -> None:
    logging.error(message)
    ctypes.windll.user32.MessageBoxW(None, message, "arXiv Updater", 0x10)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--background", action="store_true")
    args = parser.parse_args()
    configure_logging()

    already_running = is_healthy()
    if not already_running and acquire_start_lock():
        try:
            start_service()
        except Exception as exc:
            if not args.background:
                show_error(f"arXiv Updater 无法启动。详情见 data/logs/launcher.log。\n{exc}")
            else:
                logging.exception("Background launch failed")
            return

    if not wait_for_health():
        if not args.background:
            show_error("arXiv Updater 启动超时。详情见 data/logs/server.stderr.log。")
        else:
            logging.error("Background startup timed out")
        return
    if not args.background:
        webbrowser.open(URL, new=1)


if __name__ == "__main__":
    main()
