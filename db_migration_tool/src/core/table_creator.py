"""
대상 테이블 생성 모듈

파티션 테이블의 스키마를 소스에서 복제하고,
테이블 타입에 따라 TRIGGER 또는 RULE을 생성합니다.
"""

import logging
import re
from datetime import datetime
from typing import Any

from src.database.postgres_utils import (
    commit_or_raise,
    qualified_name,
    quote_ident,
    rollback_quietly,
    run_optional_statement,
)

from .table_types import (
    TABLE_TYPE_CONFIG,
    TableType,
    get_partition_primary_key_columns,
    get_table_type,
    infer_partition_range,
    should_cluster_partition_by_pkey,
)

logger = logging.getLogger(__name__)

# ── 선택 DDL에서 무시해도 되는 SQLSTATE (감사 M-14) ──────────────────────
# 목록 밖의 오류(취소 57014, 연결 오류, 디스크 부족 등)는 필수 DDL과 똑같이 즉시 실패한다.
# 인덱스: 9.3은 CREATE INDEX IF NOT EXISTS가 없다. 같은 이름이 이미 있으면(42P07,
# 드물게 42710) 건너뛴다. 권한 부족(42501)도 데이터 정합성과 무관한 성능 인덱스라 경고만 한다.
IGNORABLE_INDEX_SQLSTATES = frozenset({"42P07", "42710", "42501"})
# CLUSTER: 물리 정렬만 바꾸는 최적화다. PK 인덱스 없음(42704)·권한 부족(42501)·
# 대상 종류가 맞지 않음(42809)·전제 상태 불충족(55000)·미지원(0A000)은 건너뛴다.
IGNORABLE_CLUSTER_SQLSTATES = frozenset({"42704", "42501", "42809", "55000", "0A000"})

# 안전한 식별자 패턴: 영문자, 숫자, 언더스코어만 허용
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_SAFE_DATA_TYPE_RE = re.compile(r"^[a-zA-Z_ ]+$")
_SAFE_DEFAULT_RE = re.compile(
    r"^(?:"
    r"NULL|"
    r"'[^']*'(?:\:\:[a-zA-Z_ ]+)?|"
    r"[0-9.eE+-]+|"
    r"(?:now|current_timestamp|current_date)\(\)|"
    r"nextval\('[a-zA-Z0-9_]+'::regclass\)|"
    r"false|true"
    r")$",
    re.IGNORECASE,
)


def _validate_identifier(name: str) -> str:
    """식별자가 안전한 패턴인지 검증하고 반환. 아니면 ValueError 발생."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"안전하지 않은 식별자: {name!r}")
    return name


def _validate_data_type_declaration(data_type: str) -> str:
    """information_schema 기반 타입 선언을 안전한 문자열로 검증한다."""
    normalized = str(data_type or "").strip()
    if not normalized or not _SAFE_DATA_TYPE_RE.match(normalized):
        raise ValueError(f"안전하지 않은 데이터 타입: {data_type!r}")
    return normalized


def _sanitize_column_default(default: Any) -> str | None:
    """DEFAULT 표현식은 허용된 리터럴/함수만 통과시킨다."""
    if default is None:
        return None

    normalized = str(default).strip()
    if not normalized:
        return None

    if _SAFE_DEFAULT_RE.match(normalized):
        return normalized
    return None


def _build_column_definition(column: dict[str, Any]) -> str:
    """information_schema/manifest 공용 컬럼 정의 생성기."""
    col_name = column["name"]
    data_type = _validate_data_type_declaration(column["data_type"])
    max_length = column.get("character_maximum_length")
    is_nullable = column.get("is_nullable")
    original_default = column.get("column_default")
    safe_default = _sanitize_column_default(original_default)

    _validate_identifier(col_name)

    col_def = f"    {quote_ident(col_name)} {data_type}"
    if max_length:
        col_def += f"({int(max_length)})"
    if is_nullable == "NO":
        col_def += " NOT NULL"
    if safe_default:
        col_def += f" DEFAULT {safe_default}"
    elif original_default:
        logger.warning("안전하지 않은 DEFAULT 값 스킵: %s = %s", col_name, original_default)

    return col_def


class TableCreator:
    """대상 테이블 생성 클래스

    SQL 경계 규칙(감사 H-04 / M-14)
    - relation은 모두 `"public"."이름"`으로 한정하고 식별자는 quote한다. 연결의
      search_path가 바뀌어도(선행 스키마의 동명 객체) public 객체만 건드린다.
    - 부모 테이블·파티션·인덱스·트리거/RULE·CLUSTER·`partition_table_info` 기록은
      **한 트랜잭션**이다. `create_partition_table()`이 끝에 한 번 커밋하고, 실패하면
      rollback해 일부만 남지 않는다(테이블 없는 metadata, metadata 없는 테이블 모두 금지).
    - 실패해도 되는 DDL(중복 인덱스, CLUSTER 실패)은 SAVEPOINT로 격리해 트랜잭션을
      중단시키지 않는다. 그 밖의 오류는 즉시 실패한다.
    - psycopg2(COPY 워커)·psycopg3(legacy 워커) 연결이 모두 들어오므로 예외는 드라이버
      클래스가 아니라 SQLSTATE로 분류한다(`postgres_utils.sqlstate_of`).
    """

    def __init__(self, source_conn: Any, target_conn: Any):
        """
        Args:
            source_conn: 소스 연결. DB-API 호환이면 되고 psycopg2/psycopg3 둘 다 들어온다
                (CopyMigrationWorker는 psycopg2, MigrationWorker는 psycopg3).
                ManifestTableCreator처럼 소스가 없는 경우 None이 들어온다.
            target_conn: 대상 연결. 동일하게 duck-typed.
        """
        self.source_conn = source_conn
        self.target_conn = target_conn

    def create_partition_table(self, partition_name: str) -> bool:
        """
        파티션 테이블 생성

        Args:
            partition_name: 생성할 파티션 테이블 이름

        Returns:
            성공 여부
        """
        try:
            # 부모 테이블 이름 추출 (예: point_history_221026 -> point_history)
            _validate_identifier(partition_name)
            parent_table = "_".join(partition_name.split("_")[:-1])
            _validate_identifier(parent_table)

            # 소스에서 파티션 정보 가져오기
            partition_info = self._get_partition_info(partition_name, parent_table)
            if not partition_info:
                raise Exception(f"파티션 정보를 찾을 수 없습니다: {partition_name}")

            table_type = partition_info["table_type"]
            table_type_name = TABLE_TYPE_CONFIG[table_type].display_name

            print(f"파티션 정보: {partition_name}")
            print(f"  - 테이블 타입: {table_type_name} ({partition_info['table_data']})")
            print(f"  - 파티셔닝: {'TRIGGER' if table_type.uses_trigger else 'RULE'}")

            # 여기서부터 대상 쪽 DDL과 metadata는 한 트랜잭션이다(끝에서 한 번 커밋).
            # 대상에 부모 테이블 존재 확인
            if not self._check_parent_table_exists(parent_table):
                print(f"부모 테이블 {parent_table}이 없어 생성합니다")
                # 부모 테이블 생성 (table_type 전달)
                self._create_parent_table(parent_table, table_type)

            # 파티션 테이블 생성
            print(f"파티션 테이블 {partition_name} 생성 중...")
            self._create_partition(partition_name, parent_table, partition_info)

            # partition_table_info에 추가 (같은 트랜잭션)
            self._add_partition_info(partition_name, partition_info, commit=False)

            # DDL과 metadata를 함께 확정한다. 트랜잭션이 중단 상태면 커밋하지 않고 실패한다.
            commit_or_raise(self.target_conn)

            print(f"[OK] 파티션 테이블 생성 완료: {partition_name}")
            return True

        except Exception as e:
            # 일부 DDL이나 metadata만 남지 않게 되돌리고, 연결을 중단 상태로 남기지 않는다.
            rollback_quietly(self.target_conn)
            logger.exception("테이블 생성 실패: %s", partition_name)
            raise Exception(f"테이블 생성 오류: {str(e)}") from e

    def _get_partition_info(self, partition_name: str, parent_table: str) -> dict[str, Any]:
        """
        소스에서 파티션 정보 조회

        Returns:
            파티션 정보 딕셔너리 (table_data, table_type, from_date, to_date 포함)
        """
        with self.source_conn.cursor() as cur:
            # partition_table_info에서 정보 조회
            cur.execute(
                """
                SELECT table_data, from_date, to_date
                FROM public.partition_table_info
                WHERE table_name = %s
            """,
                (partition_name,),
            )

            row = cur.fetchone()
            if row:
                table_data_code = row[0]  # e.g. 'PH', 'PS', 'TH', 'ED', 'RT'

                # TableType enum으로 변환
                try:
                    table_type = TableType(table_data_code)
                except ValueError:
                    # 알 수 없는 타입은 기본값 사용
                    table_type = TableType.POINT_HISTORY

                return {
                    "table_data": table_data_code,
                    "table_type": table_type,
                    "from_date": row[1],
                    "to_date": row[2],
                }

            # partition_table_info에 없으면 파티션 이름에서 추측
            # parent_table 기반으로 테이블 타입 추론
            try:
                table_type = get_table_type(parent_table)
            except Exception:
                table_type = TableType.POINT_HISTORY

            inferred_from, inferred_to = infer_partition_range(table_type, partition_name)
            if inferred_from is not None and inferred_to is not None:
                return {
                    "table_data": table_type.value,
                    "table_type": table_type,
                    "from_date": inferred_from,
                    "to_date": inferred_to,
                }

            # 날짜 파싱이 안 되는 경우라도 테이블 타입만 설정해 반환
            return {
                "table_data": table_type.value,
                "table_type": table_type,
                "from_date": None,
                "to_date": None,
            }

    def _check_parent_table_exists(self, parent_table: str) -> bool:
        """부모 테이블 존재 확인"""
        with self.target_conn.cursor() as cur:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                )
            """,
                (parent_table,),
            )
            row = cur.fetchone()
            return bool(row and row[0])

    def _create_parent_table(self, parent_table: str, table_type: TableType | None = None):
        """
        부모 테이블 생성

        Args:
            parent_table: 테이블 이름 (예: 'point_history')
            table_type: 테이블 타입 (None이면 parent_table에서 추론)
        """
        # table_type이 없으면 parent_table 이름에서 추론
        if table_type is None:
            try:
                table_type = get_table_type(parent_table)
            except ValueError:
                raise Exception(f"알 수 없는 테이블 타입: {parent_table}")

        with self.source_conn.cursor() as source_cur:
            # 소스에서 테이블 구조 가져오기
            source_cur.execute(
                """
                SELECT
                    column_name,
                    data_type,
                    character_maximum_length,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_schema = 'public'
                AND table_name = %s
                ORDER BY ordinal_position
            """,
                (parent_table,),
            )

            columns = source_cur.fetchall()
            if not columns:
                raise Exception(f"소스 테이블 구조를 찾을 수 없습니다: {parent_table}")

        # CREATE TABLE 문 생성 (식별자 검증으로 SQL injection 방지)
        _validate_identifier(parent_table)
        column_defs = []

        for col in columns:
            column_defs.append(
                _build_column_definition(
                    {
                        "name": col[0],
                        "data_type": col[1],
                        "character_maximum_length": col[2],
                        "is_nullable": col[3],
                        "column_default": col[4],
                    }
                )
            )

        self._execute_parent_ddl(parent_table, table_type, column_defs)

    def _execute_parent_ddl(
        self, parent_table: str, table_type: TableType, column_defs: list[str]
    ) -> None:
        """부모 테이블 DDL(+트리거/인덱스)을 실행한다. 커밋하지 않는다(호출 트랜잭션에 포함).

        ManifestTableCreator도 컬럼 정의만 다르게 만들어 이 경로를 쓴다.
        """
        _validate_identifier(parent_table)
        config = TABLE_TYPE_CONFIG[table_type]
        create_sql = f"CREATE TABLE IF NOT EXISTS {qualified_name(parent_table)} (\n"
        create_sql += ",\n".join(column_defs) + "\n)"

        # 대상에 테이블 생성
        with self.target_conn.cursor() as target_cur:
            target_cur.execute(create_sql)

            # 테이블 타입별 처리
            if config.uses_trigger:
                # TRIGGER 기반 파티셔닝 설정 (point_history)
                self._create_trigger_based_partitioning(parent_table, table_type, target_cur)
            elif config.uses_rules:
                # RULE 기반 파티셔닝은 파티션별로 생성되므로 여기서는 스킵
                # 인덱스만 생성
                self._create_parent_indexes(parent_table, table_type, target_cur)

    def _create_partition(
        self, partition_name: str, parent_table: str, partition_info: dict[str, Any]
    ):
        """
        파티션 테이블 생성

        Args:
            partition_name: 파티션 테이블 이름
            parent_table: 부모 테이블 이름
            partition_info: 파티션 정보 (table_type, from_date, to_date 포함)
        """
        # 테이블 타입 확인
        table_type = partition_info.get("table_type")
        if table_type is None:
            # partition_info에 table_type이 없으면 추론
            try:
                table_type = get_table_type(parent_table)
            except ValueError:
                raise Exception(f"알 수 없는 테이블 타입: {parent_table}")

        config = TABLE_TYPE_CONFIG[table_type]
        from_date = partition_info.get("from_date")
        to_date = partition_info.get("to_date")

        # 식별자 검증
        _validate_identifier(partition_name)
        _validate_identifier(parent_table)
        _validate_identifier(config.date_column)
        partition_rel = qualified_name(partition_name)
        parent_rel = qualified_name(parent_table)
        date_col = quote_ident(config.date_column)

        with self.target_conn.cursor() as cur:
            # CHECK constraint 생성
            date_check = None
            if from_date is not None and to_date is not None:
                # 날짜 값은 정수로 검증
                from_date_val = int(from_date)
                to_date_val = int(to_date)
                if config.date_is_timestamp:
                    from_ts = f"to_timestamp({from_date_val}::double precision / 1000)"
                    to_ts = f"to_timestamp({to_date_val}::double precision / 1000)"
                    date_check = f"CHECK({date_col} >= {from_ts} AND {date_col} <= {to_ts})"
                else:
                    date_check = (
                        f"CHECK({date_col} >= {from_date_val} AND {date_col} <= {to_date_val})"
                    )

            # 테이블 타입별 constraint 추가
            constraints = []
            pk_columns = get_partition_primary_key_columns(table_type)
            if pk_columns:
                for pk_col in pk_columns:
                    _validate_identifier(pk_col)
                pk_list = ", ".join(quote_ident(c) for c in pk_columns)
                constraints.append(
                    f"CONSTRAINT {quote_ident(partition_name + '_pkey')} PRIMARY KEY({pk_list})"
                )
            if date_check:
                constraints.append(
                    f"CONSTRAINT {quote_ident(partition_name + '_issued_date_check')} {date_check}"
                )

            # CREATE TABLE 문 생성 (필수 DDL — 실패하면 즉시 예외)
            if constraints:
                constraint_str = ",\n        ".join(constraints)
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_rel} (
                        {constraint_str}
                    ) INHERITS ({parent_rel})
                """
            else:
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_rel}
                    INHERITS ({parent_rel})
                """

            cur.execute(create_sql)

            # RULE 기반 파티셔닝인 경우 RULE 생성
            if config.uses_rules:
                self._create_rule_for_partition(
                    parent_table, partition_name, table_type, partition_info, cur
                )

            # RT는 historical DDL 관례상 partition-level 보조 인덱스를 추가로 생성
            if table_type == TableType.RUNNING_TIME_HISTORY:
                self._create_indexes(
                    cur,
                    [
                        f"CREATE INDEX {quote_ident(partition_name + '_idx')} ON {partition_rel} "
                        f"USING btree (path_id, issued_date)",
                    ],
                )

            # historical DDL 관례상 일부 타입은 partition PK로 CLUSTER.
            # 선택 DDL: 실패해도 SAVEPOINT로 격리해 트랜잭션을 중단시키지 않는다(M-14).
            if should_cluster_partition_by_pkey(table_type):
                clustered = run_optional_statement(
                    self.target_conn,
                    cur,
                    f"CLUSTER {partition_rel} USING {quote_ident(partition_name + '_pkey')}",
                    ignorable=IGNORABLE_CLUSTER_SQLSTATES,
                    label=f"CLUSTER {partition_name}",
                )
                if clustered:
                    print(f"  [OK] 클러스터링 완료: {partition_name}")
                else:
                    print(f"  [WARN] 클러스터링 스킵: {partition_name}")
            # 커밋하지 않는다 — create_partition_table()이 metadata까지 묶어 커밋한다.

    def _sync_partition_info(self, partition_name: str):
        """소스 DB의 partition_table_info를 대상 DB에 동기화"""
        with self.source_conn.cursor() as cur:
            cur.execute(
                "SELECT table_data, from_date, to_date FROM public.partition_table_info "
                "WHERE table_name = %s",
                (partition_name,),
            )
            row = cur.fetchone()
        if row:
            self._add_partition_info(
                partition_name,
                {
                    "table_data": row[0],
                    "from_date": row[1],
                    "to_date": row[2],
                },
            )

    def _add_partition_info(
        self, partition_name: str, partition_info: dict[str, Any], *, commit: bool = True
    ):
        """partition_table_info에 정보 추가 또는 갱신 (upsert)

        Args:
            commit: False면 호출자 트랜잭션에 남긴다(테이블 생성과 함께 커밋하기 위해).
                True(기존 동작)면 여기서 커밋하고, 실패하면 rollback한다.
        """
        try:
            self._upsert_partition_info(partition_name, partition_info)
            if commit:
                commit_or_raise(self.target_conn)
        except Exception:
            if commit:
                rollback_quietly(self.target_conn)
            raise

    def _upsert_partition_info(self, partition_name: str, partition_info: dict[str, Any]):
        with self.target_conn.cursor() as cur:
            # partition_table_info 테이블 존재 확인
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'partition_table_info'
                )
            """)

            row = cur.fetchone()
            if not (row and row[0]):
                # 테이블 생성
                cur.execute("""
                    CREATE TABLE public.partition_table_info (
                        table_name varchar(100) NOT NULL,
                        table_data varchar(10) NOT NULL,
                        from_date bigint NOT NULL,
                        to_date bigint NOT NULL,
                        use_flag boolean NOT NULL,
                        save_date timestamp NOT NULL,
                        cluster_index boolean DEFAULT false
                    )
                """)

            # 기존 레코드 확인
            cur.execute(
                "SELECT 1 FROM public.partition_table_info WHERE table_name = %s",
                (partition_name,),
            )

            now = datetime.now()
            if cur.fetchone():
                # 기존 레코드 갱신
                cur.execute(
                    """
                    UPDATE public.partition_table_info
                    SET table_data = %s, from_date = %s, to_date = %s,
                        use_flag = %s, save_date = %s, cluster_index = %s
                    WHERE table_name = %s
                """,
                    (
                        partition_info["table_data"],
                        partition_info["from_date"],
                        partition_info["to_date"],
                        True,
                        now,
                        True,
                        partition_name,
                    ),
                )
            else:
                # 새 레코드 추가
                cur.execute(
                    """
                    INSERT INTO public.partition_table_info
                    (table_name, table_data, from_date, to_date, use_flag, save_date, cluster_index)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        partition_name,
                        partition_info["table_data"],
                        partition_info["from_date"],
                        partition_info["to_date"],
                        True,
                        now,
                        True,
                    ),
                )

    def ensure_partition_ready(
        self, partition_name: str, truncate_mode: str = "auto", confirm_callback=None
    ) -> tuple[bool, int]:
        """파티션 테이블 준비 (생성 또는 TRUNCATE)

        Args:
            partition_name: 파티션 테이블 이름
            truncate_mode:
                - 'auto': 데이터가 있으면 자동 TRUNCATE
                - 'ask': 데이터가 있으면 사용자 확인 후 TRUNCATE
                - 'keep': 데이터가 있어도 TRUNCATE 하지 않고 그대로 진행(재개/스모크 용)
            confirm_callback: 사용자 확인 콜백 함수 (truncate_mode='ask'일 때)
                             함수 시그니처: callback(partition_name: str, row_count: int) -> bool

        Returns:
            (table_created, existing_row_count)
            - table_created: 테이블이 새로 생성되었는지 여부
            - existing_row_count: 테이블이 이미 존재했을 때의 기존 행 수

        Raises:
            ValueError: truncate_mode가 잘못되었거나 'ask' 모드에서 callback이 없을 때
            Exception: 사용자가 TRUNCATE를 거부했을 때
        """
        with self.target_conn.cursor() as cursor:
            # 테이블 존재 확인
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = %s
                )
            """,
                (partition_name,),
            )

            exists_row = cursor.fetchone()
            table_exists = bool(exists_row and exists_row[0])

            if not table_exists:
                # 테이블 생성 (partition_table_info 동기화 포함)
                self.create_partition_table(partition_name)
                return (True, 0)

            # 테이블이 이미 존재해도 partition_table_info 동기화
            self._sync_partition_info(partition_name)

            # 기존 데이터 확인. 식별자는 검증 후 드라이버 무관 quote + public 한정(H-04).
            # NOTE: psycopg2/psycopg3 연결이 섞여 들어오므로 드라이버의 sql.Identifier 대신
            #       postgres_utils.qualified_name을 쓴다.
            _validate_identifier(partition_name)
            partition_rel = qualified_name(partition_name)
            cursor.execute(f"SELECT COUNT(*) FROM {partition_rel}")
            row_count = cursor.fetchone()[0]

            if row_count > 0:
                # TRUNCATE 모드에 따라 처리
                if truncate_mode == "auto":
                    should_truncate = True
                elif truncate_mode == "ask":
                    if confirm_callback is None:
                        raise ValueError("confirm_callback required for 'ask' mode")
                    should_truncate = confirm_callback(partition_name, row_count)
                elif truncate_mode == "keep":
                    # 재개(resume) 등에서 부분 데이터가 이미 존재하는 상황
                    # (TRUNCATE하지 않고 그대로 진행)
                    should_truncate = False
                else:
                    raise ValueError(f"Invalid truncate_mode: {truncate_mode}")

                if should_truncate:
                    cursor.execute(f"TRUNCATE TABLE {partition_rel} RESTART IDENTITY")
                    # TRUNCATE는 커밋하지 않음 — 호출자의 COPY/INSERT와 같은
                    # 트랜잭션에서 처리되어야 migration 실패 시 롤백 가능
                else:
                    # keep 모드는 예외 없이 진행
                    if truncate_mode != "keep":
                        raise Exception(f"기존 데이터 처리가 취소되었습니다: {partition_name}")

            return (False, row_count)

    def _create_trigger_based_partitioning(self, parent_table: str, table_type: TableType, cursor):
        """
        TRIGGER 기반 파티셔닝 설정 (point_history / point_sec_history용)

        Args:
            parent_table: 부모 테이블 이름
            table_type: 테이블 타입
            cursor: 데이터베이스 커서
        """
        _validate_identifier(parent_table)
        parent_rel = qualified_name(parent_table)

        # 인덱스 생성 (9.3 호환: IF NOT EXISTS 미지원 → 중복은 SAVEPOINT로 격리해 건너뜀)
        self._create_indexes(
            cursor,
            [
                f"CREATE INDEX {quote_ident(parent_table + '_path_id_date')} ON {parent_rel} "
                f"USING btree (path_id, issued_date)",
                f"CREATE INDEX {quote_ident(parent_table + '_path_id_idx')} ON {parent_rel} "
                f"USING btree (path_id)",
            ],
        )

        func_name = f"{parent_table}_partition_insert"
        trigger_name = f"insert_{parent_table}_trigger"
        _validate_identifier(func_name)
        _validate_identifier(trigger_name)
        func_rel = qualified_name(func_name)

        # 트리거 함수 생성 (필수 DDL, parent_table은 이미 검증됨).
        # 동적 INSERT 대상도 public으로 한정한다 — 이름만 쓰면 트리거가 실행되는 세션의
        # search_path에 따라 다른 스키마의 동명 파티션에 행이 들어간다(H-04).
        cursor.execute(f"""
            CREATE OR REPLACE FUNCTION {func_rel}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                _insert_time bigint;
                _insert_date text;

            BEGIN
                _insert_time := (NEW.issued_date/1000)::bigint;
                _insert_date := to_char(to_timestamp(_insert_time), 'YYMMDD');

                EXECUTE format('INSERT INTO %I.%I VALUES ($1.*)', 'public', '{parent_table}_' || _insert_date) USING NEW;

                RETURN NULL;
            END;
            $function$
        """)

        # 트리거 생성 (필수 DDL)
        cursor.execute(f"DROP TRIGGER IF EXISTS {quote_ident(trigger_name)} ON {parent_rel}")
        cursor.execute(f"""
            CREATE TRIGGER {quote_ident(trigger_name)}
            BEFORE INSERT ON {parent_rel}
            FOR EACH ROW EXECUTE PROCEDURE {func_rel}()
        """)

    def _create_parent_indexes(self, parent_table: str, table_type: TableType, cursor):
        """
        부모 테이블 인덱스 생성

        Args:
            parent_table: 부모 테이블 이름
            table_type: 테이블 타입
            cursor: 데이터베이스 커서
        """
        _validate_identifier(parent_table)
        parent_rel = qualified_name(parent_table)

        def index(suffix: str, columns: str) -> str:
            return (
                f"CREATE INDEX {quote_ident(parent_table + suffix)} ON {parent_rel} "
                f"USING btree ({columns})"
            )

        # 테이블 타입별 인덱스 (9.3 호환: IF NOT EXISTS 미지원 → 중복은 SAVEPOINT로 격리해 건너뜀)
        if table_type in (
            TableType.POINT_HISTORY,
            TableType.POINT_SEC_HISTORY,
            TableType.TREND_HISTORY,
        ):
            # PH, PS, TH: path_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    index("_path_id_date", "path_id, issued_date"),
                    index("_path_id_idx", "path_id"),
                ],
            )

        elif table_type == TableType.ENERGY_DISPLAY:
            # ED: sensor_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    index("_sensor_id_date", "sensor_id, issued_date"),
                    index("_station_id_idx", "station_id"),
                ],
            )

        elif table_type == TableType.RUNNING_TIME_HISTORY:
            # RT: path_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    index("_path_id_date", "path_id, issued_date"),
                    index("_path_id_idx", "path_id"),
                ],
            )

    def _create_rule_for_partition(
        self,
        parent_table: str,
        partition_name: str,
        table_type: TableType,
        partition_info: dict[str, Any],
        cursor,
    ):
        """
        파티션 테이블에 대한 RULE 생성

        Args:
            parent_table: 부모 테이블 이름
            partition_name: 파티션 테이블 이름
            table_type: 테이블 타입
            partition_info: 파티션 정보 (from_date, to_date 포함)
            cursor: 데이터베이스 커서
        """
        config = TABLE_TYPE_CONFIG[table_type]
        from_date = partition_info["from_date"]
        to_date = partition_info["to_date"]

        # 식별자 검증
        _validate_identifier(parent_table)
        _validate_identifier(partition_name)
        _validate_identifier(config.date_column)
        for col in config.columns:
            _validate_identifier(col)
        date_col = quote_ident(config.date_column)

        # 날짜 조건 생성 (타입에 따라 다름)
        date_condition = None
        if from_date is not None and to_date is not None:
            # 날짜 값은 정수로 강제 변환하여 injection 방지
            from_date_val = int(from_date)
            to_date_val = int(to_date)
            if config.date_is_timestamp:
                from_dt = datetime.fromtimestamp(from_date_val / 1000)
                to_dt = datetime.fromtimestamp(to_date_val / 1000)

                date_condition = f"""(new.{date_col} >= '{from_dt.strftime("%Y-%m-%d %H:%M:%S")}'::timestamp without time zone)
                AND (new.{date_col} <= '{to_dt.strftime("%Y-%m-%d %H:%M:%S")}'::timestamp without time zone)"""
            else:
                date_condition = f"""(new.{date_col} >= '{from_date_val}'::bigint)
                AND (new.{date_col} <= '{to_date_val}'::bigint)"""

        # 컬럼 리스트 생성
        columns = ", ".join(quote_ident(col) for col in config.columns)
        values = ", ".join([f"new.{quote_ident(col)}" for col in config.columns])
        parent_rel = qualified_name(parent_table)
        partition_rel = qualified_name(partition_name)

        # RULE 생성 SQL (날짜 범위가 없으면 RULE 생성을 건너뜀)
        if date_condition:
            rule_name = f"rule_{partition_name}"
            _validate_identifier(rule_name)

            # 기존 RULE 제거 (있다면)
            cursor.execute(f"DROP RULE IF EXISTS {quote_ident(rule_name)} ON {parent_rel}")
            print(f"  - RULE 재생성: {rule_name} (기존 RULE 삭제 후 생성)")

            rule_sql = f"""
                CREATE RULE {quote_ident(rule_name)} AS
                ON INSERT TO {parent_rel}
                WHERE {date_condition}
                DO INSTEAD INSERT INTO {partition_rel} ({columns})
                VALUES ({values})
            """

            cursor.execute(rule_sql)
        else:
            print(f"  [WARN] RULE 생성을 건너뜀(날짜 범위 없음): {partition_name}")

    def _create_indexes(self, cursor, statements: list[str]):
        """IF NOT EXISTS가 없는 환경(9.3)에서도 안전하게 인덱스 생성.

        각 문장을 SAVEPOINT로 격리한다. 중복·권한 부족(IGNORABLE_INDEX_SQLSTATES)은 그 문장만
        되돌리고 계속하며, 그 밖의 오류는 즉시 올린다(감사 M-14). 예전에는 예외를 잡기만 해서
        트랜잭션이 중단된 채로 다음 DDL을 보냈고, psycopg3 예외 클래스로만 분기해 psycopg2
        연결에서는 분기 자체가 타지 않았다.
        """
        for stmt in statements:
            if not run_optional_statement(
                self.target_conn,
                cursor,
                stmt,
                ignorable=IGNORABLE_INDEX_SQLSTATES,
                label=stmt.split()[2],
            ):
                print(f"  [WARN] 인덱스 생성 스킵: {stmt.split()[2]}")
