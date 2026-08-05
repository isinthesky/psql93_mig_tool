"""
파티션 테이블 탐색 및 분석
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import psycopg

from .archive_manifest import ScanCancelled
from .table_types import DEFAULT_TABLE_TYPE, TableType, infer_partition_range

# 커넥션 수립 대기 상한(초).
CONNECT_TIMEOUT_SECONDS = 10


class PartitionDiscovery:
    """파티션 테이블 탐색 클래스"""

    def __init__(
        self, connection_config: dict[str, Any], target_config: dict[str, Any] | None = None
    ):
        self.source_config = connection_config
        self.target_config = target_config
        self.connection_config = connection_config  # 하위 호환성을 위해 유지

    def discover_partitions(
        self,
        start_date: date,
        end_date: date,
        table_types: list[TableType] | None = None,
        *,
        should_stop: Callable[[], bool] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        on_connection: Callable[[Any], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        날짜 범위에 해당하는 파티션 탐색

        Args:
            start_date: 시작 날짜
            end_date: 종료 날짜
            table_types: 탐색할 테이블 타입 리스트 (기본값: [DEFAULT_TABLE_TYPE])
            should_stop: 중간에 멈춰야 하는지 묻는 콜백. 파티션이 수백 개면
                루프가 길어지므로 이 훅이 없으면 창을 닫아도 워커가 안 멈춘다.
            on_progress: (done, total) 진행 상황 콜백. total은 아직 모르는
                시점이 있어 0으로 올 수 있다.
            on_connection: 커넥션이 열리면 넘겨준다. 플래그는 쿼리 사이에서만
                읽히므로, 첫 조회가 오래 걸리면 호출자가 이 커넥션에
                `cancel()`을 걸 수 있어야 실제로 중단된다.

        Returns:
            파티션 정보 리스트

        Raises:
            ScanCancelled: should_stop이 True를 돌려준 경우
        """

        def _check_cancelled() -> None:
            if should_stop is not None and should_stop():
                raise ScanCancelled("파티션 탐색이 취소되었습니다")

        partitions = []

        if table_types is None:
            table_types = [DEFAULT_TABLE_TYPE]

        if not table_types:
            raise ValueError("최소 1개의 테이블 타입을 지정해야 합니다.")

        table_type_codes = [tt.value for tt in table_types]
        selected_by_code = {tt.value: tt for tt in table_types}
        selected_names = {tt.table_name: tt for tt in table_types}
        seen_names: set[str] = set()

        conn = None
        try:
            conn = self._create_connection()
            if on_connection is not None:
                on_connection(conn)

            with conn.cursor() as cur:
                # 1) partition_table_info 기반 조회 (기본)
                placeholders = ", ".join(["%s"] * len(table_type_codes))
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
                    self._date_to_timestamp(start_date),
                )

                cur.execute(query, params)

                # 진행률은 두 루프에 걸쳐 한 번만 센다. 루프마다 1부터 다시 세면
                # 화면의 숫자가 되돌아가 사용자가 멈춘 줄 안다.
                scanned = 0
                total_estimate = 0

                def _report(step: int = 1) -> None:
                    nonlocal scanned
                    scanned += step
                    if on_progress is not None:
                        on_progress(scanned, total_estimate)

                primary_rows = cur.fetchall()
                total_estimate = len(primary_rows)
                for row in primary_rows:
                    _check_cancelled()
                    _report()

                    table_name, table_data, from_date, to_date, use_flag = row

                    partition_start = self._timestamp_to_date(from_date)
                    partition_end = self._timestamp_to_date(to_date)

                    if partition_start <= end_date and partition_end >= start_date:
                        if self._check_table_exists(cur, table_name):
                            table_type = selected_by_code.get(table_data)
                            if not table_type:
                                parent_table = "_".join(table_name.split("_")[:-1])
                                table_type = selected_names.get(parent_table)
                            if not table_type:
                                continue

                            row_count = self._estimate_row_count(cur, table_name)
                            partitions.append(
                                {
                                    "table_name": table_name,
                                    "table_type": table_type,
                                    "table_type_code": table_type.value,
                                    "start_date": partition_start,
                                    "end_date": partition_end,
                                    "row_count": row_count,
                                    # 추정치임을 UI가 정직하게 표기하도록 알린다.
                                    "row_count_estimated": True,
                                    "from_timestamp": from_date,
                                    "to_timestamp": to_date,
                                }
                            )
                            seen_names.add(table_name)

                # 2) fallback: 물리 테이블 패턴 기반 조회
                # partition_table_info가 비어 있거나 코드가 예상과 다를 때를 대비
                for table_type in table_types:
                    prefix = f"{table_type.table_name}_%"
                    cur.execute(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name LIKE %s
                        ORDER BY table_name
                        """,
                        (prefix,),
                    )
                    physical_names = [row[0] for row in cur.fetchall()]
                    # 총계는 여기서 늘어난다(미리 알 수 없다). 진행 수치는 줄지 않는다.
                    total_estimate += len(physical_names)
                    for table_name in physical_names:
                        _check_cancelled()
                        _report()

                        if table_name in seen_names:
                            continue

                        from_ts, to_ts = infer_partition_range(table_type, table_name)
                        if from_ts is None or to_ts is None:
                            continue

                        partition_start = self._timestamp_to_date(from_ts)
                        partition_end = self._timestamp_to_date(to_ts)
                        if partition_start > end_date or partition_end < start_date:
                            continue

                        row_count = self._estimate_row_count(cur, table_name)
                        partitions.append(
                            {
                                "table_name": table_name,
                                "table_type": table_type,
                                "table_type_code": table_type.value,
                                "start_date": partition_start,
                                "end_date": partition_end,
                                "row_count": row_count,
                                # 추정치임을 UI가 정직하게 표기하도록 알린다.
                                "row_count_estimated": True,
                                "from_timestamp": from_ts,
                                "to_timestamp": to_ts,
                            }
                        )
                        seen_names.add(table_name)

        except ScanCancelled:
            # 취소는 오류가 아니다. 여기서 재포장하면 UI가 '탐색 실패'로 알린다.
            raise
        except Exception as e:
            raise Exception(f"파티션 탐색 오류: {str(e)}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        partitions.sort(key=lambda p: (p["table_type_code"], p["from_timestamp"] or 0))
        return partitions

    def get_partition_info(
        self, partition_name: str, is_target: bool = False
    ) -> dict[str, Any] | None:
        """특정 파티션 정보 조회

        Args:
            partition_name: 조회할 파티션 이름
            is_target: True이면 대상 DB 구성으로 연결
        """
        conn = None
        try:
            conn = self._create_connection(is_target=is_target)

            with conn.cursor() as cur:
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
                info = {
                    "table_name": table_name,
                    "from_date": self._timestamp_to_date(from_date),
                    "to_date": self._timestamp_to_date(to_date),
                    "active": use_flag,
                    "exists": self._check_table_exists(cur, table_name),
                }

                if info["exists"]:
                    info["row_count"] = self._estimate_row_count(cur, table_name)
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

            return info

        except Exception as e:
            raise Exception(f"파티션 정보 조회 오류: {str(e)}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def verify_partition_structure(self, source_partition: str, target_partition: str) -> bool:
        """소스와 대상 파티션 구조 비교"""
        try:
            source_info = self.get_partition_info(source_partition)
            if not self.target_config:
                return False

            target_info = self.get_partition_info(target_partition, is_target=True)
            if not source_info or not target_info:
                return False

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
            "host": config.get("host", "localhost"),
            "port": config.get("port", 5432),
            "dbname": config.get("database", ""),
            "user": config.get("username", ""),
            "password": config.get("password", ""),
            # 상한이 없으면 방화벽이 패킷을 버릴 때 OS 타임아웃(수십 초)까지
            # 붙잡혀 있고, 그동안 취소 요청조차 읽지 못한다.
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
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
        row = cursor.fetchone()
        return bool(row and row[0])

    def _estimate_row_count(self, cursor, table_name: str) -> int:
        """테이블 행 수 **추정치**를 얻는다.

        예전에는 파티션마다 `SELECT COUNT(*)`를 돌렸다. 이 도구가 다루는
        파티션은 수백만 행이고 한 번에 수십~수백 개를 훑으므로, 전수 스캔이
        탐색 시간을 지배했다(파티션당 수 초 × 개수). 그 숫자는 목록·툴팁·
        합계 라벨에만 쓰이고 건너뛰기나 완료 판정에는 쓰이지 않으므로,
        플래너 통계(`pg_class.reltuples`)로 충분하다.

        정확한 값이 필요한 곳은 각자 따로 센다:
        - 실행 전 빈 파티션 판정: `CopyMigrationWorker._resolve_total_rows()`
        - 사후 검증: `RowCountVerifyWorker` (`COUNT(*)` 유지)

        ANALYZE 전이면 통계가 없다. PG14+는 `-1`, 그 이전은 `0`을 준다.
        둘 다 '모른다'는 뜻이므로 0으로 눕힌다 — 음수가 합계를 깎으면
        화면의 총합이 실제보다 작아진다.
        """
        try:
            cursor.execute(
                """
                SELECT c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relname = %s
                  AND n.nspname = 'public'
                  AND c.relkind IN ('r', 'p')
                """,
                (table_name,),
            )
            row = cursor.fetchone()
            if not row or row[0] is None:
                return 0
            return max(0, int(row[0]))
        except (psycopg.DatabaseError, psycopg.OperationalError):
            return 0

    def _date_to_timestamp(self, d: date) -> int:
        """날짜를 밀리초 타임스탬프로 변환"""
        dt = datetime.combine(d, datetime.min.time())
        return int(dt.timestamp() * 1000)

    def _timestamp_to_date(self, ts: int) -> date:
        """밀리초 타임스탬프를 날짜로 변환"""
        return datetime.fromtimestamp(ts / 1000).date()
