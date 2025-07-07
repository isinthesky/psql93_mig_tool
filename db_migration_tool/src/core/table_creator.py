"""
대상 테이블 생성 모듈
"""
from typing import Dict, Any
import psycopg
from psycopg import sql
from datetime import datetime


class TableCreator:
    """대상 테이블 생성 클래스"""
    
    def __init__(self, source_conn: psycopg.Connection, target_conn: psycopg.Connection):
        self.source_conn = source_conn
        self.target_conn = target_conn
        
    def create_partition_table(self, partition_name: str) -> bool:
        """파티션 테이블 생성"""
        try:
            # 부모 테이블 이름 추출 (예: point_history_221026 -> point_history)
            parent_table = '_'.join(partition_name.split('_')[:-1])
            
            # 소스에서 파티션 정보 가져오기
            partition_info = self._get_partition_info(partition_name)
            if not partition_info:
                raise Exception(f"파티션 정보를 찾을 수 없습니다: {partition_name}")
                
            print(f"파티션 정보: {partition_name} - {partition_info}")
                
            # 대상에 부모 테이블 존재 확인
            if not self._check_parent_table_exists(parent_table):
                print(f"부모 테이블 {parent_table}이 없어 생성합니다")
                # 부모 테이블 생성
                self._create_parent_table(parent_table)
                
            # 파티션 테이블 생성
            print(f"파티션 테이블 {partition_name} 생성 중...")
            self._create_partition(partition_name, parent_table, partition_info)
            
            # partition_table_info에 추가
            self._add_partition_info(partition_name, partition_info)
            
            return True
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            raise Exception(f"테이블 생성 오류: {str(e)}")
            
    def _get_partition_info(self, partition_name: str) -> Dict[str, Any]:
        """소스에서 파티션 정보 조회"""
        with self.source_conn.cursor() as cur:
            # partition_table_info에서 정보 조회
            cur.execute("""
                SELECT table_data, from_date, to_date
                FROM partition_table_info
                WHERE table_name = %s
            """, (partition_name,))
            
            row = cur.fetchone()
            if row:
                return {
                    'table_data': row[0],
                    'from_date': row[1],
                    'to_date': row[2]
                }
                
            # partition_table_info에 없으면 테이블에서 직접 확인
            # 날짜 추출 (예: point_history_221026 -> 22, 10, 26)
            parts = partition_name.split('_')
            if len(parts) >= 3 and len(parts[-1]) == 6:
                date_str = parts[-1]  # 221026
                year = 2000 + int(date_str[:2])
                month = int(date_str[2:4])
                day = int(date_str[4:6])
                
                # 시작과 종료 타임스탬프 계산
                from_date = datetime(year, month, day, 0, 0, 0)
                to_date = datetime(year, month, day, 23, 59, 59, 999000)
                
                return {
                    'table_data': 'PH',
                    'from_date': int(from_date.timestamp() * 1000),
                    'to_date': int(to_date.timestamp() * 1000)
                }
                
            return None
            
    def _check_parent_table_exists(self, parent_table: str) -> bool:
        """부모 테이블 존재 확인"""
        with self.target_conn.cursor() as cur:
            cur.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_schema = 'public' 
                    AND table_name = %s
                )
            """, (parent_table,))
            return cur.fetchone()[0]
            
    def _create_parent_table(self, parent_table: str):
        """부모 테이블 생성"""
        with self.source_conn.cursor() as source_cur:
            # 소스에서 테이블 구조 가져오기
            source_cur.execute(f"""
                SELECT 
                    column_name,
                    data_type,
                    character_maximum_length,
                    is_nullable,
                    column_default
                FROM information_schema.columns
                WHERE table_name = '{parent_table}'
                ORDER BY ordinal_position
            """)
            
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
            
            # 인덱스 생성
            if parent_table == 'point_history':
                target_cur.execute("""
                    CREATE INDEX IF NOT EXISTS point_history_path_id_date 
                    ON point_history USING btree (path_id, issued_date);
                    
                    CREATE INDEX IF NOT EXISTS point_history_path_id_idx 
                    ON point_history USING btree (path_id);
                """)
                
                # 트리거 함수 생성 (필요한 경우)
                target_cur.execute("""
                    CREATE OR REPLACE FUNCTION point_history_partition_insert()
                    RETURNS trigger
                    LANGUAGE plpgsql
                    AS $function$
                    DECLARE
                    _insert_time bigint;
                    _insert_date text;
                     
                    BEGIN
                        _insert_time := (NEW.issued_date/1000)::bigint;
                        _insert_date := to_char(to_timestamp(_insert_time), 'YYMMDD');
                     
                        EXECUTE  'INSERT INTO point_history_'||_insert_date||' VALUES ($1.*);' USING NEW;
                     
                        RETURN NULL;
                    END;
                    $function$
                """)
                
                # 트리거 생성
                target_cur.execute("""
                    DROP TRIGGER IF EXISTS insert_point_history_trigger ON point_history;
                    
                    CREATE TRIGGER insert_point_history_trigger
                    BEFORE INSERT ON point_history
                    FOR EACH ROW EXECUTE PROCEDURE point_history_partition_insert();
                """)
                
            self.target_conn.commit()
            
    def _create_partition(self, partition_name: str, parent_table: str, 
                         partition_info: Dict[str, Any]):
        """파티션 테이블 생성"""
        with self.target_conn.cursor() as cur:
            # 파티션 테이블 생성
            if parent_table == 'point_history':
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_name} (
                        CONSTRAINT {partition_name}_pkey PRIMARY KEY(path_id, issued_date),
                        CONSTRAINT {partition_name}_issued_date_check 
                            CHECK(issued_date >= {partition_info['from_date']} 
                              AND issued_date <= {partition_info['to_date']})
                    ) INHERITS ({parent_table})
                """
            else:
                # 다른 테이블 타입을 위한 기본 생성
                create_sql = f"""
                    CREATE TABLE IF NOT EXISTS {partition_name} (
                        CHECK(issued_date >= {partition_info['from_date']} 
                          AND issued_date <= {partition_info['to_date']})
                    ) INHERITS ({parent_table})
                """
            
            cur.execute(create_sql)
            
            # 인덱스가 있는 경우에만 클러스터링
            if parent_table == 'point_history':
                cur.execute(f"""
                    CLUSTER {partition_name} USING {partition_name}_pkey
                """)
            
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
            cur.execute("""
                SELECT 1 FROM partition_table_info 
                WHERE table_name = %s
            """, (partition_name,))
            
            if not cur.fetchone():
                # 새 레코드 추가
                cur.execute("""
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