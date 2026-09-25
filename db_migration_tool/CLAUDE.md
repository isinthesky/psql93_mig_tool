# DB Migration Tool — CLAUDE.md

## 1. Scope
- PySide6(Qt6) GUI. PostgreSQL 9.3의 일별 파티션 테이블(`point_history_YYMMDD` 등,
  트리거 함수 `point_history_partition_insert()` 기반 inheritance+trigger 파티셔닝을
  target에 새로 생성)을 소스 `bms93`(PG 9.3)에서 대상 `bms30`(PG 16)로 이관한다.
- 부가 기능: 파일 아카이브 export/import 프로필(`src/core/file_archive_workers.py`,
  `src/ui/dialogs/file_archive_migration_dialog.py`).
- 버전 `1.2.5` (`src/version.py`, `pyproject.toml`, `installer/DBMigrationTool.iss` 세 곳
  동기화, `tools/bump_version.py`가 관리 — 손으로 고치지 않는다).
- `requires-python = ">=3.13"` (`pyproject.toml`), uv로 관리.

## 2. Environments
### Windows dev/build host — `my-wsl-01` (primary)
- 저장소: `C:\Users\hijde\Apps\psql93_mig_tool` (앱 루트 `db_migration_tool\`), `.venv` CPython 3.13.12.
- 실행/빌드: `run_dev.bat`(개발 실행), `build.bat`(PyInstaller), `installer\build_installer.bat`
  (Inno Setup, `installer\DBMigrationTool.iss`).
- 사용자 데이터: `%APPDATA%\db_migration.db` (연결 프로필·이력 SQLite, 연결 정보 암호화 저장).
- `make`는 이 호스트에 없다 — `uv run` / `.bat`을 직접 쓴다.

### Mac copy (editing only)
- 이 사본에서 코드를 읽고 편집한다. `Makefile`의 `make check` / `make test` 등은
  **Mac에서만** 동작을 기대한다(Windows 호스트에는 make 없음).
- Windows가 앞서 있을 수 있으니(원격 설치 산출물이 최종 산출물) 수정 전 최신화 여부를 확인한다.

### Test DBs — `macmini-sub` (검증용)
- `bms93`: `facreport.iptime.org:5446` (PG 9.3, source, 대체본 없는 유일한 9.3 원본). fac-report 운영도 이 DB를 읽으므로 **읽기만 한다(쓰기·DDL 금지)**.
- `bms30`: `facreport.iptime.org:5445` (PG 16, target). `temp` DB는 이관 중간/비교용 scratch target.
- 역할 계정 `migtool`(비 superuser)로 검증한다. **`bms30`의 기존 파티션은 사용자 확인 없이
  덮어쓰거나 지우지 않는다** — 테스트 이관 대상은 `temp` DB나 새 날짜 파티션으로 한정.
- 두 컨테이너가 `healthy`인지 검증 전에 먼저 확인한다(외장 볼륨 분리 이력 있음).

## 3. Code map (`src/`)
- `core/` — 마이그레이션 엔진. `copy_migration_worker.py`(1558줄, COPY 스트리밍 워커 — 유일한
  이관 워커. 취소·원본 snapshot 규칙은 `src/core/CLAUDE.md`. legacy INSERT/OFFSET 워커는 M-03으로 삭제),
  `table_creator.py`(대상 테이블·파티션·트리거 DDL 생성), `partition_discovery.py`
  (날짜별 파티션 탐색), `base_migration_worker.py`(공통 워커 기반), `scan_workers.py`,
  `file_archive_workers.py`(파일 export/import), `archive_manifest.py`(아카이브 manifest),
  `performance_metrics.py`, `table_types.py`.
- `ui/` — `main_window.py`, `tray_icon.py`, `theme.py`(디자인 토큰, 전역 스타일 소스),
  `dialogs/`(`connection_dialog.py`, `migration_wizard_dialog.py`(2001줄, 최대 다이얼로그),
  `file_archive_migration_dialog.py`, `history_dialog.py`, `license_dialog.py`,
  `log_viewer_dialog.py`, `scan_host.py`, `connection_mapper.py`), `viewmodels/`, `widgets/`.
- `database/` — `local_db.py`(APPDATA SQLite), `repository.py`, `postgres_utils.py`,
  `version_info.py`/`version_params.py`/`version_sql.py`(PG 버전별 SQL 분기).
- `models/` — `profile.py`(연결 프로필, 암호화 저장), `saved_connection.py`, `history.py`
  (작업 이력/체크포인트).
- `licensing/` — §5 참고. `utils/` — `app_paths.py`, `enhanced_logger.py`/`logger_config.py`/
  `logger_mixins.py`, `validators.py`.
- `main.py` — 엔트리포인트: 싱글 인스턴스 가드 → DB 초기화 → 라이선스 확인(1회) → 트레이 →
  메인 윈도우. `version.py` — 단일 버전 소스(§1).

## 4. Commands
### Mac (품질 게이트)
```bash
make check   # format-check + lint + typecheck (ruff, mypy)
make test    # pytest tests/ -v --tb=short (test-unit / test-integration / test-cov 개별 타깃도 있음)
```

### Windows (`my-wsl-01`, `db_migration_tool\`)
```bat
uv run ruff format --check src tests && uv run ruff check src tests && uv run mypy src
uv run pytest -m "not integration" -v      :: 단위 테스트 — 978 passed (2026-09-25 wave1 병합 후)
set DBMIG_RUN_REAL_PROFILE_TESTS=1&& .venv\Scripts\python.exe -m pytest -m integration -k saved_profiles -q
                                            :: 실DB 연결 검증(APPDATA 프로필의 source=bms93, target=bms30) — 1 passed
build.bat                                  :: PyInstaller -> dist\DBMigrationTool.exe
installer\build_installer.bat              :: Inno Setup -> dist\installer\DBMigrationTool-Setup-<ver>.exe
```
- ⚠️ **`build.bat`은 빌드 전에 `tools/bump_version.py`로 패치 버전을 자동으로 올린다**(59행). 빌드 전에
  손으로 올리지 않는다. 빌드 후 Windows에 생긴 버전 4파일 변경(pyproject/version.py/.iss/uv.lock)을 커밋·push하고
  `v<버전>` 태그를 단다. (2026-09-25: 수동 1.2.6 + 빌드 자동 → 1.2.7로 출시, v1.2.6 태그는 삭제)
- 원격 실행 시 `cmd /c "build.bat < NUL"` — 오류 경로의 `pause`가 입력 대기로 멈추지 않게 한다.
- `.gitattributes` 도입 이전 체크아웃에는 `.bat`이 LF로 남아 있을 수 있다(cmd가 한글 줄을 오해석해
  `build_installer.bat`이 실패하던 원인). 증상이 보이면 스크립트를 지우고 `git checkout -- <file>`로 CRLF 재생성.
- 산출물은 **미서명**(Authenticode NotSigned). 1.2.7 해시: exe `7221FF87…0EBE0`, 설치본 `9569F99D…23744`.
  `installer\codesign.ps1` 훅이 `CODESIGN_*` 환경변수가 있으면 서명·검증하고 없으면 `UNSIGNED BUILD` 경고(BUILD_GUIDE).
- `build.bat`은 `uv sync --locked`로 **추적되는 `uv.lock`**을 강제하고, `build_installer.bat`은
  `installer\prerequisites.sha256` 고정 해시 + Microsoft 서명 검증 gate를 통과해야 ISCC를 돌린다(감사 M-08/M-09).
- pytest marker: `unit` / `integration` / `slow` (`pyproject.toml` `[tool.pytest.ini_options]`).
  실DB를 쓰는 통합 테스트는 기본적으로 건너뛰므로(§6 감사 결과), `-m "not integration"` 결과만으로
  COPY/재개 경로의 정합성을 보장하지 않는다.

## 5. Licensing contract (DBMT1)
- 키 형식: `DBMT1-<payload_b32>-<signature_b32>` (Ed25519 서명, payload는 재직렬화 없이 그대로 서명 검증).
- 검증(앱 쪽): `src/licensing/payload.py` (`verify_key()`), 공개키는 `src/licensing/keys.py`.
- 발급(서버 쪽): `/Users/inthesky/lgetech/lgetech-license-server/app/licensing.py`.
- 공개키: `NIVHG532XSXX7YERKROEOP3SKSP5VNODHIJ7URFKQ43Y747KR5DQ`
  (`src/licensing/keys.py`의 `LICENSE_PUBLIC_KEY_B32`, 발급 서버의 서명키와 쌍).
  **키 형식·서명 규칙을 바꾸면 앱/서버 양쪽을 함께 수정**하고 공개키 일치 여부를 확인한다.
- 공용 테스트 벡터: `tests/licensing/fixtures/dbmt1_vectors.json` + `tests/licensing/test_dbmt1_vectors.py`.
  원본 생성기는 서버 `scripts/dbmt1_test_vectors.py`(테스트 전용 시드). 파일을 이쪽에서 직접 고치지 않는다.
- 검사 진입점은 `check_license()`(`src/licensing/__init__.py`) 하나뿐이며 앱 시작 시 1회만 호출한다
  (§6 H-07 참고).

## 6. ⚠️ Known open issues
`docs/plans/code-audit-remediation-2026-08-26.md` 기준(commit `3813ac3`, app 1.2.5) — 이 계획을
구현하고 재감사를 통과하기 전에는 **데이터 이관용 운영 배포에 적합하다고 승인할 수 없다.**

**2026-09-25 수정 — v1.2.7로 출시(main `027be89`)**: C-01, C-02, H-07, H-01(워커 측) 해결 +
모든 복사 경로에 **파티션 완료 전 원본·대상 `COUNT(*)` 일치 검증** 추가. 상세·검증 결과는 감사 문서 §8.
아래 원문 설명은 수정 전 상태 기록이다.

**2026-09-25 wave1 병합(main)**: H-05, H-06, H-08, H-09, M-02, M-04~M-10, M-12 해결, H-02·H-04는 연결 단계만,
M-11은 서명 훅만(인증서 없음). 상세와 남은 리뷰 지적은 감사 문서 §8.1~§8.3. 나머지(H-01 UI, H-03, M-01, M-03,
M-13, M-14 등)는 미해결.

**2026-09-25 wave2 병합(main)**: H-02(연결 수립 단계는 `connect_timeout` 상한), H-03(한계 3), H-04, M-01, M-03(legacy
워커 삭제), M-14 해결. 상세·남은 리뷰 지적·동작 변경은 감사 문서 §8.4. 미해결: H-01 UI, M-11 인증서, M-13.

- **C-01** — `CopyStreamBuffer.close()`가 큐 포화 시 취소 상태를 설정하지만 `read()`가 남은 데이터를
  비우지 않고 EOF를 반환한다. 대상에는 앞부분만 반영되는데 checkpoint는 전진할 수 있다
  (`src/core/copy_migration_worker.py:87-145,803-857`).
- **C-02** — COPY payload를 `split("\n")`/`split(",")`로 해석해 CSV 따옴표 안의 쉼표·줄바꿈·escaped
  quote·chunk 경계를 처리하지 못한다. 잘못된 재개 키가 기록되어 다음 범위를 건너뛸 수 있다
  (`src/core/copy_migration_worker.py:147-186,728-869`).
- **H-07** — `check_license()` 예외가 unrestricted로 fail-open 된다. `.activation` I/O 오류 등으로
  라이선스 제한을 우회할 수 있다 (`src/main.py:103-115`, `src/ui/main_window.py:374-377`).
- 그 외 H-01~H-09, M-01~M-14(H-05 `sslmode=require`로 hostname 미검증, M-09 VC++ prerequisite
  서명 미검증 등)는 문서 §3을 직접 참고한다.
- **규칙**: COPY/checkpoint/resume 관련 코드를 건드리면 재개(resume) 경로에 대한 테스트를 반드시
  추가한다(`AGENTS.md` Testing Guidelines와 동일 원칙).

## 7. Conventions
- 줄바꿈은 루트 `.gitattributes`가 규정: 저장소는 기본 LF, `*.py`는 명시적 LF,
  `*.bat`/`*.cmd`/`*.ps1`/`*.iss`는 저장소에 LF로 저장되고 체크아웃 시 작업 트리에서만 CRLF가 된다.
- `run_dev.bat`은 CP949(한글 Windows 콘솔 코드페이지)로 저장되어 있다 — UTF-8로 재인코딩하지 않는다.
- 커밋 메시지는 짧게, 한국어 가능.
