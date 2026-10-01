# -*- mode: python ; coding: utf-8 -*-
"""Internal API-key-only desktop build."""
from PyInstaller.utils.hooks import collect_submodules


hiddenimports = (['server.app'] + collect_submodules('uvicorn') + collect_submodules('webview') +
                 collect_submodules('fastapi') + collect_submodules('starlette') +
                 collect_submodules('pydantic') + collect_submodules('httpx') +
                 collect_submodules('anyio') + collect_submodules('server') +
                 collect_submodules('core') + collect_submodules('generator') +
                 collect_submodules('adapters'))

a = Analysis(
    ['D:/uagent/launcher.py'],
    pathex=['D:/uagent'],
    binaries=[],
    datas=[('D:/uagent/frontend/dist', 'frontend/dist'),
           ('D:/uagent/packaging/internal-byok.enabled', '.')],
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
    name='UAgent-Agent-Enhanced-0.4.4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
)
