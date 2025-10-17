# PostgreSQL 마이그레이션 도구 리팩토링 분석 및 작업 계획

**문서 버전**: 1.0
**작성일**: 2025-10-17
**대상 프로젝트**: DB Migration Tool (PostgreSQL 파티션 마이그레이션)

---

## 목차
1. [개요](#개요)
2. [현재 코드 분석](#현재-코드-분석)
3. [리팩토링 항목](#리팩토링-항목)
4. [작업 계획](#작업-계획)
5. [예상 효과](#예상-효과)
6. [리스크 및 주의사항](#리스크-및-주의사항)

---

## 개요

### 목적
- 코드 중복 제거를 통한 유지보수성 향상
- 성능 최적화 (불필요한 DB 조회 및 메타데이터 조회 제거)
- 메모리 사용량 최적화 (대용량 데이터 처리 개선)
- 일관된 트랜잭션 처리 패턴 확립

### 범위
- `src/core/` - 마이그레이션 워커 클래스들
- `src/models/` - 매니저 클래스들의 트랜잭션 패턴
- `src/database/` - 데이터베이스 세션 관리

---

## 현재 코드 분석

### 1. 워커 클래스 중복 (`MigrationWorker` vs `CopyMigrationWorker`)

#### 공통 코드 패턴
**위치**: `src/core/migration_worker.py:31-48`, `src/core/copy_migration_worker.py:35-56`

```python
# 양쪽 모두 동일한 필드와 시그널 정의
progress = Signal(dict)
log = Signal(str, str)
error = Signal(str)
finished = Signal()

self.is_running = False
self.is_paused = False
self.current_partition_index = 0
self.history_manager = HistoryManager()
self.checkpoint_manager = CheckpointManager()
```

#### 공통 메서드
- `pause()` - `src/core/migration_worker.py:368`, `src/core/copy_migration_worker.py:366`
- `resume()` - `src/core/migration_worker.py:372`, `src/core/copy_migration_worker.py:371`
- `stop()` - `src/core/migration_worker.py:376`, `src/core/copy_migration_worker.py:376`
- 세션 ID 초기화 로직 - 양쪽 `run()` 메서드 내

**문제점**: 동일한 코드가 두 클래스에 중복되어 있어 수정 시 양쪽 모두 변경해야 함

---

### 2. 트랜잭션 패턴 중복 (Manager 클래스들)

#### 반복되는 패턴
**위치**:
- `src/models/profile.py:78-99` (create_profile)
- `src/models/profile.py:124-145` (update_profile)
- `src/models/history.py:76-102` (create_history)
- `src/models/history.py:128-151` (update_history_status)

```python
session = self.db.get_session()
try:
    # 비즈니스 로직
    session.commit()
    return result
except Exception as e:
    session.rollback()
    raise e
finally:
    session.close()
```

**문제점**:
- 12개 이상의 메서드에서 동일한 패턴 반복
- 실수로 rollback/close 누락 가능성
- 일관되지 않은 에러 처리

---

### 3. 메타데이터 반복 조회 (컬럼 정보)

#### 현재 구현
**위치**: `src/core/migration_worker.py:295-317`

```python
def _copy_batch(self, source_conn, target_conn, partition_name, offset, limit):
    # 매 배치마다 컬럼 정보를 information_schema에서 다시 조회
    target_cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (partition_name,))
    columns = [row[0] for row in target_cur.fetchall()]
```

**문제점**:
- 파티션당 수백~수천 번의 배치 실행
- 매번 동일한 컬럼 정보를 네트워크를 통해 조회
- 불필요한 파싱 및 메모리 할당

**성능 영향**:
- 배치 크기 100,000일 때, 4,000,000 rows 파티션 = 40회 조회
- 네트워크 왕복 시간 누적

---

### 4. 체크포인트 선형 검색

#### 현재 구현
**위치**:
- `src/core/migration_worker.py:75-77`
- `src/core/copy_migration_worker.py:152-155`

```python
# 매 파티션마다 전체 체크포인트 조회 후 선형 검색
checkpoints = self.checkpoint_manager.get_checkpoints(self.history_id)
checkpoint = next((cp for cp in checkpoints if cp.partition_name == partition), None)
```

**문제점**:
- 파티션 개수만큼 `get_checkpoints()` DB 조회 실행
- O(n) 선형 검색 (파티션 개수 × 체크포인트 개수)
- 100개 파티션 × 100개 체크포인트 = 10,000회 비교

---

### 5. 테이블 준비 로직 중복

#### 중복 위치
- `src/core/migration_worker.py:225-285` (_prepare_target_table)
- `src/core/copy_migration_worker.py:305-334` (_prepare_target_table)

**공통 로직**:
1. 테이블 존재 확인 SQL (동일)
2. 테이블 없으면 `TableCreator` 호출 (동일)
3. 테이블 있으면 TRUNCATE 실행 (거의 동일)

**차이점**:
- `MigrationWorker`: 사용자에게 확인 요청 (`truncate_requested.emit`)
- `CopyMigrationWorker`: 자동 TRUNCATE

**문제점**: 핵심 로직은 동일하나 사용자 상호작용 방식만 다름

---

### 6. COPY 워커 메모리 이슈

#### 현재 구현
**위치**: `src/core/copy_migration_worker.py:197-256`

```python
# 전체 파티션 데이터를 StringIO 메모리 버퍼에 로드
buffer = StringIO()
source_cursor.copy_expert(copy_to_query, buffer)
buffer_size = buffer.tell()
buffer.seek(0)
target_cursor.copy_expert(copy_from_query, buffer)
```

**문제점**:
- 4,000,000 rows × 평균 100 bytes = ~400MB 메모리 사용
- 대용량 파티션에서 메모리 부족 가능성
- GC 압력 증가

**개선 가능성**: 스트리밍 파이프 방식으로 상수 메모리 사용

---

## 리팩토링 항목

### R1. BaseMigrationWorker 추상 클래스 도입 ⭐⭐⭐

#### 목표
공통 상태, 시그널, 제어 메서드를 추상 기반 클래스로 추출

#### 구현 계획

**1단계: 추상 클래스 생성**
```python
# src/core/base_migration_worker.py (신규 파일)
from abc import ABC, abstractmethod
from PySide6.QtCore import QThread, Signal

class BaseMigrationWorker(QThread, ABC):
    """마이그레이션 워커의 추상 기반 클래스"""

    # 공통 시그널
    progress = Signal(dict)
    log = Signal(str, str)
    error = Signal(str)
    finished = Signal()

    def __init__(self, profile, partitions, history_id, resume=False):
        super().__init__()
        self.profile = profile
        self.partitions = partitions
        self.history_id = history_id
        self.resume = resume

        # 공통 상태
        self.is_running = False
        self.is_paused = False
        self.current_partition_index = 0
        self.total_rows_processed = 0
        self.start_time = None

        # 공통 매니저
        self.history_manager = HistoryManager()
        self.checkpoint_manager = CheckpointManager()

    def run(self):
        """템플릿 메서드"""
        self.is_running = True
        self.start_time = time.time()

        # 세션 ID 초기화
        session_id = enhanced_logger.generate_session_id()
        log_emitter.logger.set_session_id(session_id)

        try:
            self._execute_migration()
            if self.is_running:
                self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
            log_emitter.emit_log("ERROR", f"마이그레이션 오류: {str(e)}")

    @abstractmethod
    def _execute_migration(self):
        """하위 클래스에서 구현할 실제 마이그레이션 로직"""
        pass

    def pause(self):
        """일시정지"""
        self.is_paused = True
        self.log.emit("마이그레이션 일시정지", "INFO")

    def resume(self):
        """재개"""
        self.is_paused = False
        self.log.emit("마이그레이션 재개", "INFO")

    def stop(self):
        """중지"""
        self.is_running = False
        self.is_paused = False
        self.log.emit("마이그레이션 중지 요청", "WARNING")

    def _check_pause(self):
        """일시정지 상태 확인"""
        while self.is_paused and self.is_running:
            time.sleep(0.1)
```

**2단계: 기존 워커 리팩토링**
```python
# src/core/migration_worker.py
from src.core.base_migration_worker import BaseMigrationWorker

class MigrationWorker(BaseMigrationWorker):
    """INSERT 기반 레거시 마이그레이션 워커"""

    # 추가 시그널 (이 워커에만 필요한 것)
    truncate_requested = Signal(str, int)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # INSERT 전용 필드
        self.batch_size = 100000
        self.truncate_permission = None

    def _execute_migration(self):
        """INSERT 기반 마이그레이션 실행"""
        # 기존 run() 메서드의 핵심 로직만 이동
        ...
```

```python
# src/core/copy_migration_worker.py
from src.core.base_migration_worker import BaseMigrationWorker

class CopyMigrationWorker(BaseMigrationWorker):
    """COPY 명령 기반 고성능 마이그레이션 워커"""

    # 추가 시그널
    performance = Signal(dict)
    connection_checking = Signal()
    source_connection_status = Signal(bool, str)
    target_connection_status = Signal(bool, str)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # COPY 전용 필드
        self.performance_metrics = PerformanceMetrics()

    def _execute_migration(self):
        """COPY 기반 마이그레이션 실행"""
        # 기존 run() 메서드의 핵심 로직만 이동
        ...
```

#### 예상 효과
- 코드 중복 ~150 라인 제거
- 워커 간 일관성 보장
- 새로운 워커 추가 시 기반 제공

#### 영향 범위
- `src/core/migration_worker.py` (수정)
- `src/core/copy_migration_worker.py` (수정)
- `src/core/base_migration_worker.py` (신규)

---

### R2. session_scope() 컨텍스트 매니저 ⭐⭐⭐

#### 목표
트랜잭션 패턴을 컨텍스트 매니저로 통합

#### 구현 계획

**1단계: LocalDatabase에 컨텍스트 매니저 추가**
```python
# src/database/local_db.py
from contextlib import contextmanager

class LocalDatabase:
    # ... 기존 코드 ...

    @contextmanager
    def session_scope(self):
        """트랜잭션 컨텍스트 매니저

        Usage:
            with self.db.session_scope() as session:
                session.add(obj)
                # 정상 종료 시 자동 commit
                # 예외 발생 시 자동 rollback
        """
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            raise
        finally:
            session.close()
```

**2단계: Manager 클래스들 리팩토링**

**Before:**
```python
# src/models/profile.py:78-99
def create_profile(self, profile_data):
    session = self.db.get_session()
    try:
        db_profile = Profile(...)
        session.add(db_profile)
        session.commit()
        return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
```

**After:**
```python
def create_profile(self, profile_data):
    with self.db.session_scope() as session:
        db_profile = Profile(...)
        session.add(db_profile)
        # 자동 commit/rollback/close
        return ConnectionProfile.from_db_model(db_profile, self._cipher_suite)
```

**3단계: 적용 대상 메서드**

**ProfileManager** (`src/models/profile.py`):
- `create_profile()` :78-99
- `get_profile()` :101-110
- `get_all_profiles()` :112-122
- `update_profile()` :124-145
- `delete_profile()` :147-161

**HistoryManager** (`src/models/history.py`):
- `create_history()` :76-102
- `get_history()` :104-113
- `get_all_history()` :115-126
- `update_history_status()` :128-151
- `get_incomplete_history()` :153-167

**CheckpointManager** (`src/models/history.py`):
- `create_checkpoint()` :176-195
- `get_checkpoints()` :197-209
- `update_checkpoint_status()` :211-234
- `get_pending_checkpoints()` :236-249

#### 예상 효과
- 중복 코드 ~300 라인 제거
- 트랜잭션 누락 위험 제거
- 일관된 에러 처리

#### 주의사항
- `get_*` 조회 메서드는 commit이 필요 없으나, 컨텍스트 매니저가 빈 commit을 실행함 (성능 영향 미미)
- 필요시 read-only 버전 컨텍스트 매니저 추가 가능

---

### R3. 컬럼 정보 캐싱 ⭐⭐

#### 목표
파티션별 컬럼 메타데이터를 한 번만 조회하여 재사용

#### 구현 계획

**Before:**
```python
# src/core/migration_worker.py:287-335
def _copy_batch(self, source_conn, target_conn, partition_name, offset, limit):
    # ... 데이터 조회 ...

    # 매번 컬럼 조회
    target_cur.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = %s
        ORDER BY ordinal_position
    """, (partition_name,))
    columns = [row[0] for row in target_cur.fetchall()]

    # INSERT 문 생성
    insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(...)
```

**After:**
```python
# src/core/migration_worker.py
class MigrationWorker(BaseMigrationWorker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._partition_columns_cache = {}  # 캐시 추가

    def _get_partition_columns(self, conn, partition_name):
        """파티션 컬럼 정보 조회 (캐싱)"""
        if partition_name not in self._partition_columns_cache:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_name = %s
                    ORDER BY ordinal_position
                """, (partition_name,))
                self._partition_columns_cache[partition_name] = [
                    row[0] for row in cur.fetchall()
                ]
        return self._partition_columns_cache[partition_name]

    def _migrate_partition(self, source_conn, target_conn, partition_name, checkpoint):
        # ... 기존 로직 ...

        # 파티션 시작 시 컬럼 정보 캐싱
        columns = self._get_partition_columns(target_conn, partition_name)

        # 배치 루프
        while offset < total_rows:
            rows_copied = self._copy_batch(
                source_conn, target_conn, partition_name,
                offset, current_batch_size, columns  # 캐시된 컬럼 전달
            )

    def _copy_batch(self, source_conn, target_conn, partition_name,
                   offset, limit, columns):  # columns 파라미터 추가
        # 컬럼 조회 제거, 전달받은 columns 사용
        insert_sql = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(partition_name),
            sql.SQL(', ').join(map(sql.Identifier, columns)),
            sql.SQL(', ').join(sql.Placeholder() for _ in columns)
        )
```

#### 예상 효과
- 파티션당 수십~수백 회의 불필요한 메타데이터 조회 제거
- 네트워크 왕복 시간 감소
- 100개 파티션 × 평균 40 배치 = 4,000회 조회 → 100회 조회

#### 대안: TableCreator 연동
TableCreator가 테이블 생성 시 스키마 정보를 이미 가지고 있으므로, 이를 반환하여 재사용할 수도 있음

---

### R4. 체크포인트 딕셔너리 캐싱 ⭐⭐

#### 목표
체크포인트를 시작 시 한 번만 조회하여 딕셔너리로 변환

#### 구현 계획

**Before:**
```python
# src/core/migration_worker.py:68-77
for i, partition in enumerate(self.partitions):
    # 매번 DB 조회 + 선형 검색
    checkpoints = self.checkpoint_manager.get_checkpoints(self.history_id)
    checkpoint = next((cp for cp in checkpoints if cp.partition_name == partition), None)
```

**After:**
```python
class MigrationWorker(BaseMigrationWorker):
    def _execute_migration(self):
        # 시작 시 한 번만 조회하여 딕셔너리로 변환
        checkpoints_list = self.checkpoint_manager.get_checkpoints(self.history_id)
        checkpoints_dict = {
            cp.partition_name: cp
            for cp in checkpoints_list
        }

        for i, partition in enumerate(self.partitions):
            # O(1) 조회
            checkpoint = checkpoints_dict.get(partition)

            if checkpoint and checkpoint.status == 'completed':
                continue

            self._migrate_partition(source_conn, target_conn, partition, checkpoint)
```

#### 예상 효과
- DB 조회: 100회 → 1회 (100배 감소)
- 검색 복잡도: O(n) → O(1)
- 100개 파티션 환경에서 ~99회 불필요한 DB 조회 제거

#### 적용 대상
- `src/core/migration_worker.py:68-85`
- `src/core/copy_migration_worker.py:86-103`

---

### R5. ensure_partition_ready() 헬퍼 메서드 ⭐⭐

#### 목표
테이블 준비 로직을 TableCreator로 통합하고 재사용

#### 구현 계획

**1단계: TableCreator에 헬퍼 추가**
```python
# src/core/table_creator.py
class TableCreator:
    # ... 기존 메서드 ...

    def ensure_partition_ready(self, partition_name: str,
                              truncate_mode: str = 'auto',
                              confirm_callback=None) -> tuple[bool, int]:
        """파티션 테이블 준비 (생성 또는 TRUNCATE)

        Args:
            partition_name: 파티션 테이블 이름
            truncate_mode: 'auto' (자동 TRUNCATE), 'ask' (사용자 확인)
            confirm_callback: 사용자 확인 콜백 함수 (truncate_mode='ask'일 때)

        Returns:
            (table_created, existing_row_count)
        """
        with self.target_conn.cursor() as cursor:
            # 테이블 존재 확인
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                )
            """, (partition_name,))

            table_exists = cursor.fetchone()[0]

            if not table_exists:
                # 테이블 생성
                self.create_partition_table(partition_name)
                return (True, 0)

            # 기존 데이터 확인
            cursor.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(
                    sql.Identifier(partition_name)
                )
            )
            row_count = cursor.fetchone()[0]

            if row_count > 0:
                # TRUNCATE 모드에 따라 처리
                if truncate_mode == 'auto':
                    should_truncate = True
                elif truncate_mode == 'ask':
                    if confirm_callback:
                        should_truncate = confirm_callback(partition_name, row_count)
                    else:
                        raise ValueError("confirm_callback required for 'ask' mode")
                else:
                    raise ValueError(f"Invalid truncate_mode: {truncate_mode}")

                if should_truncate:
                    cursor.execute(
                        sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(
                            sql.Identifier(partition_name)
                        )
                    )
                    self.target_conn.commit()
                else:
                    raise Exception(f"기존 데이터 처리가 취소되었습니다: {partition_name}")

            return (False, row_count)
```

**2단계: 워커에서 사용**

**CopyMigrationWorker (자동 모드):**
```python
# src/core/copy_migration_worker.py
def _prepare_target_table(self, partition_name: str):
    """대상 테이블 준비"""
    creator = TableCreator(self.source_conn, self.target_conn)
    created, row_count = creator.ensure_partition_ready(
        partition_name,
        truncate_mode='auto'
    )

    if created:
        self.log.emit(f"{partition_name} 테이블 생성 완료", "SUCCESS")
    elif row_count > 0:
        self.log.emit(f"{partition_name} 기존 데이터 삭제 완료", "INFO")
```

**MigrationWorker (확인 모드):**
```python
# src/core/migration_worker.py
def _prepare_target_table(self, partition_name: str):
    """대상 테이블 준비"""

    def confirm_truncate(partition_name, row_count):
        """사용자에게 TRUNCATE 확인"""
        self.log.emit(
            f"{partition_name} 테이블에 {row_count:,}개의 기존 데이터가 있습니다",
            "WARNING"
        )
        self.truncate_requested.emit(partition_name, row_count)

        # 사용자 응답 대기
        self.truncate_permission = None
        while self.truncate_permission is None:
            if self.is_interrupted:
                return False
            time.sleep(0.1)

        return self.truncate_permission

    creator = TableCreator(self.source_conn, self.target_conn)
    created, row_count = creator.ensure_partition_ready(
        partition_name,
        truncate_mode='ask',
        confirm_callback=confirm_truncate
    )

    # 권한 초기화
    self.truncate_permission = None
```

#### 예상 효과
- 중복 코드 ~120 라인 제거
- 테이블 준비 로직 중앙화
- 테스트 용이성 향상

---

### R6. COPY 워커 스트리밍 최적화 ⭐

#### 목표
메모리 버퍼 사용을 최소화하여 대용량 파티션 처리 개선

#### 구현 계획

**옵션 A: SpooledTemporaryFile 사용 (권장)**
```python
# src/core/copy_migration_worker.py
from tempfile import SpooledTemporaryFile

def _migrate_partition_with_copy(self, partition_name: str, checkpoint: Any):
    # ... 기존 로직 ...

    # StringIO 대신 SpooledTemporaryFile 사용
    # max_size=10MB, 이후 디스크로 스풀
    with SpooledTemporaryFile(max_size=10*1024*1024, mode='w+') as buffer:
        # COPY TO
        with self.source_conn.cursor() as source_cursor:
            source_cursor.copy_expert(copy_to_query, buffer)

        # 버퍼 크기 확인
        buffer_size = buffer.tell()
        buffer.seek(0)

        # COPY FROM
        with self.target_conn.cursor() as target_cursor:
            target_cursor.copy_expert(copy_from_query, buffer)

        # 자동 정리 (with 블록 종료 시)
```

**장점**:
- 10MB 이하: 메모리에서 처리 (빠름)
- 10MB 초과: 자동으로 임시 파일 사용 (메모리 절약)
- 컨텍스트 매니저로 자동 정리

**옵션 B: 파이프 기반 스트리밍 (고급)**
```python
import os
import threading

def _migrate_partition_with_copy_streaming(self, partition_name: str, checkpoint: Any):
    """파이프 기반 스트리밍 COPY"""

    # OS 파이프 생성
    read_fd, write_fd = os.pipe()

    def copy_to_pipe():
        """소스에서 파이프로 COPY"""
        try:
            with os.fdopen(write_fd, 'w') as write_pipe:
                with self.source_conn.cursor() as cur:
                    cur.copy_expert(copy_to_query, write_pipe)
        except Exception as e:
            self.log.emit(f"COPY TO 오류: {e}", "ERROR")

    # 백그라운드 스레드에서 COPY TO 실행
    copy_thread = threading.Thread(target=copy_to_pipe)
    copy_thread.start()

    try:
        # 메인 스레드에서 파이프로부터 COPY FROM
        with os.fdopen(read_fd, 'r') as read_pipe:
            with self.target_conn.cursor() as cur:
                cur.copy_expert(copy_from_query, read_pipe)
    finally:
        copy_thread.join()
```

**장점**:
- 진정한 스트리밍 (상수 메모리 사용)
- 대용량 파티션에서 최적 성능

**단점**:
- 구현 복잡도 증가
- 에러 처리 복잡

#### 권장사항
1. **1단계**: SpooledTemporaryFile 적용 (간단하고 안전)
2. **2단계**: 성능 모니터링 후 필요 시 파이프 방식 검토

#### 예상 효과
- 메모리 사용량: 파티션 크기에 비례 → 상수 (10MB 또는 파이프 버퍼)
- 대용량 파티션 (1GB+) 처리 가능
- GC 압력 감소

---

## 작업 계획

### 단계별 일정

#### Phase 1: 기초 리팩토링 (2-3일)
**우선순위**: 높음, 리스크: 낮음

1. **R2: session_scope() 도입** (1일)
   - LocalDatabase 수정
   - Manager 클래스 12개 메서드 리팩토링
   - 단위 테스트 실행 및 검증

2. **R3: 컬럼 정보 캐싱** (0.5일)
   - MigrationWorker 수정
   - 기존 INSERT 방식 테스트

3. **R4: 체크포인트 캐싱** (0.5일)
   - MigrationWorker, CopyMigrationWorker 수정
   - 재개 시나리오 테스트

#### Phase 2: 구조 개선 (3-4일)
**우선순위**: 높음, 리스크: 중간

4. **R1: BaseMigrationWorker 도입** (2일)
   - 추상 클래스 설계 및 구현
   - MigrationWorker 리팩토링
   - CopyMigrationWorker 리팩토링
   - 통합 테스트

5. **R5: ensure_partition_ready() 헬퍼** (1일)
   - TableCreator 수정
   - 워커 클래스 적용
   - TRUNCATE 시나리오 테스트

#### Phase 3: 성능 최적화 (1-2일)
**우선순위**: 중간, 리스크: 낮음

6. **R6: COPY 스트리밍 최적화** (1일)
   - SpooledTemporaryFile 적용
   - 대용량 파티션 테스트 (1GB+)
   - 메모리 프로파일링

### 작업 순서 근거

1. **R2 (session_scope) 먼저**
   - 독립적 변경
   - 리스크 최소
   - 다른 작업과 병렬 가능

2. **R3, R4 (캐싱) 다음**
   - 빠른 성과
   - R1 작업 전에 완료하여 BaseMigrationWorker에 반영

3. **R1 (BaseMigrationWorker)**
   - 구조적 변경으로 가장 신중하게
   - 앞선 개선사항들을 통합

4. **R5, R6 (최적화)**
   - 구조가 안정화된 후 진행
   - 선택적 적용 가능

---

## 예상 효과

### 정량적 효과

#### 코드 품질
- **중복 코드 제거**: ~570 라인
  - BaseMigrationWorker: ~150 라인
  - session_scope: ~300 라인
  - ensure_partition_ready: ~120 라인

#### 성능 개선
- **DB 조회 감소**: 파티션 100개 기준
  - 체크포인트: 100회 → 1회 (99% 감소)

- **메타데이터 조회 감소**: 파티션당 평균 40 배치 기준
  - 컬럼 정보: 4,000회 → 100회 (97.5% 감소)

- **메모리 사용량**: 대용량 파티션 (400MB 데이터)
  - StringIO: ~400MB 메모리
  - SpooledTemporaryFile: ~10MB 메모리 + 디스크 스풀

#### 유지보수성
- 워커 간 일관성 보장
- 트랜잭션 패턴 표준화
- 테스트 용이성 향상

### 정성적 효과

- **코드 가독성**: 중복 제거로 핵심 로직 명확화
- **확장성**: 새 워커 추가 시 기반 클래스 활용
- **신뢰성**: 트랜잭션 누락 위험 제거
- **안정성**: 메모리 이슈 해결로 대용량 처리 가능

---

## 리스크 및 주의사항

### 리스크 분석

#### R1 (BaseMigrationWorker) - 중간 리스크
**위험**: 추상화 설계 실수 시 오히려 복잡도 증가

**완화 방안**:
- 단계적 리팩토링 (한 워커씩)
- 기존 테스트 케이스 유지
- 코드 리뷰 필수

#### R2 (session_scope) - 낮은 리스크
**위험**: 트랜잭션 격리 수준 변경 가능성

**완화 방안**:
- SQLite 기본 동작 확인
- 각 Manager 메서드 개별 테스트

#### R3, R4 (캐싱) - 낮은 리스크
**위험**: 캐시 무효화 이슈

**완화 방안**:
- 워커 수명 내에서만 캐싱 (스레드 로컬)
- 재개 시나리오 명확화

#### R5 (ensure_partition_ready) - 낮은 리스크
**위험**: 콜백 패턴 복잡도

**완화 방안**:
- 명확한 인터페이스 정의
- 두 가지 모드('auto', 'ask') 명시

#### R6 (스트리밍) - 중간 리스크
**위험**: 파이프 방식의 에러 처리 복잡

**완화 방안**:
- 1단계에서는 SpooledTemporaryFile만 적용
- 파이프 방식은 선택적 개선

### 주의사항

1. **하위 호환성**
   - 기존 API 시그니처 최대한 유지
   - UI 레이어 영향 최소화

2. **테스트 커버리지**
   - 각 리팩토링 후 회귀 테스트 필수
   - 재개 시나리오 특히 중요

3. **문서화**
   - 추상 클래스 및 컨텍스트 매니저 사용법 문서화
   - 새 워커 추가 가이드 작성

4. **점진적 적용**
   - 한 번에 하나의 리팩토링만 진행
   - 커밋 단위 명확화

---

## 부록

### A. 체크리스트

#### 리팩토링 전
- [ ] 현재 코드의 모든 테스트 통과 확인
- [ ] 기존 기능 문서화 (변경 전 스냅샷)
- [ ] 백업 브랜치 생성

#### 각 리팩토링 작업 시
- [ ] 단위 테스트 작성/수정
- [ ] 기존 테스트 통과 확인
- [ ] 코드 리뷰 수행
- [ ] 변경 사항 문서화

#### 리팩토링 후
- [ ] 통합 테스트 실행
- [ ] 성능 벤치마크 비교
- [ ] 메모리 프로파일링
- [ ] 사용자 시나리오 테스트

### B. 참조 파일 목록

#### 수정 대상
- `src/core/migration_worker.py`
- `src/core/copy_migration_worker.py`
- `src/core/table_creator.py`
- `src/database/local_db.py`
- `src/models/profile.py`
- `src/models/history.py`

#### 신규 생성
- `src/core/base_migration_worker.py`

#### 영향 받는 파일 (테스트 필요)
- `src/ui/dialogs/migration_dialog.py` (워커 사용)
- `src/main.py` (워커 초기화)

### C. 추가 개선 아이디어

다음 단계 개선 항목 (현재 범위 밖):

1. **비동기 체크포인트 업데이트**
   - 배치마다 DB 업데이트 대신 버퍼링 후 주기적 플러시

2. **병렬 파티션 처리**
   - 독립적인 파티션들을 여러 워커로 병렬 처리

3. **진행률 예측 개선**
   - 과거 이력 기반 ETA 계산

4. **재시도 로직**
   - 일시적 네트워크 오류 시 자동 재시도

5. **압축 전송**
   - 네트워크 대역폭이 병목인 경우 COPY 데이터 압축

---

**문서 종료**
