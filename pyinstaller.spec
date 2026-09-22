# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import runpy

from PyInstaller.utils.hooks import collect_data_files

root = Path(SPECPATH)
icon_tools = runpy.run_path(str(root / "tools" / "generate_app_icon.py"))
ensure_app_icon = icon_tools["ensure_app_icon"]

# Sun Valley is a Tcl theme and must be collected explicitly. User logos are
# persistent runtime data and are never embedded in the executable.
theme_assets = collect_data_files("sv_ttk")
icon_path = ensure_app_icon(
    root / ".generated-assets" / "VoucherManagement.ico"
)
app_assets = [(str(icon_path), "assets")]
font_assets = [
    (str(path), "assets/fonts")
    for path in sorted((root / "assets" / "fonts").glob("*.ttf"))
]

a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=theme_assets + app_assets + font_assets,
    hiddenimports=[],
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
    name="VoucherManagement",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(root / "windows_version_info.txt"),
    icon=str(icon_path),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="VoucherManagement",
)
