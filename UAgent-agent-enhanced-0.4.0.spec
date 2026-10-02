# -*- mode: python ; coding: utf-8 -*-
"""Local-only desktop build with deterministic planning."""
from PyInstaller.utils.hooks import collect_submodules


hiddenimports = (['server.app'] + collect_submodules('uvicorn') + collect_submodules('webview') +
                 collect_submodules('fastapi') + collect_submodules('starlette') +
                 collect_submodules('pydantic') +
                 collect_submodules('anyio') + collect_submodules('server') +
                 collect_submodules('core') + collect_submodules('generator') +
                 collect_submodules('adapters') +
                 ['tools.runtime_convert', 'tools.cross_validate', 'tools.interaction_probe'])

a = Analysis(
    ['D:/uagent/launcher.py'],
    pathex=['D:/uagent'],
    binaries=[],
    datas=[('D:/uagent/frontend/dist', 'frontend/dist')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['node', 'npm'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='UAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)

# A console entry for unattended conversion, using the same bundled compiler.
# Keep the desktop entry unchanged for existing users.
converter = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name='UAgent-Convert', debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, console=True, disable_windowed_traceback=False,
)
