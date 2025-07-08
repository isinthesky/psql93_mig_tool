"""
PostgreSQL 최적화 유틸리티
- 세션 레벨 성능 파라미터 설정
- COPY 명령 권한 확인
- 연결 풀 관리
"""
import psycopg2
from psycopg2 import sql
from typing import Dict, Any, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


class PostgresOptimizer:
    """PostgreSQL 성능 최적화 유틸리티"""
    
    # 대량 작업을 위한 최적화 파라미터 (세션 레벨에서 변경 가능한 것만)
    BULK_OPERATION_PARAMS = {
        'work_mem': '256MB',
        'maintenance_work_mem': '1GB',
        'synchronous_commit': 'off',
        # 'wal_buffers': '16MB',  # 서버 재시작 필요
        # 'checkpoint_segments': '32',  # PostgreSQL 9.5부터 제거됨
        # 'checkpoint_completion_target': '0.9'  # 서버 재시작 필요
    }
    
    @staticmethod
    def apply_bulk_operation_optimizations(connection) -> None:
        """대량 작업을 위한 세션 레벨 최적화 적용"""
        try:
            with connection.cursor() as cursor:
                for param, value in PostgresOptimizer.BULK_OPERATION_PARAMS.items():
                    try:
                        cursor.execute(f"SET {param} = %s", (value,))
                        logger.info(f"PostgreSQL 파라미터 설정: {param} = {value}")
                    except psycopg2.Error as e:
                        # 오류 발생 시 트랜잭션 롤백
                        connection.rollback()
                        logger.warning(f"파라미터 설정 실패 (무시됨): {param} = {value}, 오류: {e}")
                        continue
                
                connection.commit()
                logger.info("PostgreSQL 대량 작업 최적화 완료")
                
        except Exception as e:
            connection.rollback()
            logger.error(f"PostgreSQL 최적화 실패: {e}")
            # 최적화 실패는 치명적이지 않으므로 예외를 발생시키지 않음
    
    @staticmethod
    def check_copy_permissions(connection, check_write: bool = True) -> Tuple[bool, str]:
        """COPY 명령 실행 권한 확인
        
        Args:
            connection: psycopg2 연결 객체
            check_write: True면 COPY FROM 권한, False면 COPY TO 권한 확인
            
        Returns:
            (권한 여부, 오류 메시지)
        """
        try:
            with connection.cursor() as cursor:
                # 현재 사용자 확인
                cursor.execute("SELECT current_user")
                current_user = cursor.fetchone()[0]
                
                # 슈퍼유저 확인
                cursor.execute("""
                    SELECT rolsuper 
                    FROM pg_roles 
                    WHERE rolname = %s
                """, (current_user,))
                is_superuser = cursor.fetchone()[0]
                
                if is_superuser:
                    return True, ""
                
                # pg_read_server_files, pg_write_server_files 역할 확인
                if check_write:
                    required_role = 'pg_write_server_files'
                else:
                    required_role = 'pg_read_server_files'
                
                # PostgreSQL 11+ 에서만 이 역할들이 존재
                cursor.execute("""
                    SELECT 1 
                    FROM pg_roles r1
                    JOIN pg_auth_members m ON r1.oid = m.roleid
                    JOIN pg_roles r2 ON m.member = r2.oid
                    WHERE r1.rolname = %s AND r2.rolname = %s
                """, (required_role, current_user))
                
                has_role = cursor.fetchone() is not None
                
                if not has_role:
                    # COPY 권한 직접 테스트 (임시 테이블 사용)
                    try:
                        cursor.execute("CREATE TEMP TABLE copy_test (id int)")
                        if check_write:
                            cursor.execute("COPY copy_test FROM STDIN WITH (FORMAT CSV)")
                            cursor.copy_expert("COPY copy_test FROM STDIN", "1\n")
                        else:
                            cursor.execute("COPY copy_test TO STDOUT WITH (FORMAT CSV)")
                        cursor.execute("DROP TABLE copy_test")
                        return True, ""
                    except psycopg2.Error:
                        error_msg = (
                            f"COPY 권한이 없습니다.\n"
                            f"현재 사용자: {current_user}\n"
                            f"필요한 권한: {required_role} 또는 SUPERUSER\n"
                            f"DBA에게 다음 명령 실행을 요청하세요:\n"
                            f"GRANT {required_role} TO {current_user};"
                        )
                        return False, error_msg
                
                return True, ""
                
        except Exception as e:
            return False, f"권한 확인 중 오류 발생: {str(e)}"
    
    @staticmethod
    def check_connection_quick(config: Dict[str, Any]) -> Tuple[bool, str]:
        """빠른 연결 확인 (타임아웃 5초)
        
        Args:
            config: 데이터베이스 연결 설정
            
        Returns:
            (연결 성공 여부, 상태 메시지 또는 오류 메시지)
        """
        import psycopg
        
        # 연결 파라미터 준비
        conn_params = {
            'host': config['host'],
            'port': config['port'],
            'dbname': config['database'],
            'user': config['username'],
            'password': config['password'],
            'connect_timeout': 5  # 5초 타임아웃
        }
        
        # SSL 설정
        if config.get('ssl'):
            conn_params['sslmode'] = 'require'
        
        try:
            # psycopg3를 사용하여 연결 시도
            conn = psycopg.connect(**conn_params)
            
            # 간단한 쿼리로 연결 확인
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
            
            conn.close()
            return True, "연결 성공"
            
        except psycopg.OperationalError as e:
            error_str = str(e)
            
            # 구체적인 오류 메시지 분류
            if "could not connect to server" in error_str or "Name or service not known" in error_str:
                return False, f"호스트를 찾을 수 없음: {config['host']}"
            elif "password authentication failed" in error_str or "authentication failed" in error_str:
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
    
    @staticmethod
    def create_optimized_connection(config: Dict[str, Any]) -> psycopg2.extensions.connection:
        """최적화된 연결 생성"""
        # 연결 파라미터 준비
        conn_params = {
            'host': config['host'],
            'port': config['port'],
            'database': config['database'],
            'user': config['username'],
            'password': config['password'],
        }
        
        # SSL 설정
        if config.get('ssl'):
            conn_params['sslmode'] = 'require'
        
        # 연결 생성
        connection = psycopg2.connect(**conn_params)
        
        # 자동 커밋 비활성화 (대량 작업 최적화)
        connection.autocommit = False
        
        # 세션 최적화 적용
        PostgresOptimizer.apply_bulk_operation_optimizations(connection)
        
        return connection
    
    @staticmethod
    def estimate_table_size(connection, table_name: str) -> Dict[str, Any]:
        """테이블 크기 추정"""
        try:
            with connection.cursor() as cursor:
                # 먼저 테이블 존재 여부 확인
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables 
                        WHERE table_schema = 'public' 
                        AND table_name = %s
                    )
                """, (table_name,))
                
                if not cursor.fetchone()[0]:
                    logger.warning(f"테이블 {table_name}이(가) 존재하지 않습니다")
                    return {
                        'row_count': 0,
                        'total_size_bytes': 0,
                        'total_size_mb': 0,
                        'avg_row_size_bytes': 0,
                        'exists': False
                    }
                
                # 행 수
                cursor.execute(
                    sql.SQL("SELECT COUNT(*) FROM {}").format(
                        sql.Identifier(table_name)
                    )
                )
                row_count = cursor.fetchone()[0]
                
                # 테이블 크기 (bytes)
                cursor.execute("""
                    SELECT pg_total_relation_size(%s)
                """, (table_name,))
                total_size = cursor.fetchone()[0]
                
                # 평균 행 크기
                avg_row_size = total_size / row_count if row_count > 0 else 0
                
                return {
                    'row_count': row_count,
                    'total_size_bytes': total_size,
                    'total_size_mb': total_size / (1024 * 1024),
                    'avg_row_size_bytes': avg_row_size,
                    'exists': True
                }
                
        except Exception as e:
            # 트랜잭션 오류 시 롤백
            try:
                connection.rollback()
            except:
                pass
            logger.error(f"테이블 크기 추정 실패: {e}")
            return {
                'row_count': 0,
                'total_size_bytes': 0,
                'total_size_mb': 0,
                'avg_row_size_bytes': 0,
                'exists': False
            }