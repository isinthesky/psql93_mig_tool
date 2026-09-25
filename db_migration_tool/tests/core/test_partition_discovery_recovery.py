"""파티션 탐색: 행 수 추정 실패 후 트랜잭션 복구(감사 M-01)와 schema 한정(감사 H-04).

예전 `_estimate_row_count`는 추정 조회 실패를 잡아 0을 돌려주기만 하고 트랜잭션을 복구하지
않았다. 탐색 커넥션은 autocommit이 아니므로 첫 실패 뒤 같은 트랜잭션의 모든 조회가
`in_failed_sql_transaction`으로 거부되고, 탐색 전체가 '파티션 탐색 오류'로 끝났다.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from src.core.partition_discovery import PartitionDiscovery
from src.core.table_types import TableType
from tests.database.fake_pg import INERROR, FakePgConnection

# 2026-05-04, 2026-05-05 (로컬 자정 기준 ms) — 날짜 필터만 통과하면 된다.
_DAY_MS = 86_400_000


def _ms(d: date) -> int:
    from datetime import datetime

    return int(datetime.combine(d, datetime.min.time()).timestamp() * 1000)


P1 = ("point_history_260504", "PH", _ms(date(2026, 5, 4)), _ms(date(2026, 5, 4)) + _DAY_MS - 1)
P2 = ("point_history_260505", "PH", _ms(date(2026, 5, 5)), _ms(date(2026, 5, 5)) + _DAY_MS - 1)


def _source(*, failing_estimate: set[str], exc_name: str = "LockNotAvailable") -> FakePgConnection:
    def responder(sql_text, params):
        if "partition_table_info" in sql_text and "table_data IN" in sql_text:
            return [(name, code, f, t, True) for name, code, f, t in (P1, P2)]
        if "information_schema.tables" in sql_text and "EXISTS" in sql_text:
            return [(True,)]
        if "reltuples" in sql_text:
            if params and params[0] in failing_estimate:
                raise conn.error(exc_name, "canceling statement due to lock timeout")
            return [(1234,)]
        if "information_schema.tables" in sql_text:
            return []  # fallback 물리 테이블 조회: 추가 없음
        return []

    conn = FakePgConnection(responder, flavor="psycopg")
    return conn


def _discover(conn: FakePgConnection) -> list[dict]:
    discovery = PartitionDiscovery({"host": "h"})
    with patch.object(PartitionDiscovery, "_create_connection", return_value=conn):
        return discovery.discover_partitions(
            date(2026, 5, 4), date(2026, 5, 5), [TableType.POINT_HISTORY]
        )


class TestEstimateFailureRecovery:
    @pytest.mark.parametrize("exc_name", ["LockNotAvailable", "InsufficientPrivilege"])
    def test_later_partitions_are_still_discovered(self, exc_name):
        conn = _source(failing_estimate={"point_history_260504"}, exc_name=exc_name)

        partitions = _discover(conn)

        names = [p["table_name"] for p in partitions]
        assert names == ["point_history_260504", "point_history_260505"]
        by_name = {p["table_name"]: p for p in partitions}
        # 실패한 추정은 '모름' = 0, 추정치 표시는 유지된다.
        assert by_name["point_history_260504"]["row_count"] == 0
        assert by_name["point_history_260504"]["row_count_estimated"] is True
        assert by_name["point_history_260505"]["row_count"] == 1234

    def test_recovery_uses_savepoint_not_whole_rollback(self):
        """전체 ROLLBACK이 아니라 SAVEPOINT로 그 조회만 되돌린다."""
        conn = _source(failing_estimate={"point_history_260504"})

        _discover(conn)

        assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in conn.statements)
        assert conn.rollbacks == 0
        assert conn.status != INERROR

    def test_estimate_directly_leaves_cursor_usable(self):
        conn = _source(failing_estimate={"t_bad"})
        discovery = PartitionDiscovery({})
        with conn.cursor() as cur:
            assert discovery._estimate_row_count(cur, "t_bad") == 0
            assert discovery._check_table_exists(cur, "anything") is True

    def test_broken_connection_is_not_hidden(self):
        """SAVEPOINT 복구조차 안 되면(연결 끊김) 조용히 0으로 넘어가지 않는다."""
        conn = _source(failing_estimate={"point_history_260504"})
        conn.fail_rollback_to = True

        with pytest.raises(Exception, match="파티션 탐색 오류"):
            _discover(conn)


class TestDiscoverySchemaQualification:
    def test_partition_table_info_is_read_from_public(self):
        conn = _source(failing_estimate=set())
        _discover(conn)

        pti = [s for s in conn.statements if "partition_table_info" in s]
        assert pti, "partition_table_info 조회가 없습니다"
        for stmt in pti:
            assert "public.partition_table_info" in stmt, stmt

    def test_catalog_lookups_filter_public_schema(self):
        conn = _source(failing_estimate=set())
        _discover(conn)

        for stmt in conn.statements:
            if "information_schema.tables" in stmt:
                assert "table_schema = 'public'" in stmt, stmt
            if "pg_class" in stmt:
                assert "nspname = 'public'" in stmt, stmt
