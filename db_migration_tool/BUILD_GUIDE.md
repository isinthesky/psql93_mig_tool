# DB Migration Tool 빌드 가이드

이 문서는 uv 기반 현대적 개발 환경에서 DB Migration Tool을 빌드하는 방법을 안내합니다.

## 사전 요구사항

- **Python 3.9 이상**
- **uv** (Python 패키지 관리자)
- **macOS** (현재 macOS만 지원)

### uv 설치

```bash
# Homebrew로 설치
brew install uv

# 또는 공식 설치 스크립트
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 개발 환경 설정

### 1. 의존성 설치

```bash
# 프로덕션 의존성만 설치
make install

# 개발 의존성 포함 전체 설치 (권장)
make install-dev

# 또는 uv 직접 사용
uv sync --all-extras
```

### 2. 개발 모드 실행

```bash
# Makefile 사용
make dev

# 또는 uv 직접 사용
uv run python src/main.py
```

## 코드 품질 관리

### 린트 및 포맷팅

```bash
# 코드 린트 검사
make lint

# 린트 자동 수정
make lint-fix

# 코드 포맷팅
make format

# 포맷팅 검사만 (수정 없이)
make format-check

# 타입 체크
make typecheck

# 전체 코드 품질 검사 (format + lint + typecheck)
make check
```

## 테스트

### 테스트 실행

```bash
# 전체 테스트
make test

# 커버리지 포함 테스트
make test-cov

# 단위 테스트만
make test-unit

# 통합 테스트만
make test-integration
```

테스트 커버리지 리포트는 `htmlcov/index.html`에서 확인할 수 있습니다.

## 애플리케이션 빌드

### macOS 앱 빌드

```bash
# Makefile 사용 (권장)
make build-mac

# 또는 직접 빌드 스크립트 실행
./build_mac.sh

# 클린 빌드 (전체 정리 후 빌드)
make clean-build
```

빌드 결과: `dist/DB Migration Tool.app`

### 빌드 산출물 정리

```bash
# 빌드 아티팩트 및 캐시 삭제
make clean
```

## DMG 생성 (배포용)

### Homebrew로 create-dmg 설치

```bash
brew install create-dmg
```

### DMG 생성

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

## Windows 인스톨러 (배포용)

### 1. 실행 파일 빌드

```bash
python -m PyInstaller DBMigrationTool.spec --clean --noconfirm
```

`build.bat`도 같은 일을 하지만 **`dist` 폴더를 통째로 지운다.** 과거 릴리스
아티팩트를 dist에 보관 중이라면 위 명령을 직접 쓴다.

### 2. 인스톨러 빌드

```bash
installer\build_installer.bat
```

산출물: `dist\installer\DBMigrationTool-Setup-<버전>.exe`

Inno Setup이 필요하다:

```bash
winget install --id JRSoftware.InnoSetup
```

winget은 `%LOCALAPPDATA%\Programs\Inno Setup 6`에 설치한다.
`build_installer.bat`이 그곳과 Program Files 양쪽을 찾는다.

### 인스톨러가 하는 일

| 항목 | 내용 |
|---|---|
| VC++ 런타임 | 없을 때만 조용히 선설치. PySide6의 QtCore 로드 실패를 막는다 |
| 설치 위치 | 관리자 권한이 있으면 Program Files, 없으면 사용자 폴더 |
| 바로가기 | 시작 메뉴(항상), 바탕화면(선택) |
| 제거 | 프로그램 추가/제거에 등록 |
| Server 2012 R2 | KB2999226이 필요하다고 알린다(자동 설치 불가) |

무음 설치(사내 배포용):

```bash
DBMigrationTool-Setup-1.2.1.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

### 버전 관리

버전은 세 곳에 있다 — `pyproject.toml`(패키징), `src/version.py`(앱이 실행 중
표시), `installer/DBMigrationTool.iss`(인스톨러 이름·표시). **손으로 고치지 말고
`tools/bump_version.py`만 쓴다.** 따로 고치면 어긋난다(인스톨러는 1.2.1인데
정보 창은 1.0.0인 상태가 실제로 있었다).

```bash
python tools/bump_version.py            # patch +1 (1.2.1 -> 1.2.2)
python tools/bump_version.py --set 1.3.0   # 메이저·마이너는 직접 지정
python tools/bump_version.py --sync     # 올리지 않고 세 곳을 현재 값으로 정렬
python tools/bump_version.py --show     # 현재 버전 출력
```

`build.bat`은 빌드 직전에 patch를 자동으로 올린다. 따라서 **빌드할 때마다
버전이 1씩 증가**한다. 올리지 않고 다시 빌드하려면 PyInstaller를 직접 부른다:

```bash
python -m PyInstaller DBMigrationTool.spec --clean --noconfirm
```

메이저·마이너를 올릴 때는 `--set`으로 먼저 지정한 뒤 빌드한다(빌드가 patch를
한 번 더 올리므로, 원하는 값보다 1 낮게 지정한다).

### 주의

- `AppId`(GUID)는 **바꾸지 않는다.** 바꾸면 업그레이드가 아니라 별개 프로그램으로
  설치되어 이전 버전이 남는다.
- `.iss`는 **UTF-8 BOM**으로 저장한다. BOM이 없으면 Inno Setup이 시스템
  코드페이지로 읽어 한글이 깨진다. `bump_version.py`는 파일을 바이트로 읽고 써서
  인코딩을 보존한다 — 텍스트로 다시 쓰면 BOM과 한글이 깨진다.
- `build.bat`은 `dist`를 통째로 지우지 않는다. 인스톨러가 `dist\prerequisites\`를
  입력으로 쓰고 `dist\installer\`에 산출물이 쌓이기 때문이다.

---

## 의존성 관리

### 의존성 업데이트

```bash
# 의존성 업데이트
make update

# 또는
uv lock --upgrade
```

### 의존성 트리 확인

```bash
make deps

# 또는
uv tree
```

### 의존성 동기화

```bash
# uv.lock 기반 동기화
make sync

# 또는
uv sync
```

## 문제 해결

### 1. 코드 서명 문제

macOS Catalina 이상에서는 코드 서명이 필요할 수 있습니다:

```bash
# 개발용 임시 서명
codesign --force --deep --sign - "dist/DB Migration Tool.app"
```

### 2. 권한 문제

```bash
# 실행 권한 부여
chmod +x "dist/DB Migration Tool.app/Contents/MacOS/DB Migration Tool"
```

### 3. Gatekeeper 문제

처음 실행 시 "개발자를 확인할 수 없습니다" 오류가 나타나면:
1. Finder에서 앱을 우클릭
2. "열기" 선택
3. 경고 대화상자에서 "열기" 클릭

### 4. 테스트 실패

```bash
# 캐시 정리 후 재시도
make clean
make test
```

### 5. 의존성 충돌

```bash
# uv.lock 재생성
rm uv.lock
uv sync --all-extras
```

## 앱 아이콘 추가

### 1. 아이콘 생성

```bash
uv run python create_icon.py
```

### 2. icns 파일 생성

```bash
# iconset 폴더 생성
mkdir assets/icon.iconset
cp assets/icon_*.png assets/icon.iconset/

# icns 생성
iconutil -c icns assets/icon.iconset -o assets/icon.icns
```

### 3. spec 파일에 아이콘 추가

`build_mac.spec` 파일의 BUNDLE 섹션에서:

```python
app = BUNDLE(
    ...
    icon='assets/icon.icns',
    ...
)
```

## 배포 준비

### 1. 버전 정보 업데이트

`pyproject.toml`에서:

```toml
[project]
version = "1.0.0"
```

`build_mac.spec`의 info_plist에서:

```python
'CFBundleVersion': '1.0.0',
'CFBundleShortVersionString': '1.0.0',
```

### 2. 앱 공증 (Notarization)

App Store 외부 배포 시 필요:

```bash
xcrun altool --notarize-app \
    --primary-bundle-id "com.yourcompany.dbmigrationtool" \
    --username "your-apple-id@example.com" \
    --password "app-specific-password" \
    --file "DB_Migration_Tool.dmg"
```

## 테스트 검증

### 1. 기본 실행 테스트

```bash
open "dist/DB Migration Tool.app"
```

### 2. 콘솔 로그 확인

```bash
"dist/DB Migration Tool.app/Contents/MacOS/DB Migration Tool"
```

### 3. 기능 테스트 체크리스트

- [ ] 데이터베이스 연결
- [ ] 파일 시스템 접근
- [ ] 네트워크 연결
- [ ] 프로필 생성/수정/삭제
- [ ] 마이그레이션 실행
- [ ] 체크포인트 저장/복구

## CI/CD 파이프라인 시뮬레이션

```bash
# 전체 CI 프로세스 실행
make ci
```

이 명령은 다음을 순차적으로 수행합니다:
1. 개발 의존성 설치
2. 코드 품질 검사 (format + lint + typecheck)
3. 테스트 실행

## 유용한 명령어 요약

```bash
make help              # 사용 가능한 명령어 목록 표시
make install          # 프로덕션 의존성 설치
make install-dev      # 개발 의존성 포함 전체 설치
make dev              # 개발 모드 실행
make test             # 테스트 실행
make test-cov         # 커버리지 포함 테스트
make lint             # 린트 검사
make format           # 코드 포맷팅
make typecheck        # 타입 체크
make check            # 전체 품질 검사
make build-mac        # macOS 앱 빌드
make clean            # 빌드 아티팩트 정리
make ci               # CI 파이프라인 시뮬레이션
make deps             # 의존성 트리 표시
make update           # 의존성 업데이트
make version          # 프로젝트 버전 표시
```

## 기술 스택

- **패키지 관리**: uv (빠른 Python 패키지 관리자)
- **빌드 도구**: PyInstaller
- **테스트**: pytest, pytest-qt, pytest-cov
- **린트**: ruff (빠른 Python linter)
- **포맷팅**: ruff formatter
- **타입 체크**: mypy
- **의존성 정의**: pyproject.toml (PEP 621 표준)

## 추가 리소스

- [uv 공식 문서](https://github.com/astral-sh/uv)
- [ruff 공식 문서](https://docs.astral.sh/ruff/)
- [PyInstaller 문서](https://pyinstaller.org/)
- [PEP 621 - pyproject.toml](https://peps.python.org/pep-0621/)

## 문의

프로젝트 관련 문의사항은 이슈 트래커를 통해 제보해주세요.
