# OMX Runbook Review — psql93_mig_tool / db_migration_tool

> Scope: macOS 로컬에서 프로젝트를 복제/실행 가능한 상태로 만들고, 품질 게이트(테스트/린트/타입체크/빌드)를 기준으로 현재 상태를 평가한다.

## 0) Repo / 위치
- Repo: https://github.com/isinthesky/psql93_mig_tool
- 로컬(clone): `repos/psql93_mig_tool`
- 앱 프로젝트: `repos/psql93_mig_tool/db_migration_tool`

## 1) Quickstart (macOS)
```bash
cd db_migration_tool

# 1) venv + core deps
make setup

# 2) 실행
make run

# 3) 테스트
make test
```

## 2) Quality Gates (현재 상태)

### ✅ Unit/Integration Tests
- Command: `make test`
- Status: **PASS** (190 passed)

### ⚠️ Formatter
- Command: `make format-check`
- Status: **FAIL**
- 요약: `ruff format --check` 기준으로 다수 파일이 reformat 필요.

### ⚠️ Lint (ruff)
- Command: `uv run ruff check src/ tests/`
- Status: **FAIL**
- 주요 유형:
  - import 정렬(I001), unused import(F401)
  - typing 레거시/권장 변경(UP006/UP035)
  - closure/loop var 바인딩 경고(B023) — `copy_migration_worker.py` 내부 스레드/클로저 패턴

### ⚠️ Typecheck (mypy)
- Command: `uv run mypy src/`
- Status: **FAIL** (다수 오류)
- 대표 원인:
  - `pyproject.toml`은 `requires-python >= 3.9`인데, 코드에 `X | Y` union 문법(파이썬 3.10+)이 다수 존재
  - PySide6 타입 스텁/enum 관련 attr 오류가 매우 많이 발생
  - SQLAlchemy ORM 모델 타입 힌트/베이스 클래스 구성 관련 오류

### (선택) Build
- Command: `make build` / `make build-mac`
- Status: **미측정** (현재 리뷰 범위에서는 테스트/정적검사까지만 수행)

## 3) 아키텍처 개요

### 핵심 컴포넌트
- GUI: PySide6(Qt6)
- Migration engine:
  - `CopyMigrationWorker` (psycopg2 기반 COPY 스트리밍) — 고성능 경로
  - `MigrationWorker` (psycopg3 기반 INSERT 배치) — 레거시/비권장 경로
- 체크포인트/이력:
  - `src/models/history.py` + `src/database/*` (로컬 DB)
- 프로필 저장:
  - `src/models/profile.py`에서 Fernet로 source/target config 암호화

### 데이터 흐름(요약)
1) 사용자가 프로필 선택/생성 → 로컬 DB 저장(암호화)
2) 날짜 범위 선택 → 파티션 디스커버리(`PartitionDiscovery`)
3) 마이그레이션 시작 → 워커 스레드 실행
4) 각 파티션에 대해 체크포인트 조회/갱신 → 중단/재개 지원

## 4) 이번 작업에서 확인/수정한 버그(테스트 안정성)

### (Fix) `resume` 플래그가 `resume()` 메서드를 덮어쓰는 문제
- 원인: `BaseMigrationWorker.__init__`에서 `self.resume = resume`로 bool을 저장 → 인스턴스에서 `resume()` 메서드가 호출 불가
- 영향: 일시정지/재개 경로에서 스레드가 조용히 예외 후 정지 → `_check_pause()`가 풀리지 않아 테스트/런타임이 멈출 수 있음
- 조치: 플래그명을 `self.should_resume`로 변경

### (Fix/정합) 체크포인트 캐싱 호출 순서
- 테스트/모듈 결합도를 낮추기 위해, 체크포인트 캐싱을 **DB 연결 생성 이전**에 수행하도록 순서를 조정

## 5) 보안/운영 리스크 체크
- **프로필 암호화 키가 코드에 하드코딩** (`ProfileManager._get_or_create_cipher`)
  - 위험: 바이너리/소스 유출 시 모든 프로필(비밀번호 포함) 복호화 가능
  - 권장: macOS Keychain/Windows DPAPI, 또는 사용자별 키를 안전 저장소에 저장 + 키 로테이션 전략

## 6) 권장 TODO (우선순위)

### P0 (품질 게이트 정리)
- `make format` 적용 후 커밋 (자동 포맷 일관성)
- `ruff check --fix`로 import/unused 정리

### P1 (Python 버전 정책 정리)
- 옵션 A) 최소 버전 **3.10+로 상향** (현재 코드 스타일과 일치)
- 옵션 B) 3.9 유지 시 `X | Y` 문법 제거(Union/Optional로 회귀)

### P1 (타입체크 현실화)
- PySide6 스텁/enum 오류는 현실적으로 `ignore_missing_imports`만으로 해결이 어려움
- `mypy` 적용 범위를 단계적으로 축소/확장(예: core 로직부터)하거나, Qt 계층은 `# type: ignore[attr-defined]` 등으로 타협 필요

### P2 (COPY 워커 B023)
- `copy_migration_worker.py`의 스레드/클로저에서 loop var 바인딩 경고는 실제 버그 가능성도 있으므로 구조 개선 권장
  - 예: `def copy_out(copy_to_query=copy_to_query, stream_buffer=stream_buffer): ...` 형태로 바인딩 명시

## 7) 재현 가능한 체크리스트
- [ ] `make setup`
- [ ] `make test` (PASS)
- [ ] `make format-check` (현재 FAIL)
- [ ] `uv run ruff check src/ tests/` (현재 FAIL)
- [ ] `uv run mypy src/` (현재 FAIL)

---

### Notes
- 이 문서는 “현재 상태를 있는 그대로” 기록한 리뷰이며, 포맷/린트/타입 오류를 해결하는 대규모 정리 작업은 별도 브랜치에서 진행하는 것을 권장합니다.
