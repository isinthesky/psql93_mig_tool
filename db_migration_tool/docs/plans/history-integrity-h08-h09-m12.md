# 이력 무결성 — H-08 · H-09 · M-12 설계와 운영 정책

- 작성일: 2026-09-25 · 브랜치 `audit/w1-hist`
- 감사 문서: `code-audit-remediation-2026-08-26.md` §3.2 H-08/H-09, §3.3 M-12

## 1. 무엇이 문제였나

| ID | 결함 |
|---|---|
| H-08 | 이력은 바뀔 수 있는 `profile_id`만 저장했다. 작업이 중단된 뒤 프로필의 source/target이나 방향을 바꾸면 남은 파티션이 새 endpoint로 복사됐고, 한 이력이 여러 대상에 나뉘어 적재됐다. |
| H-09 | history를 먼저 commit한 뒤 checkpoint를 파티션마다 따로 commit했다. N번째에서 실패하면 N-1개만 남았고, 재개는 남아 있는 checkpoint만 처리한 뒤 이력을 완료로 닫았다. |
| M-12 | 미완료 이력이 있는 프로필도 삭제할 수 있었다. 삭제하면 그 이력은 어느 화면에서도 재개할 수 없는 orphan이 됐다. |

## 2. 저장하는 불변 값 (`migration_history`, 전부 nullable)

| 컬럼 | 내용 |
|---|---|
| `plan_version` | 계획 형식 버전(현재 1). **NULL이면 legacy 이력** |
| `migration_mode` | `postgres_to_postgres` / `postgres_to_file` / `file_to_postgres` |
| `schema_name` | 대상 스키마(`public`) |
| `source_fingerprint`, `target_fingerprint` | 비밀을 뺀 endpoint identity의 SHA-256 |
| `endpoint_label` | 거부 안내용 `host:port/db → host:port/db`(사용자명·비밀번호 없음) |
| `planned_partitions` | 정렬·중복 없는 파티션 이름 JSON 배열 |
| `planned_count`, `planned_hash` | planned set 개수, 정렬 목록의 SHA-256 |
| `plan_fingerprint` | version·mode·schema·두 지문·count·hash를 묶은 SHA-256 |
| `legacy_adopted_at` | legacy 이력을 사용자가 확인하고 현재 연결에 묶은 시각 |

endpoint identity(`src/models/history.py::endpoint_identity`)
- PostgreSQL: `kind`, `host`(소문자·공백 제거), `port`, `database`, `username`
- File: `kind`, `archive_path`(`normpath` + `normcase`)
- **identity가 아닌 값**: `password`, `ssl`, `compat_mode`. 이 값들은 바뀌어도 재개를 막지 않는다.

## 3. 생성 — 한 트랜잭션 (H-09)

`HistoryManager.create_planned_history(profile, partitions, ...)`
→ `HistoryRepository.create_with_checkpoints()`가 한 세션 안에서 history INSERT → flush →
checkpoint bulk INSERT → commit한다. 어느 INSERT에서 실패하든 history까지 rollback된다.
중복·빈 파티션 이름, 빈 계획, 저장되지 않은 프로필은 쓰기 전에 `ValueError`로 거부한다.

두 마이그레이션 다이얼로그는 예외가 나면 "아무것도 기록되지 않았습니다"라고 알리고 워커를 시작하지 않는다.

## 4. 재개 — `HistoryManager.prepare_resume(history_id, profile)`

검사 순서와 판정

1. 이력이 없으면 `NOT_FOUND`.
2. `plan_version`이 NULL이면 `LEGACY`(§5).
3. **계획 무결성**: JSON 파싱, 정렬·중복 없음, `planned_count`, `planned_hash`, `plan_fingerprint`
   재계산 일치. 하나라도 다르면 `PLAN_INVALID`(차단).
4. **identity**: mode, schema, source/target 지문을 현재 프로필과 비교. 다르면
   `IDENTITY_CHANGED`(차단). 안내에 기록된 연결과 현재 연결 라벨을 함께 보여 준다.
5. **완전성**: 계획 밖 checkpoint가 있으면 `PLAN_INVALID`(차단). 계획에 있는데 checkpoint가 없으면
   **계획대로 pending checkpoint를 보충**한다(같은 트랜잭션에서 존재 여부 재확인, 중복 없음).
6. `OK` — 재개 대상 `pending` = 계획 중 `completed`가 아닌 파티션(계획 순서).

차단 판정(3·4·5의 차단)에서는 checkpoint를 쓰지 않는다. UI(`src/ui/dialogs/resume_guard.py`)는
차단 사유를 보여 준 뒤 **명시적 폐기**(→ `cancelled`)를 기본값 "아니오"로 묻는다.

## 5. legacy 이력(지문 없음) 재개 정책

기존 사용자 DB(Windows 기준 이력 56건 규모)의 이력은 새 컬럼이 전부 NULL이다.
가짜 계획을 backfill하지 않는다 — 과거 checkpoint가 H-09로 일부 빠졌을 수 있어서, 남은 행으로
계획을 만들면 그 누락을 '정상'으로 고정해 버린다.

1. 자동 재개하지 않는다. `prepare_resume`은 `LEGACY`와 남은 미완료 목록만 돌려준다.
2. UI는 다음을 보여 주고 **예/아니오(기본 아니오)**로 확인을 받는다.
   - 연결 지문이 없어 같은 원본·대상인지 자동 확인할 수 없다는 사실
   - 기록된 날짜 범위와 남아 있는 checkpoint 수(완료 수) — 범위의 파티션이 다 있는지 확인 요청
   - 재개에 쓸 **현재 연결 라벨**
3. "예"면 `adopt_legacy_history()`가 현재 identity와 **남아 있는 checkpoint 집합**을 계획으로 한 번
   기록하고 `legacy_adopted_at`을 남긴다. 기록은 조건부 UPDATE(`plan_version IS NULL`)와 checkpoint
   집합 재확인을 같은 트랜잭션에서 하므로 두 번 채택되거나 확인 중 바뀐 집합으로 채택되지 않는다.
4. 채택 이후에는 일반 이력과 같이 엄격하게 검사한다(연결이 바뀌면 거부).
5. checkpoint가 하나도 없는 legacy 이력은 계획을 복원할 근거가 없으므로 채택을 거부하고 폐기를 안내한다.

## 6. 프로필 수명주기 (M-12)

- 미완료 상태 집합: `INCOMPLETE_STATUSES = ("running", "failed", "partial")` (`src/database/repository.py`).
  재개 조회와 삭제 차단이 같은 집합을 쓴다.
- **삭제**(`MainWindow.delete_connection`): 미완료 이력이 있으면 바로 지우지 않는다. 경고 후
  "미완료 작업 N건을 폐기하고 삭제할까요?"(기본 아니오)를 묻고, 예일 때만
  `abandon_incomplete_histories()` → 삭제 순서로 진행한다. 미완료 여부 조회가 실패하면 삭제하지 않는다.
- **편집**(`MainWindow.edit_connection`): 미완료 이력이 있는 프로필의 endpoint identity를 바꾸면
  저장 전에 "그 작업은 재개할 수 없게 됩니다"를 묻는다(기본 아니오). 비밀번호·SSL만 바꾸면 묻지 않는다.
- 폐기는 이력 상태를 `cancelled`로 바꾸고 `completed_at`을 기록한다. 이력·checkpoint 행과
  대상에 이미 옮긴 데이터는 지우지 않는다.

## 7. 로컬 SQLite 스키마 마이그레이션

`LocalDatabase._migrate_schema()`는 `PRAGMA table_info`로 없는 컬럼만 골라 한 트랜잭션으로
`ALTER TABLE ... ADD COLUMN`한다(멱등). 예전처럼 모든 ALTER 예외를 삼키지 않는다 — 추가에 실패한 채
올라가면 모든 이력 조회가 `no such column`으로 깨지기 때문이다. 구버전(1.0, 1.2.7) DB 파일 fixture로
56건 이력·112건 checkpoint가 보존되고 새 컬럼이 NULL로 추가되는지 테스트한다.

## 8. 테스트

| 파일 | 내용 |
|---|---|
| `tests/models/test_history_plan_integrity.py` | N번째(첫/중간/마지막) checkpoint INSERT를 SQLite 트리거로 실패 → history·checkpoint 0건, 계획 기준 pending, 누락 보충, 계획 변조·계획 밖 checkpoint 차단 |
| `tests/models/test_history_endpoint_identity.py` | 지문 규칙(비밀번호·SSL 무시, 각 identity 필드 반영, 비밀 미포함), identity·방향·mode·schema 변경 거부, 비밀번호 교체 허용, legacy 확인·채택 |
| `tests/database/test_local_db_schema_migration.py` | 구버전 DB 파일 fixture 업그레이드·멱등성 |
| `tests/ui/test_history_guards.py` | 마법사 원자 생성 실패 시 orphan 0·워커 미시작, 두 다이얼로그의 재개 거부·허용·폐기·legacy 확인, 프로필 삭제 차단·폐기 후 삭제·조회 실패 차단, 편집 경고 |
| `tests/models/test_e2e_tool_history_api.py` | `tools/e2e_verify_copy.py`가 새 API로 이력을 만들고 identity 게이트를 검사 |

## 9. 남은 한계

- 프로세스 강제 종료·SQLite 잠금 주입은 별도 테스트가 없다. 원자성은 단일 SQLite 트랜잭션에 기대며,
  N번째 INSERT 실패 테스트가 같은 rollback 경로를 검증한다.
- 삭제 차단은 UI(`MainWindow`)에서 한다. `ProfileManager.delete_profile()` 자체에는 가드가 없다
  (다른 호출 경로는 현재 없음).
- `tools/bench/compare_copy_modes.py`는 여전히 저수준 `create_history` + `create_checkpoint`를 쓴다
  (벤치 전용, 재개하지 않음).
