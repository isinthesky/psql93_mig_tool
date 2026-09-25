# -*- mode: python ; coding: utf-8 -*-

import os
import glob
import sys

# exe 버전 리소스(탐색기 '자세히' 탭). build.bat 이 bump_version.py 로 올린 직후의
# src/version.py 를 읽어 인스톨러·앱 표시 버전과 같은 값을 넣는다.
sys.path.insert(0, os.path.join(SPECPATH, 'tools'))
from version_resource import build_version_info, read_app_version  # noqa: E402

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
    version=build_version_info(read_app_version(SPECPATH)),
)
