# Core 마이그레이션 엔진

- **문서 경로**: `src/core/CLAUDE.md`
- **레이어**: 코어 도메인 (마이그레이션 엔진)
- **역할**: 파티션 기반 데이터 이동을 수행하는 워커와 지원 컴포넌트의 동작을 설명합니다.

## 책임 범위
- 파티션 구조 분석으로 마이그레이션 대상과 순서를 결정합니다.
- PostgreSQL 연결을 생성하고 COPY/INSERT 전략을 선택적으로 실행합니다.
- 진행률, 성능 지표, 체크포인트를 통해 중단 복구와 모니터링을 보장합니다.
- 대상 스키마 준비 및 권한 검사를 자동화합니다.

## 주요 구성요소
- `partition_discovery.py`: 소스에서 파티션 메타데이터를 수집해 마이그레이션 목록을 구축합니다. 소스 전용입니다.
- `scan_workers.py`: UI가 띄우는 조회 워커들(`ConnectionCheckWorker`, `PartitionScanWorker`,
  `TargetCompletedScanWorker`, `RowCountVerifyWorker`). 공통 베이스 `ScanWorker`가 취소·예외·세대를 처리합니다.
- `copy_migration_worker.py`: PostgreSQL COPY 명령을 활용한 고성능 워커. 권한 점검, 성능 측정, 체크포인트를 포함합니다.
- `migration_worker.py`: INSERT 기반 레거시 워커. 호환성이 필요한 환경에서 사용합니다.
- `table_creator.py`: 파티션 구조를 분석해 대상에 동일한 테이블을 생성합니다.
- `performance_metrics.py`: 처리량, 소요 시간 등 실시간 마이그레이션 지표를 계산합니다.

## 행 수를 다루는 규칙
`reltuples`는 ANALYZE 전이면 0(PG14+는 -1)입니다. **추정치로 데이터 정합성을 결정하지 않습니다.**

- 탐색(`partition_discovery._estimate_row_count`)은 표시용 추정치만 냅니다. 항목에
  `row_count_estimated: True`를 붙여 UI가 정직하게 표기하게 합니다.
- 빈 파티션 건너뛰기는 `CopyMigrationWorker._resolve_total_rows()`가 실제 `COUNT(*)`로
  재확인한 뒤에만 합니다. 조회가 실패하면 "비었다"고 단정하지 않습니다.
- 사후 검증(`RowCountVerifyWorker`)은 정확한 값이 목적이므로 `COUNT(*)`를 씁니다.
- 진행량 기록은 워커의 성능 카운터가 아니라 **체크포인트 합계**를 씁니다. 카운터는 실행
  1회분만 세므로 재개하면 진행량이 뒤로 갑니다.
- **파티션 완료 판정은 `CopyMigrationWorker._verify_partition_row_count()`의 원본·대상 `COUNT(*)`
  일치로만 합니다**(Python COPY·Server COPY 공통). 다르면 예외 → 체크포인트 `failed`.
- Python COPY의 `CopyStreamBuffer`: 행 수·마지막 키는 **소비자(대상에 넘긴 데이터)** 기준,
  CSV 레코드 경계(`_CsvRecordTracker`, 따옴표 인식)로 셉니다. 커밋 전 `assert_fully_consumed()`로
  생산량 == 소비량을 확인합니다. 취소·오류에서 `read()`는 짧은 EOF가 아니라 예외를 던집니다.

## 취소 규약
조회 워커는 `isInterruptionRequested()`를 보고, `BaseMigrationWorker` 계열은 자체 `stop()`
플래그를 봅니다. 플래그는 쿼리 **사이**에서만 읽히므로, 오래 걸리는 쿼리는 `cancel_query()`가
커넥션에 `cancel()`을 걸어야 실제로 끊깁니다. 커넥션을 2개 이상 쓰는 워커는 **전부** 추적해야
합니다 — 하나만 걸면 나머지 구간에서 취소가 조용히 무시됩니다.

## SQL 경계 규칙 (감사 H-04 / M-01 / M-14)
헬퍼는 `src/database/postgres_utils.py`에 있다. psycopg2(COPY 워커)·psycopg3(탐색·legacy 워커)가
섞이므로 드라이버 전용 API 대신 이 헬퍼를 쓴다.

- relation은 `qualified_name(name)`(= `"public"."name"`) 또는 `sql.Identifier("public", name)`로
  schema를 한정한다. 이름 문자열을 `regclass`로 바꾸는 함수(`pg_table_size('name')`)는 쓰지 않고
  카탈로그 oid를 쓴다. 연결의 `search_path=public`은 2차 방어일 뿐이다.
- `information_schema.columns/tables`는 search_path와 무관하게 모든 스키마를 보여 주므로
  반드시 `table_schema = 'public'` 조건을 붙인다(search_path 방어가 통하지 않는다).
- 실패해도 되는 문장은 `isolated_statement()`/`run_optional_statement()`(SAVEPOINT)로 감싼다.
  예외를 잡기만 하면 트랜잭션이 중단돼 이후 문장이 전부 `25P02`로 거부된다.
- 무시 여부는 예외 클래스가 아니라 `sqlstate_of()`로 판단한다(허용 목록 밖은 즉시 실패).
- 커밋은 `commit_or_raise()` — 중단된 트랜잭션의 COMMIT은 오류 없이 ROLLBACK이 된다.
- `TableCreator.create_partition_table()`: 부모·파티션·인덱스·트리거/RULE·CLUSTER·
  `partition_table_info`를 한 트랜잭션으로 묶어 마지막에 한 번 커밋, 실패 시 rollback.
- 테스트: `tests/database/fake_pg.py`(중단 트랜잭션 규칙을 지키는 가짜 연결), 실DB는
  `tests/integration/test_sql_boundaries_realdb.py`(`DBMIG_REALDB_SQL_TESTS=1`, temp DB만 씀).

## 동작 시나리오
1. UI 또는 서비스가 요청을 보내면 `partition_discovery.py`가 대상 파티션을 반환합니다.
2. 선택된 워커가 `ConnectionProfile` 정보를 이용해 소스/대상 DB 연결을 생성합니다.
3. 파티션별로 데이터를 복사하면서 체크포인트와 로그를 갱신하고, 시그널로 진행률을 알립니다.
4. 예외 발생 시 워커는 에러 시그널과 로그를 남기고, UI는 이를 사용자에게 전달합니다.

## 확장 가이드
- 새로운 마이그레이션 전략을 추가하려면 QThread를 상속한 워커를 작성하고 UI와 동일한 시그널 프로토콜을 준수하세요.
- 성능 데이터가 필요한 경우 `PerformanceMetrics`를 주입하거나 확장하여 추가 지표를 계산할 수 있습니다.

## 파일 아카이브 manifest 규칙
상세·신뢰 경계는 `docs/base/archive-trust-boundary.md`.

- manifest 저장은 `ArchiveManifestStore.save()`/`commit_partition()`만 쓴다. 잠금 안에서 디스크를
  다시 읽어 항목별 `entry_version` CAS로 병합한다. 충돌은 `ManifestConflictError` — 삼키지 않는다.
- 파티션 파일을 최종 경로로 옮기는 일은 `commit_partition()` 안에서 manifest 기록과 함께 한다.
- 새 파티션 항목은 `checksum_sha256` 필수. import는 checksum·인증 없는 아카이브를
  `allow_legacy_unverified`(사용자 명시 확인) 없이는 거부하고, 허용하면 WARNING을 남긴다.
  passphrase를 줬는데 manifest가 인증 없음이면 다운그레이드 의심으로 무조건 거부한다.
- `entry_version`은 단조 증가, 항목 삭제 경로 없음. 병합 때 디스크가 base보다 뒤처지면(백업 폴백)
  오래된 사본으로 보고 되살린다 — 디스크 값을 따라 항목을 잃지 않는다.
- import는 대상 DB 연결 전에 `require_trusted()`와 전 파일 사전 검증을 하고, DDL은
  검증된 메모리 manifest(`ManifestTableCreator(manifest=...)`)로만 만든다. 디스크를 다시 읽지 않는다.
- passphrase는 워커 `configure_archive_security()`로만 받는다. 저장·로그 금지.
