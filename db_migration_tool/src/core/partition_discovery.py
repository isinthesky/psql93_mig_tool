"""
파티션 테이블 탐색 및 분석
"""

from datetime import date, datetime
from typing import Any

import psycopg
from psycopg import sql


class PartitionDiscovery:
    """파티션 테이블 탐색 클래스"""

    def __init__(self, connection_config: dict[str, Any]):
        self.connection_config = connection_config

    def discover_partitions(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """날짜 범위에 해당하는 파티션 탐색"""
        partitions = []

        try:
            # 연결 생성
            conn = self._create_connection()

            with conn.cursor() as cur:
                # partition_table_info 테이블에서 날짜 범위에 해당하는 파티션 조회
                cur.execute(
                    """
                    SELECT
                        table_name,
                        table_data,
                        from_date,
                        to_date,
                        use_flag
                    FROM partition_table_info
                    WHERE table_data = 'PH'  -- point_history 타입
                    AND use_flag = true
                    AND from_date <= %s
                    AND to_date >= %s
                    ORDER BY from_date
                """,
                    (self._date_to_timestamp(end_date), self._date_to_timestamp(start_date)),
                )

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

                            partitions.append(
                                {
                                    "table_name": table_name,
                                    "start_date": partition_start,
                                    "end_date": partition_end,
                                    "row_count": row_count,
                                    "from_timestamp": from_date,
                                    "to_timestamp": to_date,
                                }
                            )

            conn.close()

        except Exception as e:
            raise Exception(f"파티션 탐색 오류: {str(e)}")

        return partitions

    def get_partition_info(self, partition_name: str) -> dict[str, Any]:
        """특정 파티션 정보 조회"""
        try:
            conn = self._create_connection()

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

            # 대상 연결로 전환
            target_conn = self._create_connection(is_target=True)
            self.connection_config = self.target_config  # 임시 전환
            target_info = self.get_partition_info(target_partition)
            self.connection_config = self.source_config  # 복원
            target_conn.close()

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
