"""
대상 테이블 생성 모듈

파티션 테이블의 스키마를 소스에서 복제하고,
테이블 타입에 따라 TRIGGER 또는 RULE을 생성합니다.
"""
from typing import Dict, Any, Tuple
from datetime import datetime

import psycopg

from .table_types import TableType, get_table_type, TABLE_TYPE_CONFIG


class TableCreator:
    """대상 테이블 생성 클래스"""

    def __init__(self, source_conn: psycopg.Connection, target_conn: psycopg.Connection):
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
            parent_table = '_'.join(partition_name.split('_')[:-1])

            # 소스에서 파티션 정보 가져오기
            partition_info = self._get_partition_info(partition_name, parent_table)
            if not partition_info:
                raise Exception(f"파티션 정보를 찾을 수 없습니다: {partition_name}")

            table_type = partition_info['table_type']
            table_type_name = TABLE_TYPE_CONFIG[table_type].display_name

            print(f"파티션 정보: {partition_name}")
            print(f"  - 테이블 타입: {table_type_name} ({partition_info['table_data']})")
            print(f"  - 파티셔닝: {'TRIGGER' if table_type.uses_trigger else 'RULE'}")

            # 대상에 부모 테이블 존재 확인
            if not self._check_parent_table_exists(parent_table):
                print(f"부모 테이블 {parent_table}이 없어 생성합니다")
                # 부모 테이블 생성 (table_type 전달)
                self._create_parent_table(parent_table, table_type)

            # 파티션 테이블 생성
            print(f"파티션 테이블 {partition_name} 생성 중...")
            self._create_partition(partition_name, parent_table, partition_info)

            # partition_table_info에 추가
            self._add_partition_info(partition_name, partition_info)

            print(f"✓ 파티션 테이블 생성 완료: {partition_name}")
            return True

        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"테이블 생성 오류: {str(e)}")

    def _get_partition_info(self, partition_name: str, parent_table: str) -> Dict[str, Any]:
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
                FROM partition_table_info
                WHERE table_name = %s
            """, (partition_name,))

            row = cur.fetchone()
            if row:
                table_data_code = row[0]  # 'PH', 'TH', 'ED', 'RT'

                # TableType enum으로 변환
                try:
                    table_type = TableType(table_data_code)
                except ValueError:
                    # 알 수 없는 타입은 기본값 사용
                    table_type = TableType.POINT_HISTORY

                return {
                    'table_data': table_data_code,
                    'table_type': table_type,
                    'from_date': row[1],
                    'to_date': row[2]
                }

            # partition_table_info에 없으면 파티션 이름에서 추측
            # 날짜 추출 (예: point_history_221026 -> 22, 10, 26)
            parts = partition_name.split("_")
            table_type = None

            # parent_table 기반으로 테이블 타입 추론
            try:
                table_type = get_table_type(parent_table)
            except Exception:
                table_type = TableType.POINT_HISTORY

            if len(parts) >= 3 and len(parts[-1]) == 6:
                # yymmdd 형태
                date_str = parts[-1]  # 예: 221026
                year = 2000 + int(date_str[:2])
                month = int(date_str[2:4])
                day = int(date_str[4:6])

                from_date = datetime(year, month, day, 0, 0, 0)
                to_date = datetime(year, month, day, 23, 59, 59, 999000)

                return {
                    'table_data': table_type.value,
                    'table_type': table_type,
                    'from_date': int(from_date.timestamp() * 1000),
                    'to_date': int(to_date.timestamp() * 1000)
                }

            # 날짜 파싱이 안 되는 경우라도 테이블 타입만 설정해 반환
            return {
                'table_data': table_type.value,
                'table_type': table_type,
                'from_date': None,
                'to_date': None,
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
            return cur.fetchone()[0]

    def _create_parent_table(self, parent_table: str, table_type: TableType = None):
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

        config = TABLE_TYPE_CONFIG[table_type]

        with self.source_conn.cursor() as source_cur:
            # 소스에서 테이블 구조 가져오기
            source_cur.execute("""
                SELECT
                    column_name,
                    data_type,
                    character_maximum_length,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
            """, (parent_table,))

            columns = source_cur.fetchall()
            if not columns:
                raise Exception(f"소스 테이블 구조를 찾을 수 없습니다: {parent_table}")

        # CREATE TABLE 문 생성
        create_sql = f"CREATE TABLE IF NOT EXISTS {parent_table} (\n"
        column_defs = []

        for col in columns:
            col_name, data_type, max_length, is_nullable, default = col

            # 컬럼 정의 생성
            col_def = f"    {col_name} {data_type}"

            if max_length:
                col_def += f"({max_length})"

            if is_nullable == 'NO':
                col_def += " NOT NULL"

            if default:
                col_def += f" DEFAULT {default}"

            column_defs.append(col_def)

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

            self.target_conn.commit()

    def _create_partition(self, partition_name: str, parent_table: str,
                         partition_info: Dict[str, Any]):
        """
        파티션 테이블 생성

        Args:
            partition_name: 파티션 테이블 이름
            parent_table: 부모 테이블 이름
            partition_info: 파티션 정보 (table_type, from_date, to_date 포함)
        """
        # 테이블 타입 확인
        table_type = partition_info.get('table_type')
        if table_type is None:
            # partition_info에 table_type이 없으면 추론
            try:
                table_type = get_table_type(parent_table)
            except ValueError:
                raise Exception(f"알 수 없는 테이블 타입: {parent_table}")

        config = TABLE_TYPE_CONFIG[table_type]
        from_date = partition_info.get('from_date')
        to_date = partition_info.get('to_date')

        with self.target_conn.cursor() as cur:
            # CHECK constraint 생성
            date_check = None
            if from_date is not None and to_date is not None:
                if config.date_is_timestamp:
                    # 밀리초 → timestamp 변환 후 비교 (9.3 호환)
                    from_ts = f"to_timestamp({from_date}::double precision / 1000)"
                    to_ts = f"to_timestamp({to_date}::double precision / 1000)"
                    date_check = (
                        f"CHECK({config.date_column} >= {from_ts} "
                        f"AND {config.date_column} <= {to_ts})"
                    )
                else:
                    date_check = (
                        f"CHECK({config.date_column} >= {from_date} "
                        f"AND {config.date_column} <= {to_date})"
                    )

            # 테이블 타입별 constraint 추가
            constraints = []

            if table_type == TableType.POINT_HISTORY:
                # PH: PRIMARY KEY 추가
                constraints.append(
                    f"CONSTRAINT {partition_name}_pkey PRIMARY KEY(path_id, issued_date)"
                )
                if date_check:
                    constraints.append(f"CONSTRAINT {partition_name}_issued_date_check {date_check}")

            elif table_type == TableType.TREND_HISTORY:
                # TH: CHECK만
                if date_check:
                    constraints.append(f"CONSTRAINT {partition_name}_issued_date_check {date_check}")

            elif table_type == TableType.ENERGY_DISPLAY:
                # ED: CHECK만 (timestamp 타입)
                if date_check:
                    constraints.append(f"CONSTRAINT {partition_name}_issued_date_check {date_check}")

            elif table_type == TableType.RUNNING_TIME_HISTORY:
                # RT: CHECK만
                if date_check:
                    constraints.append(f"CONSTRAINT {partition_name}_issued_date_check {date_check}")

            # CREATE TABLE 문 생성
            if constraints:
                constraint_str = ",\n        ".join(constraints)
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_name} (
                        {constraint_str}
                    ) INHERITS ({parent_table})
                """
            else:
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_name}
                    INHERITS ({parent_table})
                """

            cur.execute(create_sql)

            # RULE 기반 파티셔닝인 경우 RULE 생성
            if config.uses_rules:
                self._create_rule_for_partition(
                    parent_table,
                    partition_name,
                    table_type,
                    partition_info,
                    cur
                )

            # 클러스터링 (PRIMARY KEY가 있는 경우만)
            if table_type == TableType.POINT_HISTORY:
                try:
                    cur.execute(f"""
                        CLUSTER {partition_name} USING {partition_name}_pkey
                    """)
                    print(f"  ✓ 클러스터링 완료: {partition_name}")
                except psycopg.errors.UndefinedObject:
                    # 인덱스가 없는 경우 (정상)
                    print(f"  ⚠ 클러스터링 스킵: {partition_name} - PRIMARY KEY 인덱스가 없음")
                except psycopg.errors.InsufficientPrivilege:
                    # 권한 부족 (경고)
                    print(f"  ⚠ 클러스터링 실패: {partition_name} - 권한 부족")
                except Exception as e:
                    # 기타 예외 (로깅만)
                    print(f"  ⚠ 클러스터링 실패: {partition_name} - {type(e).__name__}: {e}")

            self.target_conn.commit()

    def _add_partition_info(self, partition_name: str, partition_info: Dict[str, Any]):
        """partition_table_info에 정보 추가"""
        with self.target_conn.cursor() as cur:
            # partition_table_info 테이블 존재 확인
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public'
                    AND table_name = 'partition_table_info'
                )
            """)

            if not cur.fetchone()[0]:
                # 테이블 생성
                cur.execute("""
                    CREATE TABLE partition_table_info (
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
                """
                SELECT 1 FROM partition_table_info
                WHERE table_name = %s
            """,
                (partition_name,),
            )

            if not cur.fetchone():
                # 새 레코드 추가
                cur.execute(
                    """
                    INSERT INTO partition_table_info
                    (table_name, table_data, from_date, to_date, use_flag, save_date, cluster_index)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    partition_name,
                    partition_info['table_data'],
                    partition_info['from_date'],
                    partition_info['to_date'],
                    True,
                    datetime.now(),
                    True
                ))

            self.target_conn.commit()

    def ensure_partition_ready(
        self, partition_name: str, truncate_mode: str = "auto", confirm_callback=None
    ) -> Tuple[bool, int]:
        """파티션 테이블 준비 (생성 또는 TRUNCATE)

        Args:
            partition_name: 파티션 테이블 이름
            truncate_mode: 'auto' (자동 TRUNCATE), 'ask' (사용자 확인)
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

            table_exists = cursor.fetchone()[0]

            if not table_exists:
                # 테이블 생성
                self.create_partition_table(partition_name)
                return (True, 0)

            # 기존 데이터 확인
            cursor.execute(f"SELECT COUNT(*) FROM {partition_name}")
            row_count = cursor.fetchone()[0]

            if row_count > 0:
                # TRUNCATE 모드에 따라 처리
                if truncate_mode == "auto":
                    should_truncate = True
                elif truncate_mode == "ask":
                    if confirm_callback is None:
                        raise ValueError("confirm_callback required for 'ask' mode")
                    should_truncate = confirm_callback(partition_name, row_count)
                else:
                    raise ValueError(f"Invalid truncate_mode: {truncate_mode}")

                if should_truncate:
                    cursor.execute(f"TRUNCATE TABLE {partition_name} RESTART IDENTITY")
                    self.target_conn.commit()
                else:
                    raise Exception(f"기존 데이터 처리가 취소되었습니다: {partition_name}")

            return (False, row_count)

    def _create_trigger_based_partitioning(self, parent_table: str, table_type: TableType, cursor):
        """
        TRIGGER 기반 파티셔닝 설정 (point_history용)

        Args:
            parent_table: 부모 테이블 이름
            table_type: 테이블 타입
            cursor: 데이터베이스 커서
        """
        # 인덱스 생성 (9.3 호환: IF NOT EXISTS 미지원 → 중복은 예외 무시)
        self._create_indexes(
            cursor,
            [
                f"CREATE INDEX {parent_table}_path_id_date ON {parent_table} USING btree (path_id, issued_date)",
                f"CREATE INDEX {parent_table}_path_id_idx ON {parent_table} USING btree (path_id)",
            ],
        )

        # 트리거 함수 생성
        cursor.execute(f"""
            CREATE OR REPLACE FUNCTION {parent_table}_partition_insert()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $function$
            DECLARE
                _insert_time bigint;
                _insert_date text;

            BEGIN
                _insert_time := (NEW.issued_date/1000)::bigint;
                _insert_date := to_char(to_timestamp(_insert_time), 'YYMMDD');

                EXECUTE 'INSERT INTO {parent_table}_'||_insert_date||' VALUES ($1.*);' USING NEW;

                RETURN NULL;
            END;
            $function$
        """)

        # 트리거 생성
        cursor.execute(f"""
            DROP TRIGGER IF EXISTS insert_{parent_table}_trigger ON {parent_table};

            CREATE TRIGGER insert_{parent_table}_trigger
            BEFORE INSERT ON {parent_table}
            FOR EACH ROW EXECUTE PROCEDURE {parent_table}_partition_insert();
        """)

    def _create_parent_indexes(self, parent_table: str, table_type: TableType, cursor):
        """
        부모 테이블 인덱스 생성

        Args:
            parent_table: 부모 테이블 이름
            table_type: 테이블 타입
            cursor: 데이터베이스 커서
        """
        # 테이블 타입별 인덱스 (9.3 호환: IF NOT EXISTS 미지원 → 중복은 예외 무시)
        if table_type == TableType.POINT_HISTORY or table_type == TableType.TREND_HISTORY:
            # PH, TH: path_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    f"CREATE INDEX {parent_table}_path_id_date ON {parent_table} USING btree (path_id, issued_date)",
                    f"CREATE INDEX {parent_table}_path_id_idx ON {parent_table} USING btree (path_id)",
                ],
            )

        elif table_type == TableType.ENERGY_DISPLAY:
            # ED: sensor_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    f"CREATE INDEX {parent_table}_sensor_id_date ON {parent_table} USING btree (sensor_id, issued_date)",
                    f"CREATE INDEX {parent_table}_station_id_idx ON {parent_table} USING btree (station_id)",
                ],
            )

        elif table_type == TableType.RUNNING_TIME_HISTORY:
            # RT: path_id + issued_date 인덱스
            self._create_indexes(
                cursor,
                [
                    f"CREATE INDEX {parent_table}_path_id_date ON {parent_table} USING btree (path_id, issued_date)",
                    f"CREATE INDEX {parent_table}_path_id_idx ON {parent_table} USING btree (path_id)",
                ],
            )

    def _create_rule_for_partition(
        self,
        parent_table: str,
        partition_name: str,
        table_type: TableType,
        partition_info: Dict[str, Any],
        cursor
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
        from_date = partition_info['from_date']
        to_date = partition_info['to_date']

        # 날짜 조건 생성 (타입에 따라 다름)
        date_condition = None
        if from_date is not None and to_date is not None:
            if config.date_is_timestamp:
                # timestamp 타입 (energy_display)
                # bigint timestamp를 timestamp로 변환
                from_dt = datetime.fromtimestamp(from_date / 1000)
                to_dt = datetime.fromtimestamp(to_date / 1000)

                date_condition = f"""(new.{config.date_column} >= '{from_dt.strftime('%Y-%m-%d %H:%M:%S')}'::timestamp without time zone)
                AND (new.{config.date_column} <= '{to_dt.strftime('%Y-%m-%d %H:%M:%S')}'::timestamp without time zone)"""
            else:
                # bigint 타입 (point_history, trend_history, running_time_history)
                date_condition = f"""(new.{config.date_column} >= '{from_date}'::bigint)
                AND (new.{config.date_column} <= '{to_date}'::bigint)"""

        # 컬럼 리스트 생성
        columns = ', '.join(config.columns)
        values = ', '.join([f'new.{col}' for col in config.columns])

        # RULE 생성 SQL (날짜 범위가 없으면 RULE 생성을 건너뜀)
        if date_condition:
            rule_name = f"rule_{partition_name}"

            # 기존 RULE 제거 (있다면)
            cursor.execute(f"""
                DROP RULE IF EXISTS {rule_name} ON {parent_table};
            """)
            print(f"  - RULE 재생성: {rule_name} (기존 RULE 삭제 후 생성)")

            rule_sql = f"""
                CREATE RULE {rule_name} AS
                ON INSERT TO {parent_table}
                WHERE {date_condition}
                DO INSTEAD INSERT INTO {partition_name} ({columns})
                VALUES ({values});
            """

            cursor.execute(rule_sql)
        else:
            print(f"  ⚠ RULE 생성을 건너뜀(날짜 범위 없음): {partition_name}")

    def _create_indexes(self, cursor, statements: list[str]):
        """IF NOT EXISTS가 없는 환경(9.3)에서도 안전하게 인덱스 생성"""
        for stmt in statements:
            try:
                cursor.execute(stmt)
            except psycopg.errors.DuplicateObject:
                # 이미 존재하는 경우 무시
                print(f"  ⚠ 인덱스 생성 스킵(이미 존재): {stmt.split()[2]}")
            except psycopg.errors.InsufficientPrivilege:
                print(f"  ⚠ 인덱스 생성 실패(권한 부족): {stmt}")
            except Exception as exc:
                print(f"  ⚠ 인덱스 생성 실패: {stmt} - {type(exc).__name__}: {exc}")
