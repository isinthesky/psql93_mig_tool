# PostgreSQL 버전별 호환성 모드 구현 계획 (지원 대상: 9.3, 16)

## 개요

현재 시스템은 실제 접속한 PostgreSQL 서버 버전에 관계없이 동일한 COPY 기반 처리와 세션 파라미터를 사용합니다. 지원/검증 대상은 **PostgreSQL 9.3**과 **PostgreSQL 16** 두 버전만으로 한정합니다. 다른 버전은 고려하지 않으며, 미확인 버전은 안전을 위해 9.3 프로파일로 취급합니다. 이 문서는 **버전별 최적화 쿼리 자동 적용**과 **9.3 호환 모드 토글** 구현 계획을 정리합니다.

---

## 1단계: 프로필에 버전 호환 모드 플래그 추가

### 1.1 데이터 모델/저장 경로

- **파일**: `src/models/profile.py`, `src/ui/dialogs/connection_mapper.py`, `src/utils/validators.py`
- **보관 방식**: 기존처럼 암호화된 `source_config`/`target_config` JSON 안에 아래 필드를 포함합니다. DB 컬럼 추가 없이 config에 저장하므로 `_migrate_schema` 수정이 필요 없습니다.
  - `compat_mode`: `"auto" | "9.3" | "16"` (소스/대상 각각)
- 기본값 `auto`는 `SELECT version()` 결과를 사용합니다. `"9.3"`/`"16"`은 강제로 해당 프로파일을 적용합니다.

### 1.2 UI 확장

- **파일**: `src/ui/dialogs/connection_dialog.py`
- 각 DB 탭에 콤보박스를 추가하고 ConnectionMapper를 통해 `compat_mode`를 읽고 씁니다.

```python
compat_combo = QComboBox()
compat_combo.addItems(["자동 감지", "PostgreSQL 9.3", "PostgreSQL 16"])
layout.addRow("호환 모드:", compat_combo)
```

### 1.3 검증/매핑

- Validator에서 허용 값(`auto`, `9.3`, `16`)만 통과하도록 추가합니다.
- Mapper는 UI ↔ config 양방향으로 `compat_mode`를 포함하도록 확장합니다.

---

## 2단계: 버전 감지 및 분기 인프라

### 2.1 버전 정보 데이터 클래스

- **파일**: `src/database/version_info.py` (신규)

```python
from dataclasses import dataclass
from enum import Enum

class PgVersionFamily(Enum):
    PG_9_3 = "9.3"
    PG_16 = "16"
    UNKNOWN = "unknown"

@dataclass
class PgVersionInfo:
    major: int
    minor: int
    full_version: str
    family: PgVersionFamily

    @property
    def is_legacy(self) -> bool:
        return self.family == PgVersionFamily.PG_9_3

    @property
    def supports_jsonb(self) -> bool:
        return self.family == PgVersionFamily.PG_16
```

### 2.2 버전 감지 유틸리티

- **파일**: `src/database/postgres_utils.py` 확장 (필수: `re` 임포트)

```python
def parse_version_string(version_str: str) -> PgVersionInfo:
    match = re.search(r"PostgreSQL (\\d+)\\.(\\d+)", version_str)
    if match:
        major, minor = int(match.group(1)), int(match.group(2))
        if major == 9 and minor == 3:
            family = PgVersionFamily.PG_9_3
        elif major == 16:
            family = PgVersionFamily.PG_16
        else:
            family = PgVersionFamily.UNKNOWN
        return PgVersionInfo(major, minor, version_str, family)
    return PgVersionInfo(0, 0, version_str, PgVersionFamily.UNKNOWN)

@staticmethod
def detect_version(connection) -> PgVersionInfo:
    with connection.cursor() as cursor:
        cursor.execute("SELECT version()")
        return parse_version_string(cursor.fetchone()[0])

@staticmethod
def resolve_effective_version(connection, compat_mode: str) -> PgVersionInfo:
    detected = PostgresOptimizer.detect_version(connection)
    if compat_mode == "9.3":
        return PgVersionInfo(9, 3, "forced:9.3", PgVersionFamily.PG_9_3)
    if compat_mode == "16":
        return PgVersionInfo(16, 0, "forced:16", PgVersionFamily.PG_16)
    return detected  # auto
```

---

## 3단계: 버전별 SQL 템플릿 및 세션 파라미터

### 3.1 세션 파라미터 매트릭스

- **파일**: `src/database/version_params.py` (신규)

```python
VERSION_PARAMS = {
    "9.3": {
        "work_mem": "128MB",
        "maintenance_work_mem": "512MB",
        "synchronous_commit": "off",
        "checkpoint_segments": "32",
    },
    "16": {
        "work_mem": "256MB",
        "maintenance_work_mem": "1GB",
        "synchronous_commit": "off",
        "max_wal_size": "4GB",
        "max_parallel_workers_per_gather": "2",
    },
}

def get_params_for_version(version_info: PgVersionInfo) -> dict:
    if version_info.family == PgVersionFamily.PG_9_3:
        return VERSION_PARAMS["9.3"]
    if version_info.family == PgVersionFamily.PG_16:
        return VERSION_PARAMS["16"]
    # UNKNOWN → 보수적으로 9.3 프로파일 사용
    return VERSION_PARAMS["9.3"]
```

### 3.2 SQL 템플릿 매트릭스

- **파일**: `src/database/version_sql.py` (신규)

```python
SQL_TEMPLATES = {
    "9.3": {
        "copy_to": """
            COPY (
                SELECT path_id, issued_date, changed_value,
                       COALESCE(connection_status::text, 'true') as connection_status
                FROM {table}
                {where_clause}
                ORDER BY path_id, issued_date
                LIMIT {limit}
            ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
        """,
        "estimate_size": """
            SELECT
                (SELECT reltuples::bigint FROM pg_class WHERE relname = %s) as row_count,
                pg_table_size(%s) as total_size
        """,
        "check_permission": """
            SELECT rolsuper FROM pg_roles WHERE rolname = current_user
        """,
    },
    "16": {
        "copy_to": """
            COPY (
                SELECT path_id, issued_date, changed_value,
                       COALESCE(connection_status::text, 'true') as connection_status
                FROM {table}
                {where_clause}
                ORDER BY path_id, issued_date
                LIMIT {limit}
            ) TO STDOUT WITH (FORMAT CSV, HEADER FALSE, NULL 'NULL')
        """,
        "estimate_size": """
            SELECT
                (SELECT reltuples::bigint FROM pg_class WHERE relname = %s) as row_count,
                pg_total_relation_size(%s) as total_size
        """,
        "check_permission": """
            SELECT rolsuper OR pg_has_role(current_user, 'pg_read_server_files', 'MEMBER')
            FROM pg_roles WHERE rolname = current_user
        """,
    },
}

def get_sql_for_version(version_info: PgVersionInfo, query_name: str) -> str:
    key = "9.3" if version_info.family == PgVersionFamily.PG_9_3 else "16"
    return SQL_TEMPLATES[key][query_name]
```

---

## 4단계: 워커/DAO 분기 로직 통합

### 4.1 CopyMigrationWorker

- **파일**: `src/core/copy_migration_worker.py`
- 소스/대상 연결 후 `resolve_effective_version`를 호출해 버전 정보를 보관하고 로그로 표시합니다.
- `compat_mode` 값은 `profile.source_config["compat_mode"]` / `profile.target_config["compat_mode"]`(없으면 `auto`)에서 읽습니다.
- `_apply_version_optimizations`에서 `get_params_for_version` 결과를 `apply_params`로 세션에 반영합니다.

### 4.2 PostgresOptimizer

- **파일**: `src/database/postgres_utils.py`
- `apply_params(connection, params)` 추가: 지원하지 않는 파라미터는 경고 후 롤백하고 계속 진행합니다.

---

## 5단계: 9.3 미지원 기능 차단 및 대체

| 기능 | 지원 버전 | 9.3 대체 방안 |
|------|-----------|---------------|
| JSONB | 16만 지원 | JSON 또는 TEXT 직렬화 |
| pg_read_server_files | 16만 사용 | 9.3은 슈퍼유저 확인만 수행 |
| 병렬 쿼리 | 16 사용 | 9.3은 단일 스레드 처리 |
| UPSERT (ON CONFLICT) | 16 사용 | 9.3은 DELETE + INSERT 조합 |
| TABLESAMPLE | 16 사용 | 9.3은 전체 스캔 |

### 검증 로직

- **파일**: `src/utils/validators.py`
- `validate_version_compatibility`에서 다운그레이드 경고, JSONB 호환 경고 등을 반환해 UI/로그에 표시합니다.

---

## 6단계: 테스트 계획

### 6.1 단위 테스트

- **파일**: `tests/unit/test_version_detection.py`
- 케이스: 9.3 파싱, 16 파싱, 파라미터 매핑(9.3/16), UNKNOWN → 9.3 폴백 확인.

### 6.2 통합 테스트 (Docker 기반)

- **파일**: `tests/integration/test_version_matrix.py`
- 픽스처 파라미터: `["9.3", "16"]`만 사용.
- 시나리오: 9.3→9.3, 9.3→16, 16→9.3, 16→16 마이그레이션 성공/경고 확인.

### 6.3 테스트 매트릭스

| 소스 버전 | 대상 버전 | 테스트 항목 |
|-----------|-----------|-------------|
| 9.3 | 9.3 | COPY 기본 동작, 체크포인트 |
| 9.3 | 16 | 호환 모드 전환, 파라미터 차이 |
| 16 | 9.3 | 다운그레이드 경고, JSONB 차단 |
| 16 | 16 | 최적화 파라미터 적용 |

---

## 구현 우선순위

| 순서 | 작업 | 복잡도 | 영향도 |
|------|------|--------|--------|
| 1 | 버전 감지 유틸리티 (`version_info.py`, 파싱 함수) | 낮음 | 높음 |
| 2 | 세션 파라미터/SQL 매트릭스 (9.3/16) | 낮음 | 중간 |
| 3 | 프로필 config 확장 + 매퍼/밸리데이터 | 중간 | 높음 |
| 4 | UI 콤보박스 추가 | 낮음 | 중간 |
| 5 | 워커 분기 로직 통합 | 중간 | 높음 |
| 6 | 단위 테스트 | 낮음 | 높음 |
| 7 | Docker 통합 테스트(9.3,16) | 높음 | 중간 |

---

## 파일 변경 요약

### 신규 파일
- `src/database/version_info.py` - 버전 정보 데이터 클래스
- `src/database/version_params.py` - 9.3/16 세션 파라미터
- `src/database/version_sql.py` - 9.3/16 SQL 템플릿
- `tests/unit/test_version_detection.py` - 버전 감지/매핑 테스트
- `tests/integration/test_version_matrix.py` - 9.3/16 통합 테스트

### 수정 파일
- `src/models/profile.py` - compat_mode 필드 포함
- `src/database/postgres_utils.py` - 버전 감지, 파라미터 적용 함수 추가
- `src/ui/dialogs/connection_dialog.py` - 호환 모드 콤보박스 및 매퍼 연동
- `src/core/copy_migration_worker.py` - 버전별 분기 로직 통합
- `src/utils/validators.py` - 버전 호환성 검증 추가

---

## 참고 자료

- [PostgreSQL 9.3 Documentation](https://www.postgresql.org/docs/9.3/)
- [PostgreSQL 16 Documentation](https://www.postgresql.org/docs/16/)
