#!/bin/bash

# DB Migration Tool macOS 빌드 스크립트 (uv)

set -e  # 오류 발생 시 즉시 중단

echo "=========================================="
echo "🔧 DB Migration Tool 빌드 시작 (uv)"
echo "=========================================="
echo

# uv 설치 확인
if ! command -v uv &> /dev/null; then
    echo "❌ uv가 설치되어 있지 않습니다."
    echo
    echo "uv를 설치하려면 다음 명령을 실행하세요:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo "  또는: pip install uv"
    echo
    exit 1
fi
echo "✅ uv found"

# 가상환경 확인 및 생성
if [ ! -d ".venv" ]; then
    echo "📦 가상환경을 생성합니다..."
    uv venv
    echo "✅ 가상환경 생성 완료"
fi

# 가상환경 활성화
if [ -f ".venv/bin/activate" ]; then
    echo "📦 가상환경 활성화..."
    source .venv/bin/activate
    echo "✅ 가상환경 활성화 완료"
else
    echo "❌ 가상환경을 찾을 수 없습니다."
    exit 1
fi

# 개발 의존성 설치 (PyInstaller 포함)
echo
echo "📋 개발 의존성을 설치합니다..."
uv pip install -e ".[dev]"
echo "✅ 의존성 설치 완료"

# 이전 빌드 정리
echo
echo "🧹 이전 빌드 정리..."
rm -rf build dist
echo "✅ 정리 완료"

# PyInstaller로 빌드
echo
echo "=========================================="
echo "🏗️ 애플리케이션 빌드 중..."
echo "=========================================="
python -m PyInstaller build_mac.spec --clean

# 빌드 성공 확인
if [ -d "dist/DB Migration Tool.app" ]; then
    echo "✅ 빌드 성공!"
    echo "📍 위치: dist/DB Migration Tool.app"
    
    # 앱 크기 확인
    SIZE=$(du -sh "dist/DB Migration Tool.app" | cut -f1)
    echo "📊 앱 크기: $SIZE"
    
    # DMG 생성 (선택사항)
    read -p "DMG 파일을 생성하시겠습니까? (y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "💿 DMG 생성 중..."
        # create-dmg 설치 확인
        if ! command -v create-dmg &> /dev/null; then
            echo "create-dmg 설치 중..."
            brew install create-dmg
        fi
        
        # DMG 생성
        create-dmg \
            --volname "DB Migration Tool" \
            --window-pos 200 120 \
            --window-size 600 400 \
            --icon-size 100 \
            --icon "DB Migration Tool.app" 150 150 \
            --hide-extension "DB Migration Tool.app" \
            --app-drop-link 450 150 \
            "DB_Migration_Tool.dmg" \
            "dist/"
            
        if [ -f "DB_Migration_Tool.dmg" ]; then
            echo "✅ DMG 생성 완료: DB_Migration_Tool.dmg"
        fi
    fi
else
    echo "❌ 빌드 실패"
    exit 1
fi

echo "🎉 빌드 프로세스 완료!"