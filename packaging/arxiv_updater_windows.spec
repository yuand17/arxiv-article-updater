from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPEC).resolve().parents[1]
package_datas, package_binaries, package_hiddenimports = collect_all("arxiv_updater")
hiddenimports = sorted(
    set(
        package_hiddenimports
        + collect_submodules("arxiv_updater")
        + [
            "keyring.backends.Windows",
            "keyring.backends.null",
            "pystray._win32",
            "uvicorn.lifespan.on",
            "uvicorn.loops.auto",
            "uvicorn.protocols.http.auto",
            "uvicorn.protocols.websockets.auto",
        ]
    )
)
datas = package_datas + [
    (str(project_root / "alembic.ini"), "arxiv_updater"),
    (str(project_root / "alembic"), "arxiv_updater/alembic"),
]

a = Analysis(
    [str(project_root / "scripts" / "launch_arxiv_updater.pyw")],
    pathex=[str(project_root / "src")],
    binaries=package_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="arXiv Updater",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(
        project_root
        / "src"
        / "arxiv_updater"
        / "static"
        / "icons"
        / "arxiv-updater.ico"
    ),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="arXiv Updater",
)
