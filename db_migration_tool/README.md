# DB Migration Tool

PostgreSQL 파티션 테이블 마이그레이션 도구

## 기능

- PostgreSQL 9.3+ 지원
- 파티션 테이블 자동 인식
- 날짜 범위 기반 선택적 마이그레이션
- 실시간 진행 모니터링 (5초 단위 업데이트)
- 중단/재개 기능 (테이블 단위)
- 연결 프로필 저장 (암호화)
- 작업 이력 관리

## 개발 환경 설정

### 전제 조건

- Python 3.9+
- [uv](https://github.com/astral-sh/uv) (빠른 Python 패키지 관리자)

**uv 설치:**

```bash
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# pip으로 설치
pip install uv
```

### Make 사용 (권장)

#### 1. 초기 설정 (가상환경 + 의존성 설치)

```bash
make setup
```

#### 2. 개발 모드 실행

```bash
make run
```

#### 3. 빌드 (실행 파일 생성)

```bash
make build
```

#### 4. 테스트 실행

```bash
make test
```

#### 5. 정리

```bash
make clean        # 빌드 산출물만 정리
make clean-all    # 가상환경 포함 모두 정리
```

#### 사용 가능한 모든 명령어 보기

```bash
make help
```

### 수동 설정 (Make 없이)

#### 1. 가상환경 생성 및 의존성 설치

```bash
# 가상환경 생성
uv venv

# 가상환경 활성화
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS/Linux

# 의존성 설치
uv pip install -e .
uv pip install -e ".[dev,test]"  # 개발 도구 포함
```

#### 2. 개발 모드 실행

```bash
python src/main.py
```

#### 3. 빌드

```bash
python -m PyInstaller db_migration_tool.spec --clean  # Windows
python -m PyInstaller build_mac.spec --clean  # macOS
```

### Windows 전용 배치 스크립트

Windows에서 Make를 사용할 수 없는 경우:

```batch
build.bat  # 빌드 실행
```

## 빌드 결과물

- **Windows**: `dist/DBMigrationTool.exe`
- **macOS**: `dist/DB Migration Tool.app`

## 사용 방법

1. **연결 프로필 생성**
   - 새 연결 버튼 클릭
   - 소스/대상 데이터베이스 정보 입력
   - 연결 테스트 후 저장

2. **마이그레이션 실행**
   - 프로필 선택
   - 마이그레이션 시작 버튼 클릭
   - 날짜 범위 선택
   - 시작 버튼 클릭

3. **진행 상황 모니터링**
   - 실시간 진행률 확인
   - 처리 속도 및 예상 완료 시간 확인
   - 필요시 일시정지/재개

## 기술 스택

- Python 3.9+
- PySide6 (Qt6)
- psycopg3
- SQLAlchemy
- qdarktheme

## 라이선스

Proprietary