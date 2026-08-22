from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import SimpleNamespace

import arxiv_updater.db
import arxiv_updater.web


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "scripts" / "launch_arxiv_updater.pyw"
    loader = SourceFileLoader("arxiv_updater_tray_launcher", str(path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_tray_stop_requests_graceful_shutdown() -> None:
    launcher = _load_launcher()
    controller = launcher.TrayController(open_on_start=False)

    class FakeServer:
        should_exit = False

    class FakeSocket:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeIcon:
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    server = FakeServer()
    ipc_socket = FakeSocket()
    icon = FakeIcon()
    controller.server = server
    controller.ipc_socket = ipc_socket
    controller.icon = icon

    controller.stop()

    assert controller.stop_event.is_set()
    assert server.should_exit is True
    assert ipc_socket.closed is True
    assert icon.stopped is True


def test_web_service_disables_console_log_config(monkeypatch) -> None:
    launcher = _load_launcher()
    received: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app, **kwargs) -> None:
            received.update(kwargs)

    class FakeServer:
        def __init__(self, config) -> None:
            self.should_exit = False

        def run(self) -> None:
            return None

    monkeypatch.setitem(
        __import__("sys").modules,
        "uvicorn",
        SimpleNamespace(Config=FakeConfig, Server=FakeServer),
    )
    monkeypatch.setattr(arxiv_updater.db, "init_db", lambda: None)
    monkeypatch.setattr(
        arxiv_updater.web,
        "create_app",
        lambda *, with_scheduler: received.update(with_scheduler=with_scheduler)
        or object(),
    )

    controller = launcher.TrayController(open_on_start=False)
    controller._start_web_service()
    assert controller.server_thread is not None
    controller.server_thread.join(timeout=2)

    assert received["log_config"] is None
    assert received["with_scheduler"] is True


def test_windows_smoke_service_does_not_start_scheduler(monkeypatch) -> None:
    launcher = _load_launcher()
    received: dict[str, object] = {}

    controller = launcher.TrayController(open_on_start=False)
    monkeypatch.setattr(controller, "_start_ipc", lambda: None)
    monkeypatch.setattr(
        controller,
        "_start_web_service",
        lambda *, with_scheduler=True: received.update(
            with_scheduler=with_scheduler,
        ),
    )
    monkeypatch.setattr(controller, "stop", lambda: None)
    monkeypatch.setattr(controller, "wait_for_shutdown", lambda: None)
    monkeypatch.setattr(launcher, "TrayController", lambda **_kwargs: controller)
    monkeypatch.setattr(launcher, "wait_for_health", lambda: True)

    launcher.run_smoke_test()

    assert received["with_scheduler"] is False


def test_packaged_windows_state_uses_executable_directory(monkeypatch, tmp_path) -> None:
    launcher = _load_launcher()
    executable = tmp_path / "arXiv Updater.exe"
    monkeypatch.delenv(launcher.STATE_DIR_ENV, raising=False)
    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launcher.sys, "executable", str(executable))

    assert launcher.application_state_root() == tmp_path.resolve()
