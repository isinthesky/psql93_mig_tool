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

## 취소 규약
조회 워커는 `isInterruptionRequested()`를 보고, `BaseMigrationWorker` 계열은 자체 `stop()`
플래그를 봅니다. 플래그는 쿼리 **사이**에서만 읽히므로, 오래 걸리는 쿼리는 `cancel_query()`가
커넥션에 `cancel()`을 걸어야 실제로 끊깁니다. 커넥션을 2개 이상 쓰는 워커는 **전부** 추적해야
합니다 — 하나만 걸면 나머지 구간에서 취소가 조용히 무시됩니다.

## 동작 시나리오
1. UI 또는 서비스가 요청을 보내면 `partition_discovery.py`가 대상 파티션을 반환합니다.
2. 선택된 워커가 `ConnectionProfile` 정보를 이용해 소스/대상 DB 연결을 생성합니다.
3. 파티션별로 데이터를 복사하면서 체크포인트와 로그를 갱신하고, 시그널로 진행률을 알립니다.
4. 예외 발생 시 워커는 에러 시그널과 로그를 남기고, UI는 이를 사용자에게 전달합니다.

## 확장 가이드
- 새로운 마이그레이션 전략을 추가하려면 QThread를 상속한 워커를 작성하고 UI와 동일한 시그널 프로토콜을 준수하세요.
- 성능 데이터가 필요한 경우 `PerformanceMetrics`를 주입하거나 확장하여 추가 지표를 계산할 수 있습니다.
