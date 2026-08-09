"""Single-instance Windows tray controller for the local arXiv Updater service."""

from __future__ import annotations

import argparse
import ctypes
import logging
import socket
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
URL = "http://127.0.0.1:8000/"
HEALTH_URL = f"{URL}health"
IPC_HOST = "127.0.0.1"
IPC_PORT = 48731
MUTEX_NAME = "Local\\arXivUpdaterController"
ERROR_ALREADY_EXISTS = 183
WM_CLOSE = 0x10
LOGGER = logging.getLogger("arxiv_updater.controller")


def configure_logging() -> None:
    log_dir = ROOT / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    for existing_handler in LOGGER.handlers:
        existing_handler.close()
    LOGGER.handlers.clear()
    handler = RotatingFileHandler(
        log_dir / "controller.log",
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.disabled = False
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def show_error(message: str) -> None:
    LOGGER.disabled = False
    LOGGER.error(message)
    ctypes.windll.user32.MessageBoxW(None, message, "arXiv Updater", WM_CLOSE)


def is_healthy() -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=1.5) as response:  # noqa: S310 - fixed loopback URL
            return response.status == 200
    except (OSError, URLError):
        return False


def service_port_is_occupied() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
            return True
    except OSError:
        return False


def wait_for_health(timeout_seconds: float = 30) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if is_healthy():
            return True
        time.sleep(0.25)
    return False


def acquire_controller_mutex() -> tuple[int, bool]:
    handle = ctypes.windll.kernel32.CreateMutexW(None, True, MUTEX_NAME)
    return handle, ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def send_command(command: str) -> bool:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=2) as connection:
            connection.sendall(command.encode("ascii") + b"\n")
            return connection.recv(16).strip() == b"OK"
    except OSError:
        return False


class TrayController:
    def __init__(self, *, open_on_start: bool) -> None:
        self.open_on_start = open_on_start
        self.stop_event = threading.Event()
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.ipc_socket: socket.socket | None = None
        self.ipc_thread: threading.Thread | None = None
        self.icon = None

    def open_page(self, _icon=None, _item=None) -> None:
        webbrowser.open(URL, new=1)

    def _serve_ipc(self) -> None:
        assert self.ipc_socket is not None
        self.ipc_socket.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                connection, _address = self.ipc_socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                command = connection.recv(32).decode("ascii", errors="ignore").strip()
                if command == "OPEN":
                    self.open_page()
                connection.sendall(b"OK\n")

    def _start_ipc(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((IPC_HOST, IPC_PORT))
        listener.listen(2)
        self.ipc_socket = listener
        self.ipc_thread = threading.Thread(
            target=self._serve_ipc,
            name="arxiv-updater-ipc",
            daemon=True,
        )
        self.ipc_thread.start()

    def _start_web_service(self) -> None:
        import uvicorn

        from arxiv_updater.db import init_db
        from arxiv_updater.web import create_app

        LOGGER.info("Initializing local database")
        init_db()
        LOGGER.disabled = False
        LOGGER.info("Local database is ready")
        config = uvicorn.Config(
            create_app(with_scheduler=True),
            host="127.0.0.1",
            port=8000,
            log_level="info",
            access_log=False,
            log_config=None,
        )
        # Uvicorn's logging setup runs while Config is constructed. Re-enable
        # the private controller logger before any service thread can emit.
        LOGGER.disabled = False
        LOGGER.info("Uvicorn configuration is ready")
        self.server = uvicorn.Server(config)
        self.server_thread = threading.Thread(
            target=self.server.run,
            name="arxiv-updater-web",
            daemon=True,
        )
        self.server_thread.start()
        LOGGER.info("Web service thread started")

    def stop(self, _icon=None, _item=None) -> None:
        LOGGER.disabled = False
        LOGGER.info("Stopping tray controller")
        self.stop_event.set()
        if self.server is not None:
            self.server.should_exit = True
        if self.ipc_socket is not None:
            self.ipc_socket.close()
        if self.icon is not None:
            self.icon.stop()

    def run(self) -> None:
        import pystray
        from PIL import Image

        LOGGER.info("Starting tray controller")
        self._start_ipc()
        self._start_web_service()
        if not wait_for_health():
            self.stop()
            raise RuntimeError("服务健康检查超时，详情见 data/logs/controller.log")
        if self.open_on_start:
            self.open_page()
        icon_path = ROOT / "src" / "arxiv_updater" / "static" / "icons" / "arxiv-updater.ico"
        image = Image.open(icon_path)
        menu = pystray.Menu(
            pystray.MenuItem("打开 arXiv Updater", self.open_page, default=True),
            pystray.MenuItem("结束", self.stop),
        )
        self.icon = pystray.Icon("arxiv-updater", image, "arXiv Updater", menu)
        LOGGER.disabled = False
        LOGGER.info("Tray controller started")
        self.icon.run()
        self.stop()
        if self.server_thread is not None:
            self.server_thread.join(timeout=20)
            if self.server_thread.is_alive():
                LOGGER.error("Web service did not finish within the shutdown timeout")


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--open", action="store_true")
    args = parser.parse_args()
    configure_logging()
    LOGGER.info("Controller launch requested: %s", "background" if args.background else "open")

    mutex_handle, is_owner = acquire_controller_mutex()
    if not is_owner:
        if args.open or not args.background:
            if not send_command("OPEN"):
                show_error("arXiv Updater 已在运行，但无法发送打开命令。")
        return
    controller: TrayController | None = None
    try:
        if is_healthy() or service_port_is_occupied():
            show_error("检测到旧版或未知的 8000 端口服务；请先关闭它，再启动托盘版本。")
            return
        controller = TrayController(open_on_start=args.open or not args.background)
        controller.run()
    except Exception as exc:
        if controller is not None:
            controller.stop()
        LOGGER.disabled = False
        LOGGER.exception("Controller failed")
        if not args.background:
            show_error(f"arXiv Updater 无法启动。\n{exc}")
    finally:
        ctypes.windll.kernel32.ReleaseMutex(mutex_handle)
        ctypes.windll.kernel32.CloseHandle(mutex_handle)


if __name__ == "__main__":
    main()
