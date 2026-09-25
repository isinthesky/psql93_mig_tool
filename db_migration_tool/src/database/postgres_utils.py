"""
PostgreSQL 최적화 유틸리티
- 세션 레벨 성능 파라미터 설정
- COPY 명령 권한 확인
- 연결 풀 관리
- 버전 감지 및 버전별 최적화
"""

import logging
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from functools import cache
from io import StringIO
from typing import Any

import psycopg
import psycopg2
import psycopg2.errorcodes
import psycopg2.errors

from src.database.version_info import PgVersionFamily, PgVersionInfo, parse_version_string
from src.database.version_params import get_params_for_version
from src.database.version_sql import get_sql_for_version

logger = logging.getLogger(__name__)

# ── SQL 경계 공용 헬퍼 (감사 H-04 / M-01 / M-14) ─────────────────────────
# 이 도구가 다루는 relation은 전부 public에 있다. 연결에도 search_path=public을 넣지만
# (connection_params), 호출자·세션이 search_path를 바꿔도 의도한 객체만 건드리도록
# relation은 문장 안에서 직접 schema를 한정한다.
PUBLIC_SCHEMA = "public"

# libpq PQtransactionStatus. psycopg2 `conn.info.transaction_status`(int)와
# psycopg3 `pq.TransactionStatus`(IntEnum)가 같은 값을 쓴다.
_TRANSACTION_STATUS_INERROR = 3
_TRANSACTION_STATUS_UNKNOWN = 4  # 연결이 끊긴 상태


def quote_ident(name: str) -> str:
    """식별자를 큰따옴표로 감싼다(내부 큰따옴표는 두 번). 드라이버 무관.

    psycopg2 연결과 psycopg3 연결이 섞여 들어오므로 드라이버의 `sql.Identifier`
    대신 PostgreSQL 규칙을 직접 쓴다. quote하면 대소문자를 접지 않으므로 카탈로그에
    저장된 이름 그대로를 가리킨다.
    """
    if not isinstance(name, str) or not name or "\x00" in name:
        raise ValueError(f"잘못된 식별자: {name!r}")
    return '"' + name.replace('"', '""') + '"'


def qualified_name(name: str, schema: str = PUBLIC_SCHEMA) -> str:
    """`"schema"."name"` — search_path와 무관하게 한 relation만 가리킨다."""
    return f"{quote_ident(schema)}.{quote_ident(name)}"


@cache
def _psycopg2_sqlstate_by_class() -> dict[type, str]:
    mapping: dict[type, str] = {}
    for attr in dir(psycopg2.errorcodes):
        code = getattr(psycopg2.errorcodes, attr)
        if not (isinstance(code, str) and len(code) == 5 and attr.isupper()):
            continue
        try:
            mapping.setdefault(psycopg2.errors.lookup(code), code)
        except KeyError:
            continue
    return mapping


def is_db_error(exc: BaseException) -> bool:
    """psycopg(3)·psycopg2 어느 쪽이든 드라이버 예외인가."""
    return isinstance(exc, (psycopg.Error, psycopg2.Error))


def sqlstate_of(exc: BaseException) -> str | None:
    """예외의 SQLSTATE. psycopg3는 `sqlstate`, psycopg2는 `pgcode`(없으면 클래스로 역조회)."""
    if isinstance(exc, psycopg.Error):
        return exc.sqlstate
    if isinstance(exc, psycopg2.Error):
        if exc.pgcode:
            return str(exc.pgcode)
        for klass in type(exc).__mro__:
            code = _psycopg2_sqlstate_by_class().get(klass)
            if code:
                return code
    return None


def _uses_autocommit(conn: Any) -> bool:
    return getattr(conn, "autocommit", False) is True


def _transaction_status(conn: Any) -> int | None:
    """libpq 트랜잭션 상태(psycopg2·psycopg3 공통 `conn.info.transaction_status`). 모르면 None."""
    status = getattr(getattr(conn, "info", None), "transaction_status", None)
    if isinstance(status, int):  # psycopg3 IntEnum도 int다
        return int(status)
    return None


def transaction_is_aborted(conn: Any) -> bool:
    """연결의 트랜잭션이 오류로 중단(aborted)된 상태인가."""
    return _transaction_status(conn) == _TRANSACTION_STATUS_INERROR


def connection_unusable(conn: Any) -> bool:
    """트랜잭션이 중단된 채이거나 연결이 닫혔거나 상태를 알 수 없는가(복구 실패 판정용)."""
    if conn is None:
        return False
    if transaction_is_aborted(conn):
        return True
    closed = getattr(conn, "closed", False)
    if isinstance(closed, (bool, int)) and closed:
        return True
    return _transaction_status(conn) == _TRANSACTION_STATUS_UNKNOWN


@contextmanager
def isolated_statement(conn: Any, cursor: Any, name: str = "dbmig_sp") -> Iterator[None]:
    """블록 안 문장을 SAVEPOINT로 감싼다.

    PostgreSQL은 트랜잭션 안에서 한 문장이 실패하면 트랜잭션 전체를 중단시키고,
    이후 문장을 전부 `in_failed_sql_transaction`으로 거부한다. 실패해도 되는 조회·DDL은
    이 블록으로 감싸야 실패를 잡은 뒤 같은 트랜잭션을 이어 쓸 수 있다.

    - 실패하면 `ROLLBACK TO SAVEPOINT`로 그 문장만 되돌리고 **원래 예외를 다시 던진다**
      (무시할지는 호출자가 SQLSTATE로 정한다).
    - `ROLLBACK TO`까지 실패하면(연결 끊김 등) 원래 예외를 그대로 던진다 — 복구된 척하지 않는다.
    - autocommit 연결은 문장마다 트랜잭션이 끝나므로 SAVEPOINT 없이 그대로 실행한다.
    """
    if _uses_autocommit(conn):
        yield
        return

    savepoint = quote_ident(name)
    cursor.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception as exc:
        try:
            cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            cursor.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception as rollback_exc:
            logger.error("SAVEPOINT 복구 실패(원래 오류를 전달): %s", rollback_exc)
            raise exc from rollback_exc
        raise
    cursor.execute(f"RELEASE SAVEPOINT {savepoint}")


def run_optional_statement(
    conn: Any,
    cursor: Any,
    statement: str,
    *,
    ignorable: Collection[str],
    label: str,
) -> bool:
    """실패해도 되는 문장을 SAVEPOINT로 격리해 실행한다.

    Returns:
        True면 실행됨, False면 `ignorable` SQLSTATE로 실패해 건너뜀(트랜잭션은 계속 사용 가능).

    Raises:
        `ignorable`에 없는 오류(취소 57014, 연결 오류 등)는 그대로 던진다. 삼키면
        사용자의 취소가 먹지 않거나 망가진 연결로 계속 진행하게 된다.
    """
    try:
        with isolated_statement(conn, cursor):
            cursor.execute(statement)
    except Exception as exc:
        state = sqlstate_of(exc)
        if state is not None and state in ignorable:
            logger.warning("선택 DDL 건너뜀(%s, SQLSTATE %s): %s", label, state, exc)
            return False
        raise
    return True


def commit_or_raise(conn: Any) -> None:
    """트랜잭션이 중단 상태면 커밋하지 않고 예외를 던진다.

    중단된 트랜잭션에 COMMIT을 보내면 서버는 오류 없이 ROLLBACK하고 드라이버도
    예외를 던지지 않는다. 그대로 두면 사라진 DDL을 성공으로 보고한다.
    """
    if transaction_is_aborted(conn):
        raise RuntimeError(
            "트랜잭션이 오류로 중단된 상태라 커밋할 수 없습니다(이미 실패한 문장이 있음)"
        )
    conn.commit()


def rollback_quietly(conn: Any) -> None:
    """실패 경로 정리용 rollback. rollback 자체의 오류는 원래 오류를 가리지 않게 로그만 남긴다."""
    try:
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 - 원래 예외를 보존하는 것이 우선
        logger.warning("rollback 실패(무시): %s", exc)


class PostgresOptimizer:
    """PostgreSQL 성능 최적화 유틸리티"""

    @staticmethod
    def check_copy_permissions(
        connection,
        check_write: bool = True,
        version_info: PgVersionInfo | None = None,
    ) -> tuple[bool, str]:
        """COPY 명령 실행 권한 확인

        Args:
            connection: psycopg2 연결 객체
            check_write: True면 COPY FROM 권한, False면 COPY TO 권한 확인
            version_info: 연결 대상의 PostgreSQL 버전 정보

        Returns:
            (권한 여부, 오류 메시지)
        """
        version_family = version_info.family if version_info else PgVersionFamily.UNKNOWN

        try:
            with connection.cursor() as cursor:
                # 현재 사용자 확인
                cursor.execute("SELECT current_user")
                current_user = cursor.fetchone()[0]

                # 슈퍼유저 확인
                cursor.execute(
                    """
                    SELECT rolsuper
                    FROM pg_roles
                    WHERE rolname = %s
                """,
                    (current_user,),
                )
                is_superuser = cursor.fetchone()[0]

                if is_superuser:
                    return True, ""

                # 16에서는 서버 파일 역할을 확인, 9.3/UNKNOWN은 바로 프로빙
                required_role = "pg_write_server_files" if check_write else "pg_read_server_files"
                if version_family == PgVersionFamily.PG_16:
                    # NOTE: pg_has_role()만으로 역할 보유 여부를 확인한다.
                    # (이 쿼리는 FROM 절이 없으므로 rolsuper 같은 컬럼을 참조하면 오류가 난다)
                    cursor.execute(
                        "SELECT pg_has_role(current_user, %s, 'MEMBER')",
                        (required_role,),
                    )
                    has_role = bool(cursor.fetchone()[0])
                    if has_role:
                        return True, ""

                # COPY 권한 직접 테스트 (임시 테이블 사용)
                success, probe_error = PostgresOptimizer._probe_copy_privilege(
                    connection, check_write
                )
                if success:
                    return True, ""

                # 실패 시 오류 메시지 구성
                if version_family == PgVersionFamily.PG_16:
                    error_msg = (
                        f"COPY 권한이 없습니다.\n"
                        f"현재 사용자: {current_user}\n"
                        f"필요한 권한: {required_role} 또는 SUPERUSER\n"
                        f"DBA에게 다음 명령 실행을 요청하세요:\n"
                        f"GRANT {required_role} TO {current_user};"
                    )
                else:
                    error_msg = (
                        f"COPY 권한이 없습니다.\n"
                        f"현재 사용자: {current_user}\n"
                        "슈퍼유저 권한이 필요합니다."
                    )

                if probe_error:
                    error_msg = f"{error_msg}\n오류: {probe_error}"
                return False, error_msg

        except Exception as e:
            return False, f"권한 확인 중 오류 발생: {str(e)}"

    @staticmethod
    def check_connection_quick(config: dict[str, Any]) -> tuple[bool, str]:
        """빠른 연결 확인 (타임아웃 5초)

        Args:
            config: 데이터베이스 연결 설정

        Returns:
            (연결 성공 여부, 상태 메시지 또는 오류 메시지)
        """
        import psycopg

        from src.database.connection_params import ConnectionConfigError, connect_psycopg

        conn = None
        try:
            # 공용 빌더: TLS 검증·search_path 적용. 빠른 확인이므로 기본 5초 타임아웃.
            conn = connect_psycopg(config, connect_timeout=5)

            # 간단한 쿼리로 연결 확인
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

            return True, "연결 성공"

        except ConnectionConfigError as e:
            return False, f"연결 설정 오류: {e}"
        except psycopg.OperationalError as e:
            error_str = str(e)

            # 구체적인 오류 메시지 분류
            if (
                "could not connect to server" in error_str
                or "Name or service not known" in error_str
            ):
                return False, f"호스트를 찾을 수 없음: {config['host']}"
            elif (
                "password authentication failed" in error_str
                or "authentication failed" in error_str
            ):
                return False, f"인증 실패: 사용자 {config['username']}"
            elif "timeout expired" in error_str:
                return False, "네트워크 타임아웃"
            elif "permission denied" in error_str:
                return False, "권한 부족"
            elif "database" in error_str and "does not exist" in error_str:
                return False, f"데이터베이스 없음: {config['database']}"
            else:
                return False, f"연결 실패: {error_str}"

        except Exception as e:
            return False, f"예상치 못한 오류: {str(e)}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def estimate_table_size(
        connection,
        table_name: str,
        version_info: PgVersionInfo | None = None,
    ) -> dict[str, Any]:
        """테이블 크기 추정"""
        effective_version = version_info or PostgresOptimizer.detect_version(connection)
        try:
            with connection.cursor() as cursor:
                # 먼저 테이블 존재 여부 확인
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

                if not cursor.fetchone()[0]:
                    logger.warning(f"테이블 {table_name}이(가) 존재하지 않습니다")
                    return {
                        "row_count": 0,
                        "total_size_bytes": 0,
                        "total_size_mb": 0,
                        "avg_row_size_bytes": 0,
                        "exists": False,
                    }

                # 버전별 테이블 크기 추정 쿼리 사용. 크기도 public에서 고른 oid로 잰다
                # (`pg_table_size('name')`은 regclass 변환이 search_path를 따른다 — H-04).
                estimate_query = get_sql_for_version(effective_version, "estimate_size")
                cursor.execute(estimate_query, (table_name,))
                estimate_row = cursor.fetchone()
                if estimate_row is None:
                    # 존재 확인(information_schema)은 통과했는데 일반/파티션 테이블 행이 없다
                    # (뷰 등). '없음'이 아니라 '확인 불가'로 올린다.
                    raise LookupError(f"pg_class에서 public.{table_name}을(를) 찾지 못했습니다")
                row_count, total_size = estimate_row

                # 평균 행 크기
                avg_row_size = total_size / row_count if row_count > 0 else 0

                return {
                    "row_count": row_count,
                    "total_size_bytes": total_size,
                    "total_size_mb": total_size / (1024 * 1024),
                    "avg_row_size_bytes": avg_row_size,
                    "exists": True,
                }

        except Exception as e:
            # 트랜잭션 오류 시 롤백
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            logger.error(f"테이블 크기 추정 실패: {e}")
            # 여기서 exists=False를 돌려주면 안 된다.
            # 호출부(COPY 워커)는 그것을 '소스 테이블이 없다'로 읽고 체크포인트를
            # 완료로 마킹한 뒤 건너뛴다. 실제로는 '확인하지 못했다'일 뿐이라,
            # 멀쩡한 파티션이 조용히 누락된다.
            # 확인 불가는 존재한다고 보고 진행시킨다. 정말 없으면 이어지는
            # COPY가 크게 실패하므로 사용자가 알 수 있다.
            return {
                "row_count": 0,
                "total_size_bytes": 0,
                "total_size_mb": 0,
                "avg_row_size_bytes": 0,
                "exists": True,
                "lookup_failed": True,
                "error": str(e),
            }

    @staticmethod
    def detect_version(connection) -> PgVersionInfo:
        """PostgreSQL 버전 감지

        Args:
            connection: psycopg2 연결 객체

        Returns:
            PgVersionInfo: 감지된 버전 정보
        """
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT version()")
                version_str = cursor.fetchone()[0]
                return parse_version_string(version_str)
        except Exception as e:
            logger.error(f"버전 감지 실패: {e}")
            return PgVersionInfo(0, 0, "unknown", PgVersionFamily.UNKNOWN)

    @staticmethod
    def resolve_effective_version(connection, compat_mode: str) -> PgVersionInfo:
        """호환 모드를 고려한 유효 버전 결정

        Args:
            connection: psycopg2 연결 객체
            compat_mode: 호환 모드 ("auto", "9.3", "16")

        Returns:
            PgVersionInfo: 유효 버전 정보
        """
        detected = PostgresOptimizer.detect_version(connection)

        if compat_mode == "9.3":
            return PgVersionInfo(
                9, 3, f"forced:9.3 (실제: {detected.full_version})", PgVersionFamily.PG_9_3
            )
        if compat_mode == "16":
            return PgVersionInfo(
                16, 0, f"forced:16 (실제: {detected.full_version})", PgVersionFamily.PG_16
            )

        # auto: 감지된 버전 사용
        return detected

    @staticmethod
    def apply_version_params(connection, version_info: PgVersionInfo) -> None:
        """버전별 세션 파라미터 적용

        Args:
            connection: psycopg2 연결 객체
            version_info: PostgreSQL 버전 정보
        """
        params = get_params_for_version(version_info)
        PostgresOptimizer.apply_params(connection, params)

    @staticmethod
    def apply_params(connection, params: dict[str, str]) -> None:
        """세션 파라미터 적용 (실패 시 무시)

        Args:
            connection: psycopg2 연결 객체
            params: 파라미터 딕셔너리
        """
        try:
            with connection.cursor() as cursor:
                for param, value in params.items():
                    try:
                        # 실패한 SET만 SAVEPOINT로 되돌린다. 트랜잭션 전체를 rollback하면
                        # 앞서 성공한 SET까지 취소된다(트랜잭션 안의 SET은 ROLLBACK 대상).
                        with isolated_statement(connection, cursor, name="dbmig_set_param"):
                            cursor.execute(f"SET {param} = %s", (value,))
                        logger.info(f"PostgreSQL 파라미터 설정: {param} = {value}")
                    except Exception as e:
                        if not is_db_error(e) or connection_unusable(connection):
                            raise
                        # 지원하지 않는 파라미터는 무시하고 계속
                        logger.warning(f"파라미터 설정 실패 (무시됨): {param} = {value}, 오류: {e}")
                        continue

                commit_or_raise(connection)
                logger.info("PostgreSQL 버전별 파라미터 적용 완료")

        except Exception as e:
            rollback_quietly(connection)
            logger.error(f"파라미터 적용 실패: {e}")

    @staticmethod
    def _probe_copy_privilege(connection, check_write: bool) -> tuple[bool, str]:
        """COPY 권한을 직접 프로빙 (임시 테이블 사용)"""
        try:
            # 세션 임시 스키마로 한정한다. 이름만 쓰면 search_path에 따라 같은 이름의
            # 영구 테이블을 가리킬 수 있다(H-04).
            probe = "pg_temp.dbmig_copy_probe"
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE TEMP TABLE {probe} (id int)")
                if check_write:
                    cursor.copy_expert(
                        f"COPY {probe} FROM STDIN WITH (FORMAT CSV)", StringIO("1\n")
                    )
                else:
                    cursor.copy_expert(f"COPY {probe} TO STDOUT WITH (FORMAT CSV)", StringIO())
                cursor.execute(f"DROP TABLE {probe}")
            connection.commit()
            return True, ""
        except psycopg2.Error as e:
            try:
                connection.rollback()
            except psycopg2.Error:
                pass
            return False, str(e)
