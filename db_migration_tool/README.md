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

### 1. 가상환경 생성

```bash
python -m venv venv
venv\Scripts\activate  # Windows
```

### 2. 의존성 설치

```bash
pip install -r requirements.txt
```

### 3. 개발 모드 실행

```bash
python src/main.py
```

## 빌드 (단일 실행 파일)

### Windows용 exe 파일 생성

```bash
pyinstaller db_migration_tool.spec
```

빌드된 파일은 `dist/DBMigrationTool.exe`에 생성됩니다.

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