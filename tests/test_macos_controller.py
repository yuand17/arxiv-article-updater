from __future__ import annotations

import plistlib
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import arxiv_updater.db
import arxiv_updater.web


def _load_launcher():
    path = Path(__file__).resolve().parents[1] / "scripts" / "launch_arxiv_updater_macos.py"
    spec = spec_from_file_location("arxiv_updater_macos_launcher", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_macos_stop_requests_graceful_shutdown() -> None:
    launcher = _load_launcher()
    controller = launcher.MenuBarController(open_on_start=False)

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


def test_macos_web_service_disables_console_log_config(monkeypatch) -> None:
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
        lambda *, with_scheduler: object(),
    )

    controller = launcher.MenuBarController(open_on_start=False)
    controller._start_web_service()
    assert controller.server_thread is not None
    controller.server_thread.join(timeout=2)

    assert received["log_config"] is None


def test_login_agent_targets_packaged_app_and_background_mode(tmp_path, monkeypatch) -> None:
    launcher = _load_launcher()
    executable = (
        tmp_path / "Applications" / "arXiv Updater.app" / "Contents" / "MacOS" / "arXiv Updater"
    )
    plist_path = tmp_path / "Library" / "LaunchAgents" / "arxiv-updater.plist"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=1 if command[1] == "print" else 0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher, "current_user_id", lambda: 501)

    launcher.enable_login_startup(executable=executable, plist_path=plist_path)

    with plist_path.open("rb") as stream:
        payload = plistlib.load(stream)
    assert payload["Label"] == launcher.LOGIN_AGENT_LABEL
    assert payload["ProgramArguments"] == [str(executable.resolve()), "--background"]
    assert payload["RunAtLoad"] is True
    assert launcher.is_login_startup_enabled(
        executable=executable,
        plist_path=plist_path,
    )
    assert calls == [
        ["/bin/launchctl", "print", f"gui/501/{launcher.LOGIN_AGENT_LABEL}"],
        ["/bin/launchctl", "bootstrap", "gui/501", str(plist_path)]
    ]

    launcher.disable_login_startup(plist_path=plist_path)
    assert not plist_path.exists()


def test_login_agent_rejects_non_app_executable(tmp_path) -> None:
    launcher = _load_launcher()

    try:
        launcher.enable_login_startup(
            executable=tmp_path / "python",
            plist_path=tmp_path / "agent.plist",
        )
    except RuntimeError as exc:
        assert "打包后的" in str(exc)
    else:
        raise AssertionError("non-app executable unexpectedly accepted")
