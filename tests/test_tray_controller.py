from __future__ import annotations

from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path


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
