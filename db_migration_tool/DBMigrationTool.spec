# -*- mode: python ; coding: utf-8 -*-

import os
import glob

# VC++ Runtime DLLs 번들링 (대상 PC에 VC++ 미설치 시 QtCore 로드 실패 방지)
pyside_dir = os.path.dirname(__import__('PySide6').__file__)
vcrt_dlls = []
for pattern in ['msvcp*.dll', 'vcruntime*.dll', 'concrt*.dll']:
    vcrt_dlls.extend(
        (dll, '.') for dll in glob.glob(os.path.join(pyside_dir, pattern))
    )

a = Analysis(
    ['src\\main.py'],
    pathex=[],
    binaries=vcrt_dlls,
    datas=[('resources', 'resources')],
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
    a.binaries,
    a.datas,
    [],
    name='DBMigrationTool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['resources\\icons\\psql_migration_tool.ico'],
)
