# DB Migration Tool 빌드 가이드

## macOS 앱 빌드 방법

### 사전 요구사항
- Python 3.9 이상
- 가상환경 활성화
- 모든 의존성 패키지 설치

### 빌드 단계

#### 1. 가상환경 활성화
```bash
source venv/bin/activate
```

#### 2. 의존성 확인
```bash
pip install -r requirements.txt
pip install pyinstaller
```

#### 3. 빌드 실행
```bash
./build_mac.sh
```

또는 수동으로:
```bash
pyinstaller build_mac.spec --clean
```

#### 4. 빌드 결과
- 앱 위치: `dist/DB Migration Tool.app`
- 더블클릭하여 실행

### DMG 생성 (배포용)

#### Homebrew로 create-dmg 설치
```bash
brew install create-dmg
```

#### DMG 생성
```bash
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
```

### 문제 해결

#### 1. 코드 서명 문제
macOS Catalina 이상에서는 코드 서명이 필요할 수 있습니다:
```bash
# 개발용 임시 서명
codesign --force --deep --sign - "dist/DB Migration Tool.app"
```

#### 2. 권한 문제
```bash
# 실행 권한 부여
chmod +x "dist/DB Migration Tool.app/Contents/MacOS/DB Migration Tool"
```

#### 3. Gatekeeper 문제
처음 실행 시 "개발자를 확인할 수 없습니다" 오류가 나타나면:
1. Finder에서 앱을 우클릭
2. "열기" 선택
3. 경고 대화상자에서 "열기" 클릭

### 앱 아이콘 추가

#### 1. 아이콘 생성
```bash
python create_icon.py
```

#### 2. icns 파일 생성
```bash
# iconset 폴더 생성
mkdir assets/icon.iconset
cp assets/icon_*.png assets/icon.iconset/

# icns 생성
iconutil -c icns assets/icon.iconset -o assets/icon.icns
```

#### 3. spec 파일에 아이콘 추가
`build_mac.spec` 파일의 BUNDLE 섹션에서:
```python
app = BUNDLE(
    ...
    icon='assets/icon.icns',
    ...
)
```

### 최적화 팁

#### 1. 앱 크기 줄이기
`build_mac.spec`에서 불필요한 패키지 제외:
```python
excludes=[
    'tkinter',
    'matplotlib',
    'numpy',
    'pandas',
    'scipy',
    'test',
    'unittest',
]
```

#### 2. 시작 속도 개선
- `--onefile` 옵션 대신 `--onedir` 사용 (현재 설정)
- 불필요한 모듈 import 제거

### 배포 준비

#### 1. 버전 정보 업데이트
`build_mac.spec`의 info_plist에서:
```python
'CFBundleVersion': '1.0.0',
'CFBundleShortVersionString': '1.0.0',
```

#### 2. 앱 공증 (Notarization)
App Store 외부 배포 시 필요:
```bash
xcrun altool --notarize-app \
    --primary-bundle-id "com.yourcompany.dbmigrationtool" \
    --username "your-apple-id@example.com" \
    --password "app-specific-password" \
    --file "DB_Migration_Tool.dmg"
```

### 테스트

#### 1. 기본 실행 테스트
```bash
open "dist/DB Migration Tool.app"
```

#### 2. 콘솔 로그 확인
```bash
"dist/DB Migration Tool.app/Contents/MacOS/DB Migration Tool"
```

#### 3. 권한 테스트
- 데이터베이스 연결
- 파일 시스템 접근
- 네트워크 연결

## Windows 빌드 (참고)

Windows에서 빌드하려면:
1. `build_win.spec` 파일 생성 (유사한 구조)
2. `console=True`로 설정 (디버깅용)
3. Windows 환경에서 빌드 실행

## Linux 빌드 (참고)

Linux에서 빌드하려면:
1. `build_linux.spec` 파일 생성
2. AppImage 또는 deb/rpm 패키지 생성 고려