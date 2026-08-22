from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


project_root = Path(SPEC).resolve().parents[1]
package_datas, package_binaries, package_hiddenimports = collect_all("arxiv_updater")
hiddenimports = sorted(
    set(
        package_hiddenimports
        + collect_submodules("arxiv_updater")
        + [
            "keyring.backends.macOS",
            "keyring.backends.null",
            "pystray._darwin",
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
    [str(project_root / "scripts" / "launch_arxiv_updater_macos.py")],
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
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="arXiv Updater",
)
app = BUNDLE(
    coll,
    name="arXiv Updater.app",
    icon=str(
        project_root
        / "src"
        / "arxiv_updater"
        / "static"
        / "icons"
        / "arxiv-updater-icon.png"
    ),
    bundle_identifier="com.yuand17.arxiv-updater",
    version="0.2.0",
    info_plist={
        "CFBundleDisplayName": "arXiv Updater",
        "LSMinimumSystemVersion": "13.0",
        "LSMultipleInstancesProhibited": True,
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    },
)
