"""Single-instance macOS menu-bar controller for arXiv Updater."""

from __future__ import annotations

import argparse
import logging
import os
import plistlib
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import urlopen

SERVICE_PORT_ENV = "ARXIV_UPDATER_PORT"
IPC_PORT_ENV = "ARXIV_UPDATER_IPC_PORT"
SERVICE_PORT = int(os.environ.get(SERVICE_PORT_ENV, "8000"))
URL = f"http://127.0.0.1:{SERVICE_PORT}/"
HEALTH_URL = f"{URL}health"
IPC_HOST = "127.0.0.1"
IPC_PORT = int(os.environ.get(IPC_PORT_ENV, "48731"))
LOGIN_AGENT_LABEL = "com.yuand17.arxiv-updater"
STATE_DIR_ENV = "ARXIV_UPDATER_STATE_DIR"
LOGGER = logging.getLogger("arxiv_updater.controller")
RESOURCE_ROOT = Path(
    getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])
).resolve()


def application_support_root() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "arXiv Updater"


def prepare_application_support() -> Path:
    state_root = application_support_root()
    state_root.mkdir(parents=True, exist_ok=True)
    os.chdir(state_root)
    return state_root


def resource_path(relative_path: str) -> Path:
    candidates = (
        RESOURCE_ROOT / relative_path,
        RESOURCE_ROOT / "src" / relative_path,
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def configure_logging(state_root: Path) -> None:
    log_dir = state_root / "data" / "logs"
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
    script = (
        'on run argv\n'
        'display alert "arXiv Updater" message (item 1 of argv) as critical\n'
        "end run"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, "--", message],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def is_healthy() -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=1.5) as response:  # noqa: S310 - fixed loopback URL
            return response.status == 200
    except (OSError, URLError):
        return False


def service_port_is_occupied() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SERVICE_PORT), timeout=0.5):
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


def acquire_controller_lock(state_root: Path) -> tuple[BinaryIO, bool]:
    import fcntl

    lock_path = state_root / "controller.lock"
    handle = lock_path.open("a+b")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return handle, False
    return handle, True


def release_controller_lock(handle: BinaryIO, *, is_owner: bool) -> None:
    if is_owner:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def send_command(command: str) -> bool:
    try:
        with socket.create_connection((IPC_HOST, IPC_PORT), timeout=2) as connection:
            connection.sendall(command.encode("ascii") + b"\n")
            return connection.recv(16).strip() == b"OK"
    except OSError:
        return False


def application_bundle_path(executable: Path | None = None) -> Path | None:
    current = (executable or Path(sys.executable)).resolve()
    for parent in current.parents:
        if parent.suffix == ".app":
            return parent
    return None


def login_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LOGIN_AGENT_LABEL}.plist"


def login_agent_payload(executable: Path) -> dict[str, object]:
    return {
        "Label": LOGIN_AGENT_LABEL,
        "ProgramArguments": [str(executable.resolve()), "--background"],
        "RunAtLoad": True,
        "KeepAlive": False,
        "LimitLoadToSessionType": "Aqua",
        "ProcessType": "Interactive",
    }


def current_user_id() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise RuntimeError("登录自启只能在 macOS 上配置")
    return int(getuid())


def is_login_startup_enabled(
    *,
    executable: Path | None = None,
    plist_path: Path | None = None,
) -> bool:
    target = (executable or Path(sys.executable)).resolve()
    path = plist_path or login_agent_path()
    try:
        with path.open("rb") as stream:
            payload = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException):
        return False
    arguments = payload.get("ProgramArguments")
    return (
        payload.get("Label") == LOGIN_AGENT_LABEL
        and isinstance(arguments, list)
        and bool(arguments)
        and Path(str(arguments[0])).resolve() == target
    )


def enable_login_startup(
    *,
    executable: Path | None = None,
    plist_path: Path | None = None,
    launchctl: str = "/bin/launchctl",
) -> None:
    target = (executable or Path(sys.executable)).resolve()
    bundle = application_bundle_path(target)
    if bundle is None:
        raise RuntimeError("登录自启只支持打包后的 arXiv Updater.app")
    if Path("/Volumes") in (bundle, *bundle.parents):
        raise RuntimeError("请先把 arXiv Updater.app 拖到 Applications，再开启登录自启")

    path = plist_path or login_agent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".plist.tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(login_agent_payload(target), stream, sort_keys=True)
    os.replace(temporary, path)

    domain = f"gui/{current_user_id()}"
    loaded = subprocess.run(
        [launchctl, "print", f"{domain}/{LOGIN_AGENT_LABEL}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if loaded.returncode == 0:
        return

    result = subprocess.run(
        [launchctl, "bootstrap", domain, str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        path.unlink(missing_ok=True)
        detail = result.stderr.strip() or result.stdout.strip() or "launchctl failed"
        raise RuntimeError(f"无法启用登录自启：{detail}")


def disable_login_startup(*, plist_path: Path | None = None) -> None:
    # Do not boot out the active job here: when the app itself was launched by
    # launchd, bootout would terminate it. Removing the plist is sufficient to
    # prevent the next login launch, and the current menu-bar session stays up.
    (plist_path or login_agent_path()).unlink(missing_ok=True)


class MenuBarController:
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
            port=SERVICE_PORT,
            log_level="info",
            access_log=False,
            log_config=None,
        )
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

    def login_startup_checked(self, _item) -> bool:
        return is_login_startup_enabled()

    def toggle_login_startup(self, icon, _item) -> None:
        try:
            if is_login_startup_enabled():
                disable_login_startup()
                message = "已关闭登录时自动启动"
            else:
                enable_login_startup()
                message = "已开启登录时自动启动"
            icon.update_menu()
            icon.notify(message, "arXiv Updater")
        except Exception as exc:
            LOGGER.exception("Unable to change login startup")
            show_error(str(exc))

    def stop(self, _icon=None, _item=None) -> None:
        LOGGER.disabled = False
        LOGGER.info("Stopping menu-bar controller")
        self.stop_event.set()
        if self.server is not None:
            self.server.should_exit = True
        if self.ipc_socket is not None:
            self.ipc_socket.close()
        if self.icon is not None:
            self.icon.stop()

    def wait_for_shutdown(self) -> None:
        if self.server_thread is not None:
            self.server_thread.join(timeout=20)
            if self.server_thread.is_alive():
                LOGGER.error("Web service did not finish within the shutdown timeout")

    def run(self) -> None:
        import pystray
        from PIL import Image

        LOGGER.info("Starting menu-bar controller")
        self._start_ipc()
        self._start_web_service()
        if not wait_for_health():
            self.stop()
            raise RuntimeError("服务健康检查超时，详情见 data/logs/controller.log")
        if self.open_on_start:
            self.open_page()
        image = Image.open(
            resource_path("arxiv_updater/static/icons/arxiv-updater-icon.png")
        )
        menu = pystray.Menu(
            pystray.MenuItem("打开 arXiv Updater", self.open_page),
            pystray.MenuItem(
                "登录时自动启动",
                self.toggle_login_startup,
                checked=self.login_startup_checked,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self.stop),
        )
        self.icon = pystray.Icon("arxiv-updater", image, "arXiv Updater", menu)
        LOGGER.disabled = False
        LOGGER.info("Menu-bar controller started")
        self.icon.run()
        self.stop()
        self.wait_for_shutdown()


def run_smoke_test() -> None:
    controller = MenuBarController(open_on_start=False)
    try:
        controller._start_ipc()
        controller._start_web_service()
        if not wait_for_health():
            raise RuntimeError("packaged service health check timed out")
    finally:
        controller.stop()
        controller.wait_for_shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--background", action="store_true")
    mode.add_argument("--open", action="store_true")
    mode.add_argument("--smoke-test", action="store_true")
    args = parser.parse_args()

    state_root = prepare_application_support()
    configure_logging(state_root)
    LOGGER.info("Controller launch requested: %s", vars(args))
    lock_handle, is_owner = acquire_controller_lock(state_root)
    if not is_owner:
        if not args.smoke_test and (args.open or not args.background):
            if not send_command("OPEN"):
                show_error("arXiv Updater 已在运行，但无法发送打开命令。")
        release_controller_lock(lock_handle, is_owner=False)
        return

    controller: MenuBarController | None = None
    try:
        if is_healthy() or service_port_is_occupied():
            show_error(f"检测到旧版或未知的 {SERVICE_PORT} 端口服务；请先关闭它。")
            return
        if args.smoke_test:
            run_smoke_test()
            return
        controller = MenuBarController(open_on_start=args.open or not args.background)
        controller.run()
    except Exception as exc:
        if controller is not None:
            controller.stop()
        LOGGER.disabled = False
        LOGGER.exception("Controller failed")
        if not args.background and not args.smoke_test:
            show_error(f"arXiv Updater 无法启动。\n{exc}")
        raise
    finally:
        release_controller_lock(lock_handle, is_owner=True)


if __name__ == "__main__":
    main()
