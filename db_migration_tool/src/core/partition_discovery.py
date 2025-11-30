"""
파티션 테이블 탐색 및 분석
"""

from datetime import date, datetime
from typing import Any, Optional

import psycopg
from psycopg import sql

from .table_types import TableType, DEFAULT_TABLE_TYPE


class PartitionDiscovery:
    """파티션 테이블 탐색 클래스"""

    def __init__(self, connection_config: dict[str, Any], target_config: dict[str, Any] = None):
        self.source_config = connection_config
        self.target_config = target_config
        self.connection_config = connection_config  # 하위 호환성을 위해 유지

    def discover_partitions(
        self,
        start_date: date,
        end_date: date,
        table_types: Optional[list[TableType]] = None
    ) -> list[dict[str, Any]]:
        """
        날짜 범위에 해당하는 파티션 탐색

        Args:
            start_date: 시작 날짜
            end_date: 종료 날짜
            table_types: 탐색할 테이블 타입 리스트 (기본값: [DEFAULT_TABLE_TYPE])

        Returns:
            파티션 정보 리스트
        """
        partitions = []

        # 기본값 설정 (backward compatibility)
        if table_types is None:
            table_types = [DEFAULT_TABLE_TYPE]

        # 빈 리스트 검증
        if not table_types:
            raise ValueError("최소 1개의 테이블 타입을 지정해야 합니다.")

        # TableType enum을 문자열로 변환
        table_type_codes = [tt.value for tt in table_types]

        try:
            # 연결 생성
            conn = self._create_connection()

            with conn.cursor() as cur:
                # partition_table_info 테이블에서 날짜 범위에 해당하는 파티션 조회
                placeholders = ', '.join(['%s'] * len(table_type_codes))
                query = f"""
                    SELECT
                        table_name,
                        table_data,
                        from_date,
                        to_date,
                        use_flag
                    FROM partition_table_info
                    WHERE table_data IN ({placeholders})
                    AND use_flag = true
                    AND from_date <= %s
                    AND to_date >= %s
                    ORDER BY table_data, from_date
                """

                params = tuple(table_type_codes) + (
                    self._date_to_timestamp(end_date),
                    self._date_to_timestamp(start_date)
                )

                cur.execute(query, params)

                for row in cur.fetchall():
                    table_name, table_data, from_date, to_date, use_flag = row

                    # 날짜 범위 확인
                    partition_start = self._timestamp_to_date(from_date)
                    partition_end = self._timestamp_to_date(to_date)

                    # 선택된 날짜 범위와 겹치는지 확인
                    if partition_start <= end_date and partition_end >= start_date:
                        # 테이블 존재 여부 확인
                        if self._check_table_exists(cur, table_name):
                            # 행 수 조회
                            row_count = self._get_row_count(cur, table_name)

                            # TableType enum으로 변환
                            try:
                                table_type = TableType(table_data)
                            except ValueError:
                                # 알 수 없는 타입은 건너뜀
                                continue

                            partitions.append({
                                'table_name': table_name,
                                'table_type': table_type,
                                'table_type_code': table_data,
                                'start_date': partition_start,
                                'end_date': partition_end,
                                'row_count': row_count,
                                'from_timestamp': from_date,
                                'to_timestamp': to_date
                            })

            conn.close()

        except Exception as e:
            raise Exception(f"파티션 탐색 오류: {str(e)}")

        return partitions

    def get_partition_info(self, partition_name: str, is_target: bool = False) -> dict[str, Any]:
        """특정 파티션 정보 조회

        Args:
            partition_name: 조회할 파티션 이름
            is_target: True이면 대상 DB 구성으로 연결
        """
        try:
            conn = self._create_connection(is_target=is_target)

            with conn.cursor() as cur:
                # 파티션 정보 조회
                cur.execute(
                    """
                    SELECT
                        table_name,
                        from_date,
                        to_date,
                        use_flag
                    FROM partition_table_info
                    WHERE table_name = %s
                """,
                    (partition_name,),
                )

                row = cur.fetchone()
                if not row:
                    return None

                table_name, from_date, to_date, use_flag = row

                # 테이블 정보
                info = {
                    "table_name": table_name,
                    "from_date": self._timestamp_to_date(from_date),
                    "to_date": self._timestamp_to_date(to_date),
                    "active": use_flag,
                    "exists": self._check_table_exists(cur, table_name),
                }

                if info["exists"]:
                    # 행 수 조회
                    info["row_count"] = self._get_row_count(cur, table_name)

                    # 컬럼 정보 조회
                    cur.execute(
                        """
                        SELECT column_name, data_type
                        FROM information_schema.columns
                        WHERE table_name = %s
                        ORDER BY ordinal_position
                    """,
                        (table_name,),
                    )

                    info["columns"] = [{"name": col[0], "type": col[1]} for col in cur.fetchall()]

            conn.close()
            return info

        except Exception as e:
            raise Exception(f"파티션 정보 조회 오류: {str(e)}")

    def verify_partition_structure(self, source_partition: str, target_partition: str) -> bool:
        """소스와 대상 파티션 구조 비교"""
        try:
            source_info = self.get_partition_info(source_partition)

            # 대상 정보 조회 (대상 설정이 없으면 비교 불가)
            if not self.target_config:
                return False

            target_info = self.get_partition_info(target_partition, is_target=True)

            if not source_info or not target_info:
                return False

            # 컬럼 비교
            source_cols = {(c["name"], c["type"]) for c in source_info.get("columns", [])}
            target_cols = {(c["name"], c["type"]) for c in target_info.get("columns", [])}

            return source_cols == target_cols

        except Exception:
            return False

    def _create_connection(self, is_target: bool = False) -> psycopg.Connection:
        """데이터베이스 연결 생성"""
        if is_target and self.target_config:
            config = self.target_config
        else:
            config = self.connection_config

        conn_params = {
            "host": config["host"],
            "port": config["port"],
            "dbname": config["database"],
            "user": config["username"],
            "password": config["password"],
        }

        if config.get("ssl"):
            conn_params["sslmode"] = "require"

        return psycopg.connect(**conn_params)

    def _check_table_exists(self, cursor, table_name: str) -> bool:
        """테이블 존재 여부 확인"""
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public'
                AND table_name = %s
            )
        """,
            (table_name,),
        )
        return cursor.fetchone()[0]

    def _get_row_count(self, cursor, table_name: str) -> int:
        """테이블 행 수 조회"""
        try:
            cursor.execute(sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table_name)))
            return cursor.fetchone()[0]
        except (psycopg.DatabaseError, psycopg.OperationalError):
            return 0

    def _date_to_timestamp(self, d: date) -> int:
        """날짜를 밀리초 타임스탬프로 변환"""
        dt = datetime.combine(d, datetime.min.time())
        return int(dt.timestamp() * 1000)

    def _timestamp_to_date(self, ts: int) -> date:
        """밀리초 타임스탬프를 날짜로 변환"""
        return datetime.fromtimestamp(ts / 1000).date()
