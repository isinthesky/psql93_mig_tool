# DB Migration Tool 안내

- **문서 경로**: `CLAUDE.md`
- **레이어**: 프로젝트 루트 (전체 제품 개요)
- **역할**: 신규 기여자에게 프로젝트 목표, 구조, 실행 흐름을 빠르게 공유합니다.

## 프로젝트 개요
DB Migration Tool은 PostgreSQL 파티션 테이블을 다른 데이터베이스로 안전하게 옮기기 위한 데스크톱 애플리케이션입니다. 날짜 범위를 지정한 부분 마이그레이션, 중단 후 재개, 진행률 및 로그 모니터링을 지원하여 운영 중단 없이 파티션 데이터 이동을 돕습니다.

## 기술 구조
- `src/`: 애플리케이션의 핵심 Python 패키지. UI, 코어 마이그레이션 엔진, 데이터 계층이 모두 포함됩니다.
- `migrations/`: 로컬 SQLite/SQLAlchemy 스키마를 관리하기 위한 마이그레이션 스크립트.
- `docs/`: 기능 분석 및 기획 문서 모음.
- `tests/`: 핵심 동작을 검증하는 단위 테스트와 통합 테스트.
- `build*`, `dist/`: PyInstaller 기반 배포 산출물과 빌드 스크립트.
- `assets/`, `resources/`: UI에서 사용하는 아이콘과 번역 리소스.

## 실행 및 배포
- 개발 실행: `python src/main.py`
- 빌드: `pyinstaller db_migration_tool.spec` (Windows), `build_mac.sh` (macOS)
- 주요 진입점: `src/main.py`에서 UI를 초기화하고 사용자 상호작용을 처리합니다.

## 주요 동작 흐름
1. 사용자가 UI에서 연결 프로필을 생성하거나 선택합니다.
2. `src/core/partition_discovery.py`가 대상 파티션을 분석합니다.
3. 사용자 선택에 따라 `CopyMigrationWorker` 또는 `MigrationWorker`가 데이터 이동을 수행합니다.
4. 진행 상태와 로그는 PySide6 시그널을 통해 UI로 전달되고, 이력은 `HistoryManager`가 저장합니다.

## 관련 문서
- `src/CLAUDE.md`: 애플리케이션 패키지 구조와 하위 모듈 책임을 설명합니다.
- `src/core/CLAUDE.md`: 마이그레이션 엔진의 구성과 동작을 다룹니다.
- `src/ui/CLAUDE.md`: PySide6 기반 프리젠테이션 레이어 구조를 설명합니다.
